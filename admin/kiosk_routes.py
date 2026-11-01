from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
import os
import secrets

from flask import (
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from werkzeug.utils import secure_filename

from website.extensions import csrf, db
from website.models import (
    Admin,
    Announcement,
    Application,
    Employer,
    EmployerRegistrationRequest,
    Job,
    JobSeeker,
    Notification,
    Referral,
    User,
    JobFair,
    JobFairEmployerRegistration,
    JobFairRegistration,
    Kiosk
)
from website.utils.email import send_email
from website.utils.notifications import notify_role, notify_user
from website.utils.decorators import role_required

from . import admin


def _kiosk_password_error(password):
    """Match the authentication password policy for kiosk credentials."""
    import re
    if not 8 <= len(password) <= 128:
        return "Kiosk password must contain 8 to 128 characters."
    if not re.search(r"[A-Z]", password):
        return "Kiosk password must contain an uppercase letter."
    if not re.search(r"[a-z]", password):
        return "Kiosk password must contain a lowercase letter."
    if not re.search(r"[0-9]", password):
        return "Kiosk password must contain a number."
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        return "Kiosk password must contain a special character (!@#$%^&*(),.?\":{}|<>)."
    return None



@admin.route("/kiosk-management")
@login_required
@role_required("admin")
def kiosk_management():

    search = request.args.get(
        "search",
        ""
    ).strip()


    # ==========================================================
    # KIOSK ACCOUNTS
    # ==========================================================

    kiosk_query = (
        Kiosk.query
        .join(
            User,
            Kiosk.user_id == User.user_id
        )
    )


    # ==========================================================
    # SEARCH
    # ==========================================================

    if search:

        search_pattern = f"%{search}%"


        # ------------------------------------------------------
        # Try to match Kiosk ID
        # ------------------------------------------------------

        kiosk_id_match = None

        search_id = search.upper().replace(
            "KSK-",
            ""
        )

        if search_id.isdigit():

            kiosk_id_match = int(search_id)


        # ------------------------------------------------------
        # Text search
        # ------------------------------------------------------

        search_filters = [

            Kiosk.kiosk_name.ilike(
                search_pattern
            ),

            Kiosk.location.ilike(
                search_pattern
            ),

            User.username.ilike(
                search_pattern
            ),

            Kiosk.status.ilike(
                search_pattern
            ),

            Kiosk.mode.ilike(
                search_pattern
            ),

        ]


        # ------------------------------------------------------
        # Include exact Kiosk ID when applicable
        # ------------------------------------------------------

        if kiosk_id_match is not None:

            search_filters.append(
                Kiosk.kiosk_id == kiosk_id_match
            )


        kiosk_query = kiosk_query.filter(
            or_(*search_filters)
        )


    # ==========================================================
    # ORDER
    # ==========================================================

    kiosks = (
        kiosk_query
        .order_by(
            Kiosk.kiosk_id.desc()
        )
        .all()
    )


    # Reconcile finished assignments before rendering management status.
    from website.utils.job_fair_lifecycle import is_past_scheduled_end
    changed = False
    for kiosk_record in kiosks:
        if kiosk_record.mode not in ("job_fair", "queue_display") or not kiosk_record.job_fair_id:
            continue
        assigned_fair = db.session.get(JobFair, kiosk_record.job_fair_id)
        if assigned_fair is None or assigned_fair.status == "Finished" or is_past_scheduled_end(assigned_fair):
            kiosk_record.mode = "standby"
            kiosk_record.job_fair_id = None
            changed = True
    if changed:
        db.session.commit()

    # ==========================================================
    # ASSIGNED JOB FAIRS
    #
    # Fetch the Job Fairs assigned to the returned kiosks.
    #
    # This is intentionally separate from the ongoing Job Fair
    # list because a kiosk may still display its previously
    # assigned Job Fair even after that Job Fair has finished.
    # ==========================================================

    assigned_job_fair_ids = [
        kiosk.job_fair_id
        for kiosk in kiosks
        if kiosk.job_fair_id is not None
    ]


    assigned_job_fairs = {}


    if assigned_job_fair_ids:

        assigned_job_fair_records = (
            JobFair.query
            .filter(
                JobFair.job_fair_id.in_(
                    assigned_job_fair_ids
                )
            )
            .all()
        )


        assigned_job_fairs = {
            fair.job_fair_id: fair
            for fair in assigned_job_fair_records
        }


    # ==========================================================
    # ONGOING JOB FAIRS
    # ==========================================================

    ongoing_job_fairs = (
        JobFair.query
        .filter_by(
            status="Ongoing"
        )
        .order_by(
            JobFair.start_datetime.asc()
        )
        .all()
    )


    # ==========================================================
    # RENDER
    # ==========================================================

    return render_template(
        "admin/admin_kiosk_management.html",
        kiosks=kiosks,
        ongoing_job_fairs=ongoing_job_fairs,
        assigned_job_fairs=assigned_job_fairs,
        search=search
    )


@admin.route(
    "/kiosk-management/create",
    methods=["POST"]
)
@login_required
@role_required("admin")
def create_kiosk():

    kiosk_name = request.form.get(
        "kiosk_name",
        ""
    ).strip()

    username = request.form.get(
        "username",
        ""
    ).strip()

    password = request.form.get(
        "password",
        ""
    )

    location = request.form.get(
        "location",
        ""
    ).strip()

    description = request.form.get(
        "description",
        ""
    ).strip()


    # Validate on the server; browser maxlength can be bypassed.
    if (
        len(kiosk_name) > 100
        or len(location) > 255
        or len(username) > 50
        or len(description) > 5000
    ):
        flash("Kiosk name (100), location (255), username (50), or description (5000) exceeds its limit.", "error")
        return redirect(url_for("admin.kiosk_management"))

    if len(password) > 128:
        flash("Kiosk password must be at most 128 characters.", "error")
        return redirect(url_for("admin.kiosk_management"))

    if password:
        password_error = _kiosk_password_error(password)
        if password_error:
            flash(password_error, "error")
            return redirect(url_for("admin.kiosk_management"))

    # ==========================================================
    # BASIC VALIDATION
    # ==========================================================

    if (
        not kiosk_name
        or not username
        or not password
        or not location
    ):

        flash(
            "Please fill in all required kiosk account fields.",
            "error"
        )

        return redirect(
            url_for("admin.kiosk_management")
        )


    # ==========================================================
    # CHECK USERNAME
    # ==========================================================

    existing_user = User.query.filter_by(
        username=username
    ).first()


    if existing_user:

        flash(
            "That username is already in use.",
            "error"
        )

        return redirect(
            url_for("admin.kiosk_management")
        )


    # ==========================================================
    # CREATE ACCOUNT
    # ==========================================================

    try:

        # ------------------------------------------------------
        # Create User
        # ------------------------------------------------------

        user = User(
            username=username,
            email=f"{username}@gmail.com",
            role="kiosk",
            status="active",
            email_verified=True
        )

        user.set_password(password)

        db.session.add(user)


        # Generate user_id
        db.session.flush()


        # ------------------------------------------------------
        # Create Kiosk
        # ------------------------------------------------------

        kiosk = Kiosk(
            user_id=user.user_id,
            kiosk_name=kiosk_name,
            location=location,
            description=description,
            status="active"
        )

        db.session.add(kiosk)


        # ------------------------------------------------------
        # Commit
        # ------------------------------------------------------

        db.session.commit()


    except Exception:

        db.session.rollback()

        current_app.logger.exception(
            "Error creating kiosk account"
        )

        flash(
            "An error occurred while creating the kiosk account.",
            "error"
        )

        return redirect(
            url_for("admin.kiosk_management")
        )


    # ==========================================================
    # SUCCESS
    # ==========================================================

    flash(
        f"Kiosk account '{kiosk_name}' was created successfully.",
        "success"
    )

    return redirect(
        url_for("admin.kiosk_management")
    )


@admin.route(
    "/kiosk-management/<int:kiosk_id>/manage",
    methods=["POST"]
)
@login_required
@role_required("admin")
def manage_kiosk(kiosk_id):

    kiosk = Kiosk.query.get_or_404(
        kiosk_id
    )


    mode = request.form.get(
        "mode",
        "standby"
    ).strip()


    job_fair_id = request.form.get(
        "job_fair_id",
        ""
    ).strip()


    status = request.form.get(
        "status",
        "active"
    ).strip()


    # ==========================================================
    # VALIDATE MODE
    # ==========================================================

    if mode not in (
        "standby",
        "job_fair",
        "queue_display"
    ):

        flash(
            "Invalid kiosk mode.",
            "error"
        )

        return redirect(
            url_for("admin.kiosk_management")
        )


    if status not in (
        "active",
        "disabled"
    ):

        flash(
            "Invalid kiosk status.",
            "error"
        )

        return redirect(
            url_for("admin.kiosk_management")
        )


    kiosk.status = status


    # ==========================================================
    # STANDBY MODE
    # ==========================================================

    if mode == "standby":

        kiosk.mode = "standby"

        # Remove any previous Job Fair assignment
        kiosk.job_fair_id = None


    # ==========================================================
    # JOB FAIR MODE
    # ==========================================================

    elif mode in ("job_fair", "queue_display"):

        if not job_fair_id:

            flash(
                "Please select an ongoing Job Fair.",
                "error"
            )

            return redirect(
                url_for("admin.kiosk_management")
            )


        # ------------------------------------------------------
        # IMPORTANT:
        # Only allow an ACTUALLY ONGOING Job Fair
        # ------------------------------------------------------

        job_fair = (
            JobFair.query
            .filter(
                JobFair.job_fair_id == job_fair_id,
                JobFair.status == "Ongoing",
                JobFair.start_datetime <= datetime.now(ZoneInfo("Asia/Manila")).replace(tzinfo=None),
                JobFair.end_datetime > datetime.now(ZoneInfo("Asia/Manila")).replace(tzinfo=None)
            )
            .first()
        )


        if not job_fair:

            flash(
                "The selected Job Fair is no longer ongoing.",
                "error"
            )

            return redirect(
                url_for("admin.kiosk_management")
            )


        kiosk.mode = mode

        kiosk.job_fair_id = job_fair.job_fair_id


    # ==========================================================
    # SAVE
    # ==========================================================

    try:

        db.session.commit()

    except Exception:

        db.session.rollback()

        current_app.logger.exception(
            "Error managing kiosk %s",
            kiosk_id
        )

        flash(
            "An error occurred while updating the kiosk.",
            "error"
        )

        return redirect(
            url_for("admin.kiosk_management")
        )


    # ==========================================================
    # SUCCESS
    # ==========================================================

    if kiosk.mode == "standby":

        flash(
            f"Kiosk '{kiosk.kiosk_name}' is now in Standby / View Jobs mode.",
            "success"
        )

    elif kiosk.mode == "queue_display":

        flash(
            f"Kiosk '{kiosk.kiosk_name}' is locked to Queue Display for "
            f"'{job_fair.title}'.",
            "success"
        )

    else:

        flash(
            f"Kiosk '{kiosk.kiosk_name}' is now assigned to Job Fair mode for "
            f"'{job_fair.title}'.",
            "success"
        )


    return redirect(
        url_for("admin.kiosk_management")
    )


@admin.route(
    "/kiosk-management/<int:kiosk_id>/edit",
    methods=["POST"]
)
@login_required
@role_required("admin")
def edit_kiosk(kiosk_id):

    kiosk = Kiosk.query.get_or_404(
        kiosk_id
    )

    user = kiosk.user


    kiosk_name = request.form.get(
        "kiosk_name",
        ""
    ).strip()


    location = request.form.get(
        "location",
        ""
    ).strip()


    description = request.form.get(
        "description",
        ""
    ).strip()


    username = request.form.get(
        "username",
        ""
    ).strip()


    password = request.form.get(
        "password",
        ""
    )


    # Validate on the server; browser maxlength can be bypassed.
    if (
        len(kiosk_name) > 100
        or len(location) > 255
        or len(username) > 50
        or len(description) > 5000
    ):
        flash("Kiosk name (100), location (255), username (50), or description (5000) exceeds its limit.", "error")
        return redirect(url_for("admin.kiosk_management"))

    if len(password) > 128:
        flash("Kiosk password must be at most 128 characters.", "error")
        return redirect(url_for("admin.kiosk_management"))

    if password:
        password_error = _kiosk_password_error(password)
        if password_error:
            flash(password_error, "error")
            return redirect(url_for("admin.kiosk_management"))

    # ==========================================================
    # REQUIRED FIELDS
    # ==========================================================

    if (
        not kiosk_name
        or not location
        or not username
    ):

        flash(
            "Kiosk name, location, and username are required.",
            "error"
        )

        return redirect(
            url_for("admin.kiosk_management")
        )


    # ==========================================================
    # CHECK USERNAME
    # ==========================================================

    existing_user = (
        User.query
        .filter(
            User.username == username,
            User.user_id != user.user_id
        )
        .first()
    )


    if existing_user:

        flash(
            "That username is already in use.",
            "error"
        )

        return redirect(
            url_for("admin.kiosk_management")
        )


    # ==========================================================
    # UPDATE KIOSK
    # ==========================================================

    kiosk.kiosk_name = kiosk_name

    kiosk.location = location

    kiosk.description = description


    # ==========================================================
    # UPDATE USERNAME
    # ==========================================================

    user.username = username


    # ==========================================================
    # OPTIONAL PASSWORD CHANGE
    # ==========================================================

    if password:

        user.set_password(password)


    # ==========================================================
    # SAVE
    # ==========================================================

    try:

        db.session.commit()

    except Exception:

        db.session.rollback()

        current_app.logger.exception(
            "Error editing kiosk %s",
            kiosk_id
        )

        flash(
            "An error occurred while updating the kiosk.",
            "error"
        )

        return redirect(
            url_for("admin.kiosk_management")
        )


    flash(
        f"Kiosk '{kiosk.kiosk_name}' was updated successfully.",
        "success"
    )

    return redirect(
        url_for("admin.kiosk_management")
    )