from website.utils.registration_time import registration_datetime_ph
import re
from datetime import datetime, timedelta, timezone, date
import os
import secrets
from io import StringIO, BytesIO
import csv
from zoneinfo import ZoneInfo
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from flask import (
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
    make_response,
    send_file,
    session
)
from website.utils.application_state import mark_application_reviewed
from website.utils.image_utils import save_job_fair_banner
from website.utils.uploads import save_validated_image
from website.utils.jsrf_excel import generate_jsrf_excel, generate_bulk_jsrf_excel
from website.utils.pdf_generator import generate_referral_pdf
from flask_login import current_user, login_required
from sqlalchemy import func, or_
from website.utils.decorators import role_required
from website.extensions import csrf, db, limiter
from website.utils.application_state import sync_jobseeker_employment_status
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
    JobFairJob,
    JobFairRegistration,
    JobFairCandidateEvaluation,
    JobFairQueue,
    JobFairInterviewStation,
    AuditLog,
    SupervisedRegistrationSession,
)
from website.utils.email import send_email
from website.utils.background_email import send_bulk_email_async
from website.utils.notifications import notify_role, notify_user, commit_notifications
from website.utils.audit import record_audit
from website.utils.referral_flow import (
    validate_referral_for_issue,
    validate_referral_for_rejection,
)
from website.utils.job_fair_lifecycle import (
    close_exclusive_jobs_for_fair,
    close_exclusive_jobs_for_registration,
    sync_exclusive_job_deadlines,
    is_registration_open,
    is_queue_entry_open,
)

from website.utils.job_fair_reporting import (
    build_job_fair_report,
    generate_job_fair_report_excel,
    build_job_fair_employer_report,
    generate_job_fair_employer_report_excel,
)

from . import admin


# --- Helper Function for Role Checks ---
def check_admin_access():
    if not current_user.is_authenticated or current_user.role != "admin":
        return False
    return True


def format_work_date(value):
    if not value:
        return None

    if hasattr(value, "strftime"):
        return value.strftime("%B %d, %Y")

    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d")
        return parsed.strftime("%B %d, %Y")
    except (ValueError, TypeError):
        return str(value)
# ==========================================
# DASHBOARD
# ==========================================


@admin.route("/dashboard")
@login_required
@role_required("admin")
def dashboard():
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    pending_registration_requests = (
        EmployerRegistrationRequest.query
        .filter_by(status="Pending")
        .count()
    )

    total_employers = Employer.query.count()
    jobseeker_count = JobSeeker.query.count()

    active_jobs = (
        Job.query
        .filter(
            Job.status == "Open",
            Job.approval_status == "Approved"
        )
        .count()
    )

    referral_applications = (
        Application.query
        .join(
            Referral,
            Referral.application_id == Application.application_id
        )
        .join(
            Job,
            Job.job_id == Application.job_id
        )
        .filter(
            Job.job_type == "Referral",
            Referral.status == "Pending"
        )
        .order_by(Referral.created_at.desc())
        .limit(5)
        .all()
    )

    announcements = (
        JobFair.query
        .order_by(JobFair.start_datetime.desc())
        .limit(3)
        .all()
    )

    return render_template(
        "admin/admin_dashboard.html",
        pending_registration_requests=pending_registration_requests,
        total_employers=total_employers,
        jobseeker_count=jobseeker_count,
        active_jobs=active_jobs,
        referral_applications=referral_applications,
        announcements=announcements
    )


@admin.route("/profile", methods=["GET", "POST"])
@login_required
@role_required("admin")
def profile():
    if not check_admin_access():
        return render_template("errors/404.html"), 404
        
    if request.method == "POST":
        action = request.form.get("action")
        
        if action == "update_personal":
            from email_validator import validate_email, EmailNotValidError
            from datetime import date
            if not current_user.admin:
                return jsonify(success=False, message="Admin profile record not found."), 404
            first = (request.form.get("first_name") or "").strip()
            last = (request.form.get("last_name") or "").strip()
            phone = (request.form.get("phone") or "").strip()
            email = (request.form.get("email") or "").strip()
            dob = (request.form.get("date_of_birth") or "").strip()
            country = (request.form.get("country") or "").strip()
            city = (request.form.get("city") or "").strip()
            postal = (request.form.get("postal_code") or "").strip()
            if not first or not last or len(first) > 50 or len(last) > 50 or not re.fullmatch(r"[A-Za-zÀ-ÿ .'-]+", first) or not re.fullmatch(r"[A-Za-zÀ-ÿ .'-]+", last):
                return jsonify(success=False, message="Enter valid first and last names (up to 50 characters)."), 400
            if phone and not re.fullmatch(r"09[0-9]{9}", phone):
                return jsonify(success=False, message="Enter an 11-digit Philippine mobile number starting with 09."), 400
            try:
                if not email or len(email) > 100:
                    raise ValueError()
                validate_email(email, check_deliverability=False)
            except (EmailNotValidError, ValueError):
                return jsonify(success=False, message="Enter a valid email address."), 400
            if len(country) > 100 or len(city) > 100:
                return jsonify(success=False, message="Country and city must be at most 100 characters."), 400
            if postal and not re.fullmatch(r"[0-9]{4}", postal):
                return jsonify(success=False, message="Postal code must contain exactly four digits."), 400
            try:
                birth_date = date.fromisoformat(dob) if dob else None
                if birth_date and birth_date > date.today():
                    raise ValueError()
            except ValueError:
                return jsonify(success=False, message="Enter a valid date of birth that is not in the future."), 400
            email_changed = email.casefold() != current_user.email.casefold()
            if email_changed and User.query.filter(func.lower(User.email) == email.lower(), User.user_id != current_user.user_id).first():
                return jsonify(success=False, message="That email address is already in use."), 409
            try:
                admin_record = current_user.admin
                admin_record.first_name = first
                admin_record.last_name = last
                admin_record.contact_number = phone or None
                admin_record.date_of_birth = birth_date
                admin_record.country = country or None
                admin_record.city = city or None
                admin_record.postal_code = postal or None
                # NEVER update the login email before OTP confirmation.
                db.session.commit()
                return jsonify(success=True, email_verification_required=email_changed,
                               message="Profile details saved." if email_changed else "Profile saved successfully.")
            except Exception:
                db.session.rollback()
                current_app.logger.exception("Admin profile update failed")
                return jsonify(success=False, message="Unable to save profile. Please try again."), 500

        if action == "request_email_change":
            from email_validator import validate_email, EmailNotValidError
            from werkzeug.security import generate_password_hash
            new_email = (request.form.get("email") or "").strip()
            try:
                if not new_email or len(new_email) > 100:
                    raise ValueError()
                validate_email(new_email, check_deliverability=False)
            except (EmailNotValidError, ValueError):
                return jsonify(success=False, message="Enter a valid new email address."), 400
            if new_email.casefold() == current_user.email.casefold():
                return jsonify(success=False, message="This is already your account email."), 400
            if User.query.filter(func.lower(User.email) == new_email.lower(), User.user_id != current_user.user_id).first():
                return jsonify(success=False, message="That email address is already in use."), 409
            now = datetime.now(timezone.utc).timestamp()
            pending = session.get("admin_email_change")
            if pending and pending.get("user_id") == current_user.user_id and now - pending.get("sent_at", 0) < 60:
                return jsonify(success=False, message="Please wait 60 seconds before requesting another code."), 429
            code = f"{secrets.randbelow(1000000):06d}"
            try:
                send_email(
                    recipient=new_email,
                    subject="PESO Admin Email Change Verification Code",
                    body=(
                        "Hello,\n\n"
                        "A request was made to change the email address for your PESO administrator account.\n\n"
                        f"Your verification code is: [[important:{code}]]\n\n"
                        "[[bold:This code expires in 10 minutes.]] Do not share this code with anyone.\n\n"
                        "If you did not request this change, ignore this message. "
                        "Your current account email will remain unchanged."
                    ),
                )
            except Exception:
                current_app.logger.exception("Admin email change verification delivery failed")
                return jsonify(success=False, message="Unable to send the verification email. Please try again."), 502
            session["admin_email_change"] = {
                "user_id": current_user.user_id,
                "email": new_email,
                "code_hash": generate_password_hash(code),
                "expires_at": now + 600,
                "sent_at": now,
                "attempts": 0,
            }
            session.modified = True
            return jsonify(success=True, message="Verification code sent to your new email address.")

        if action == "confirm_email_change":
            from werkzeug.security import check_password_hash
            code = (request.form.get("code") or "").strip()
            pending = session.get("admin_email_change")
            now = datetime.now(timezone.utc).timestamp()
            if not pending or pending.get("user_id") != current_user.user_id:
                return jsonify(success=False, message="No pending email verification. Request a new code."), 400
            if now > pending.get("expires_at", 0):
                session.pop("admin_email_change", None)
                return jsonify(success=False, message="Verification code expired. Request a new code."), 400
            if pending.get("attempts", 0) >= 5:
                session.pop("admin_email_change", None)
                return jsonify(success=False, message="Too many incorrect attempts. Request a new code."), 429
            if not re.fullmatch(r"[0-9]{6}", code) or not check_password_hash(pending["code_hash"], code):
                pending["attempts"] = pending.get("attempts", 0) + 1
                session["admin_email_change"] = pending
                return jsonify(success=False, message="Incorrect verification code."), 400
            email = pending["email"]
            if User.query.filter(func.lower(User.email) == email.lower(), User.user_id != current_user.user_id).first():
                session.pop("admin_email_change", None)
                return jsonify(success=False, message="That email address is already in use."), 409
            try:
                current_user.email = email
                db.session.commit()
                session.pop("admin_email_change", None)
                return jsonify(success=True, message="Email address verified and updated.")
            except Exception:
                db.session.rollback()
                current_app.logger.exception("Admin verified email update failed")
                return jsonify(success=False, message="Unable to update email. Please try again."), 500

        if action == "upload_photo":
            file = request.files.get("profile_picture")
            if not file or not file.filename:
                flash("Please select an image to upload.", "danger")
                return redirect(url_for("admin.profile"))

            if not current_user.admin:
                flash("Admin profile record not found.", "danger")
                return redirect(url_for("admin.profile"))

            try:
                upload_folder = os.path.join(
                    current_app.root_path,
                    "static",
                    "uploads",
                    "profiles",
                )
                profile_pic_filename = save_validated_image(
                    file,
                    upload_folder,
                    f"admin_{current_user.user_id}",
                )
                current_user.admin.profile_picture = profile_pic_filename
                db.session.commit()
                flash("Profile picture updated successfully!", "success")
            except ValueError as exc:
                db.session.rollback()
                flash(str(exc), "danger")
            except Exception:
                db.session.rollback()
                current_app.logger.exception("Admin profile image upload failed")
                flash("The profile picture could not be uploaded.", "danger")

            return redirect(url_for("admin.profile"))
            
    return render_template("admin/admin_profile.html")

# ==========================================
# EMPLOYER MANAGEMENT
# ==========================================


@admin.route("/employers")
@login_required
@role_required("admin")
def all_employers():
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    search = request.args.get("search", "").strip()
    page = request.args.get("page", 1, type=int)

    query = Employer.query

    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            or_(
                Employer.company_name.ilike(search_filter),
                Employer.company_email.ilike(search_filter),
                Employer.company_phone.ilike(search_filter),
            )
        )

    if hasattr(Employer, "created_at"):
        employers = query.order_by(Employer.created_at.desc()).paginate(
            page=page, per_page=15, error_out=False
        )
    else:
        employers = query.paginate(page=page, per_page=15, error_out=False)

    return render_template(
        "admin/admin_employers.html",
        employers=employers,
        total_employers=Employer.query.count(),
        search=search,
    )


@admin.route("/employers/export", methods=["GET"])
@login_required
@role_required("admin")
@limiter.limit("10 per hour", methods=["GET"])
def export_employers():
    """Export registered employers as a JSRF-style Excel masterlist."""
    if not check_admin_access():
        abort(403)

    # Explicit bounds prevent accidental unbounded personal-data exports.
    query = Employer.query.join(User, Employer.user_id == User.user_id)
    if query.count() > 10000:
        abort(400, description="Too many employers to export at once.")

    employers = query.order_by(Employer.employer_id.asc()).all()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Registered Employers"
    headers = [
        "Employer ID", "Company Name", "Company Email", "Contact Number",
        "Street", "Barangay", "City", "Province", "Industry",
        "Company Size", "Website", "Account Status", "Verification Status",
        "Registered On", "Login Email",
    ]
    sheet.append(headers)
    ph_tz = ZoneInfo("Asia/Manila")

    def clean(value):
        if value is None:
            return ""
        value = str(value)
        # Excel formula injection protection for user-controlled strings.
        if value.lstrip().startswith(("=", "+", "-", "@")):
            value = "'" + value
        return value

    for employer in employers:
        user = employer.user
        registered = ""
        if user and user.created_at:
            timestamp = user.created_at
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            registered = timestamp.astimezone(ph_tz).strftime("%Y-%m-%d %I:%M %p")
        sheet.append([
            clean(user.public_id if user else employer.employer_id),
            clean(employer.company_name),
            clean(employer.company_email),
            clean(employer.company_phone),
            clean(employer.street),
            clean(employer.barangay),
            clean(employer.city),
            clean(employer.province),
            clean(employer.company_industry),
            clean(employer.company_size),
            clean(employer.website),
            clean(employer.account_status),
            clean(employer.verified),
            registered,
            clean(user.email if user else ""),
        ])

    navy = PatternFill("solid", fgColor="24547C")
    white = Font(name="Arial", size=10, bold=True, color="FFFFFF")
    thin_border = Border(
        bottom=Side(style="thin", color="DEE4EA"),
        right=Side(style="thin", color="DEE4EA"),
    )
    for cell in sheet[1]:
        cell.fill = navy
        cell.font = white
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = thin_border
    sheet.row_dimensions[1].height = 29
    widths = [21, 34, 33, 19, 34, 23, 23, 23, 26, 19, 34, 19, 21, 25, 32]
    for idx, width in enumerate(widths, 1):
        sheet.column_dimensions[get_column_letter(idx)].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.font = Font(name="Arial", size=10, color="1E293B")
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            cell.border = thin_border
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions

    stream = BytesIO()
    workbook.save(stream)
    stream.seek(0)
    filename = f"Registered_Employers_Masterlist_{datetime.now(ph_tz):%Y%m%d_%H%M%S}.xlsx"
    return send_file(
        stream,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _valid_employer_permit_values(values):
    """Reject control characters and oversized permit identifiers."""
    return all(
        len(value) <= 100
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
        for value in values
    )


@admin.route("/employer/<string:public_id>", methods=["GET", "POST"])
@login_required
@role_required("admin")
def employer_profile(public_id):
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    employer = (Employer.query.join(User, Employer.user_id == User.user_id).filter(User.public_id == public_id).first_or_404())

    if request.method == "POST":
        # Never trust hidden/modified browser fields. This page only edits
        # permit identifiers; account status is controlled by dedicated
        # server-side workflows and cannot be injected through DevTools.
        permit_values = {
            "business_permit_no": (request.form.get("business_permit_no") or "").strip(),
            "mayor_permit_no": (request.form.get("mayor_permit_no") or "").strip(),
            "bir_no": (request.form.get("bir_no") or "").strip(),
        }
        if not _valid_employer_permit_values(permit_values.values()):
            flash("Permit/TIN values must be at most 100 characters and cannot contain control characters.", "danger")
            return redirect(url_for("admin.employer_profile", public_id=employer.user.public_id))

        employer.business_permit_no = permit_values["business_permit_no"] or None
        employer.mayor_permit_no = permit_values["mayor_permit_no"] or None
        employer.bir_no = permit_values["bir_no"] or None

        try:
            db.session.commit()
            flash("Employer permit details updated successfully!", "success")
        except Exception:
            db.session.rollback()
            flash("Failed to update employer permit details.", "danger")

        return redirect(
            url_for("admin.employer_profile", public_id=employer.user.public_id)
        )

    jobs = Job.query.filter_by(employer_id=employer.employer_id).all()

    active_jobs = Job.query.filter_by(
        employer_id=employer.employer_id, status="Open"
    ).count()

    total_applicants = (
        Application.query.join(Job)
        .filter(Job.employer_id == employer.employer_id)
        .count()
    )

    hired_applicants = (
        Application.query.join(Job)
        .filter(
            Job.employer_id == employer.employer_id,
            Application.status == "Accepted",
        )
        .count()
    )

    return render_template(
        "admin/admin_employer_profile.html",
        employer=employer,
        jobs=jobs,
        active_jobs=active_jobs,
        total_applicants=total_applicants,
        hired_applicants=hired_applicants,
    )
@admin.route('/employer/<string:public_id>/update-permits', methods=['POST'])
@login_required
@role_required("admin")
def update_employer_permits(public_id):
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    employer = (Employer.query.join(User, Employer.user_id == User.user_id).filter(User.public_id == public_id).first_or_404())

    # 1. Capture and validate form values from modal inputs
    business_permit = (request.form.get('business_permit') or '').strip()
    mayors_permit = (request.form.get('mayors_permit') or '').strip()
    bir_tin = (request.form.get('bir_tin') or '').strip()

    if not _valid_employer_permit_values((business_permit, mayors_permit, bir_tin)):
        flash('Permit/TIN values must be at most 100 characters and cannot contain control characters.', 'danger')
        return redirect(url_for('admin.employer_profile', public_id=employer.user.public_id))

    # 2. Map form inputs to actual Employer model column names
    employer.business_permit_no = business_permit if business_permit else None
    employer.mayor_permit_no = mayors_permit if mayors_permit else None
    employer.bir_no = bir_tin if bir_tin else None

    try:
        # 3. Commit changes to the database
        db.session.commit()
        flash('Legal & verification permits updated successfully!', 'success')
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Unable to update employer permits for %s', public_id)
        flash('Unable to update employer permit details. Please try again.', 'danger')

    # 4. Redirect back to profile page
    return redirect(url_for('admin.employer_profile', public_id=employer.user.public_id))


@admin.route("/company/<string:public_id>/jobs")
@login_required
@role_required("admin")
def company_jobs(public_id):
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    employer = (Employer.query.join(User, Employer.user_id == User.user_id).filter(User.public_id == public_id).first_or_404())
    jobs = Job.query.filter_by(employer_id=employer.employer_id).all()

    return render_template(
        "admin/admin_company_jobs.html", employer=employer, jobs=jobs
    )


# ==========================================
# EMPLOYER REGISTRATION REQUESTS
# ==========================================


@admin.route("/employer-registration-requests")
@login_required
@role_required("admin")
def employer_registration_requests():
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    status = request.args.get("status", "all")
    search = request.args.get("search", "").strip()
    page = request.args.get("page", 1, type=int)

    query = EmployerRegistrationRequest.query

    if search:
        query = query.filter(
            EmployerRegistrationRequest.email.ilike(f"%{search}%")
        )

    normalized_status = status.lower()

    if normalized_status == "approved":
        query = query.filter(
            EmployerRegistrationRequest.status.in_(["Approved", "Completed"])
        )
    elif normalized_status == "pending":
        query = query.filter(EmployerRegistrationRequest.status == "Pending")
    elif normalized_status == "rejected":
        query = query.filter(EmployerRegistrationRequest.status == "Rejected")

    requests = query.order_by(
        EmployerRegistrationRequest.created_at.desc()
    ).paginate(page=page, per_page=15, error_out=False)

    return render_template(
        "admin/admin_verification.html",
        requests=requests,
        total_employers=EmployerRegistrationRequest.query.count(),
        pending_count=EmployerRegistrationRequest.query.filter_by(
            status="Pending"
        ).count(),
        approved_count=EmployerRegistrationRequest.query.filter(
            EmployerRegistrationRequest.status.in_(["Approved", "Completed"])
        ).count(),
        rejected_count=EmployerRegistrationRequest.query.filter_by(
            status="Rejected"
        ).count(),
        current_status=status,
        search=search,
    )


@admin.route("/employer-registration/approve/<int:request_id>", methods=["POST"])
@login_required
@role_required("admin")
def approve_employer_registration(request_id):
    if not check_admin_access():
        flash("Unauthorized access.", "danger")
        return redirect(url_for("admin.dashboard"))

    employer_request = EmployerRegistrationRequest.query.get_or_404(request_id)

    if employer_request.status != "Pending":
        flash("This request was already processed.", "warning")
        return redirect(url_for("admin.employer_registration_requests"))

    now_utc = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)

    employer_request.registration_token = token
    employer_request.token_expiry = now_utc + timedelta(hours=24)
    employer_request.status = "Approved"
    employer_request.approved_at = now_utc

    target_user = User.query.filter_by(email=employer_request.email).first()

    if target_user:
        notification = Notification(
            user_id=target_user.user_id,
            title="Employer Registration Approved",
            message=(
                "Your employer registration request has been approved. "
                "Please check your email to complete your setup."
            ),
            notification_type="Employer Registration",
        )
        db.session.add(notification)

    record_audit(
        f"EMPLOYER_REGISTRATION_APPROVED request_id={employer_request.request_id}"
    )

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("Failed to approve employer registration request %s: %s", request_id, e)
        flash("Unable to approve employer registration.", "danger")
        return redirect(url_for("admin.employer_registration_requests"))

    registration_link = url_for(
        "auth.complete_employer_registration",
        token=token,
        _external=True,
    )

    email_body = f"""Hello,

Your employer registration request has been approved after document verification at the PESO office.

Please complete your company registration and set up your password using this secure link:

{registration_link}

This link will expire after 24 hours.

Thank you.

PESO Management Information System"""

    try:
        send_email(
            recipient=employer_request.email,
            subject="Employer Registration Approved - Complete Your Account",
            body=email_body,
        )
        flash("Employer approved. Registration link successfully sent via email.", "success")
    except Exception as e:
        current_app.logger.exception("Employer registration approval email failed for request %s: %s", request_id, e)
        flash(
            "Employer was approved, but the registration email could not be sent. "
            "Check the mail logs and resend if necessary.",
            "warning",
        )

    return redirect(url_for("admin.employer_registration_requests"))


@admin.route("/employer-registration/reject/<int:request_id>", methods=["POST"])
@login_required
@role_required("admin")
def reject_employer_registration(request_id):
    if not check_admin_access():
        flash("Unauthorized access.", "danger")
        return redirect(url_for("admin.dashboard"))

    employer_request = EmployerRegistrationRequest.query.get_or_404(request_id)

    if employer_request.status != "Pending":
        flash(
            "This employer registration request was already processed.",
            "warning",
        )
        return redirect(
            url_for("admin.employer_registration_requests")
        )

    remarks = (request.form.get("remarks") or "").strip() or "Requirements were not approved."

    if len(remarks) > 1000:
        flash("Rejection remarks must not exceed 1,000 characters.", "danger")
        return redirect(url_for("admin.employer_registration_requests"))

    employer_request.status = "Rejected"
    employer_request.remarks = remarks

    target_user = User.query.filter_by(email=employer_request.email).first()

    if target_user:
        notify_user(
            target_user.user_id,
            "Employer Registration Rejected",
            "Your employer registration request was rejected. Please review the remarks and contact the PESO office.",
            "Employer Registration",
        )

    record_audit(
        f"EMPLOYER_REGISTRATION_REJECTED request_id={employer_request.request_id}"
    )

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("Failed to reject employer registration request %s: %s", request_id, e)
        flash("Unable to reject employer registration.", "danger")
        return redirect(url_for("admin.employer_registration_requests"))

    email_body = f"""Hello,

Your employer registration request has been reviewed by the Public Employment Service Office (PESO) and was not approved.

Remarks:
{remarks}

If you need clarification or wish to submit updated requirements, please contact the PESO office.

Best regards,
PESO Management Information System"""

    try:
        send_email(
            recipient=employer_request.email,
            subject="Employer Registration Request Update",
            body=email_body,
        )
        flash("Employer registration rejected and email sent.", "success")
    except Exception as e:
        current_app.logger.exception("Employer registration rejection email failed for request %s: %s", request_id, e)
        flash("Employer registration was rejected, but the email could not be sent.", "warning")

    return redirect(url_for("admin.employer_registration_requests"))



# ==========================================
# REFERRALS & APPLICATIONS
# ==========================================

@admin.route("/job_referral")
@login_required
@role_required("admin")
def job_referral():

    if not check_admin_access():
        return render_template(
            "errors/404.html"
        ), 404

    search_query = request.args.get(
        "search",
        ""
    ).strip()

    query = (
        Job.query
        .join(
            Application,
            Application.job_id == Job.job_id
        )
        .join(
            Referral,
            Referral.application_id
            == Application.application_id
        )
        .filter(
            Job.job_type == "Referral"
        )
        .distinct()
    )

    if search_query:

        search_filter = f"%{search_query}%"

        query = query.filter(
            or_(
                Job.job_title.ilike(
                    search_filter
                ),
                Job.location.ilike(
                    search_filter
                ),
            )
        )

    jobs = (
        query
        .order_by(
            Job.created_at.desc()
        )
        .all()
    )

    for job in jobs:

        referral_query = (
            Referral.query
            .join(
                Application,
                Referral.application_id
                == Application.application_id
            )
            .filter(
                Application.job_id
                == job.job_id
            )
        )

        job.referral_request_count = (
            referral_query.count()
        )

        job.pending_referral_count = (
            referral_query
            .filter(
                Referral.status == "Pending"
            )
            .count()
        )

        job.referred_count = (
            referral_query
            .filter(
                Referral.status == "Referred"
            )
            .count()
        )


    referral_years = [int(y) for (y,) in (
        db.session.query(func.year(Referral.created_at))
        .join(Application, Referral.application_id == Application.application_id)
        .join(Job, Application.job_id == Job.job_id)
        .filter(Job.job_type == "Referral", Referral.created_at.isnot(None))
        .distinct().order_by(func.year(Referral.created_at).desc()).all()
    ) if y is not None]

    return render_template(
        "admin/admin_job_referral.html",
        referral_years=referral_years,
        jobs=jobs,
        today=date.today()
    )


@admin.route("/job_referral/export", methods=["GET"])
@login_required
@role_required("admin")
@limiter.limit("10 per hour", methods=["GET"])
def export_job_referrals():
    """Download year-filtered referral activity; year uses referral creation date (UTC)."""
    if not check_admin_access():
        abort(403)
    raw_year = (request.args.get("year") or "all").strip()
    if raw_year != "all" and (not re.fullmatch(r"[0-9]{4}", raw_year) or not 2000 <= int(raw_year) <= 2100):
        abort(400, description="Invalid referral year.")

    query = (db.session.query(Referral, Application, Job, JobSeeker, User, Employer)
             .join(Application, Referral.application_id == Application.application_id)
             .join(Job, Application.job_id == Job.job_id)
             .join(JobSeeker, Application.job_seeker_id == JobSeeker.job_seeker_id)
             .join(User, JobSeeker.user_id == User.user_id)
             .join(Employer, Job.employer_id == Employer.employer_id)
             .filter(Job.job_type == "Referral"))
    if raw_year != "all":
        year = int(raw_year)
        query = query.filter(Referral.created_at >= datetime(year, 1, 1),
                             Referral.created_at < datetime(year + 1, 1, 1))
    records = query.order_by(Referral.created_at.desc(), Referral.referral_id.desc()).all()
    if len(records) > 20000:
        abort(400, description="Too many records. Choose a specific year.")

    from collections import defaultdict
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    book = Workbook()
    overview = book.active
    overview.title = "Job Summary"
    detail = book.create_sheet("Referral Records")
    summary = defaultdict(lambda: {"pending": 0, "referred": 0, "not_referred": 0})
    for ref, app, job, seeker, user, employer in records:
        key = (job.job_id, job.job_title or "", employer.company_name or "", job.status or "", job.approval_status or "")
        state = (ref.status or "").lower()
        bucket = "referred" if state == "referred" else ("pending" if state == "pending" else "not_referred")
        summary[key][bucket] += 1

    overview.append(["PESO-IMS | Job Referral Report"])
    overview.append(["Referral creation year:", raw_year.upper() if raw_year == "all" else raw_year])
    overview.append(["Generated (Philippine time):", datetime.now(ZoneInfo("Asia/Manila")).strftime("%b %d, %Y %I:%M %p")])
    overview.append([])
    overview.append(["Job ID", "Job Title", "Company", "Job Status", "Approval", "Awaiting Review", "Referred", "Do Not Refer / Other", "Total Requests"])
    for (jid, title, company, status, approval), counts in sorted(summary.items(), key=lambda x: x[0][1].lower()):
        overview.append([jid, title, company, status, approval, counts["pending"], counts["referred"], counts["not_referred"], sum(counts.values())])

    detail.append(["PESO-IMS | Referral Records"])
    detail.append(["Referral creation year:", raw_year.upper() if raw_year == "all" else raw_year])
    detail.append([])
    detail.append(["Referral ID", "Created On (PHT)", "Job Title", "Company", "Job Status", "Applicant ID", "Applicant Name", "Referral Decision", "Reviewed On (PHT)"])
    def ph_time(value):
        if not value:
            return ""
        return value.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Manila")).strftime("%Y-%m-%d %I:%M %p")
    for ref, app, job, seeker, user, employer in records:
        detail.append([ref.referral_id, ph_time(ref.created_at), job.job_title or "", employer.company_name or "", job.status or "", user.public_id or "", " ".join(filter(None, [seeker.first_name, seeker.last_name])), "Do Not Refer" if ref.status == "Rejected" else ref.status or "", ph_time(ref.reviewed_at)])
    for sheet, header in [(overview, 5), (detail, 4)]:
        sheet.freeze_panes = f"A{header + 1}"
        sheet.auto_filter.ref = f"A{header}:{get_column_letter(sheet.max_column)}{max(header, sheet.max_row)}"
        for cell in sheet[header]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="123D82")
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        sheet.row_dimensions[header].height = 30
        for col in range(1, sheet.max_column + 1):
            column_letter = get_column_letter(col)
            width = min(42, max(14, max(len(str(sheet.cell(row, col).value or "")) for row in range(header, min(sheet.max_row, 250) + 1)) + 3))
            sheet.column_dimensions[column_letter].width = width
        for row in sheet.iter_rows(min_row=header + 1):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
    stream = BytesIO()
    book.save(stream)
    stream.seek(0)
    return send_file(stream, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name=f"PESO_Job_Referrals_{raw_year}_{datetime.now():%Y%m%d}.xlsx")


def _referral_employer_info(employer):
    """Use the registered employer account name; never invent a job title."""
    account = employer.user if employer else None
    registered_name = (getattr(account, "username", "") or "").strip()
    # Some accounts use their email address as username, which is not a person's name.
    if not registered_name or "@" in registered_name:
        registered_name = "Company Representative"
    return {
        "contact_person": registered_name,
        "designation": "",  # No verified job title exists in the Employer model.
        "company_name": employer.company_name if employer else "Company",
        "address": employer.address if employer else "",
    }


@admin.route("/referral/<int:application_id>", methods=["POST"])
@login_required
@role_required("admin")
def create_referral(application_id):
    if not check_admin_access():
        flash("Unauthorized access.", "danger")
        return redirect(url_for("admin.dashboard"))

    application = (
        db.session.query(Application)
        .filter(Application.application_id == application_id)
        .with_for_update()
        .first_or_404()
    )

    can_issue, reason, referral = validate_referral_for_issue(application)

    if not can_issue:
        db.session.rollback()
        flash(reason, "warning")
        return redirect(
            url_for("admin.job_rankings", job_id=application.job_id)
        )

    admin_profile = Admin.query.filter_by(
        user_id=current_user.user_id
    ).first()

    if not admin_profile:
        db.session.rollback()
        flash("Admin profile not found for this account.", "danger")
        return redirect(url_for("admin.job_referral"))

    try:
        referral.status = "Referred"
        referral.referred_by = current_user.admin.admin_id
        referral.reviewed_at = datetime.now(timezone.utc)
        referral.referral_date = datetime.now(timezone.utc)
        referral.remarks = "Referred by PESO Admin"

        mark_application_reviewed(application)
        record_audit(
            f"REFERRAL_ISSUED application_id={application.application_id} referral_id={referral.referral_id}"
        )

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(
            "Failed to refer application %s: %s",
            application.application_id,
            e,
        )
        flash("Unable to refer applicant. Please try again.", "danger")
        return redirect(
            url_for("admin.job_rankings", job_id=application.job_id)
        )

    try:
        jobseeker = application.job_seeker
        job = application.job
        employer = job.employer if job else None

        date_str = datetime.now().strftime("%B %d, %Y")
        employer_info = _referral_employer_info(employer)

        js_address_parts = [
            getattr(jobseeker, "street", ""),
            getattr(jobseeker, "barangay", ""),
            getattr(jobseeker, "city", ""),
            getattr(jobseeker, "province", ""),
        ]

        js_address = ", ".join(
            [p for p in js_address_parts if p]
        ) or "San Jose del Monte, Bulacan"

        jobseeker_info = {
            "first_name": jobseeker.first_name if jobseeker else "",
            "last_name": jobseeker.last_name if jobseeker else "",
            "degree": (
                getattr(jobseeker, "tertiary_course", None)
                or getattr(jobseeker, "educational_attainment", "Graduate")
            ),
            "address": js_address,
        }

        admin_fullname = (
            " ".join(
                part
                for part in [
                    getattr(admin_profile, "first_name", None),
                    getattr(admin_profile, "middle_name", None),
                    getattr(admin_profile, "last_name", None),
                    getattr(admin_profile, "suffix", None),
                ]
                if part
            )
            if admin_profile
            else "PESO ADMIN"
        )

        pdf_bytes = generate_referral_pdf(
            date_str=date_str,
            employer_info=employer_info,
            jobseeker_info=jobseeker_info,
            job_title=job.job_title if job else "Applicant",
            admin_name=admin_fullname,
        )

        js_full_name = f"{jobseeker.first_name} {jobseeker.last_name}"

        if jobseeker and jobseeker.user_id:
            notify_user(
                jobseeker.user_id,
                "Referral Created",
                f"Your application for {job.job_title} was referred. Check your email for your official referral letter.",
                "Referral",
            )
            commit_notifications("jobseeker referral-created notification")

            jobseeker_email = (
                jobseeker.user.email if jobseeker.user else None
            )

            if jobseeker_email:
                send_email(
                    recipient=jobseeker_email,
                    subject=f"PESO Official Referral Letter - {job.job_title}",
                    body=(
                        f"Dear {jobseeker.first_name},\n\n"
                        f"Great news! Your application for {job.job_title} at "
                        f"{employer.company_name} has been officially reviewed and referred "
                        f"by the PESO Office.\n\n"
                        f"Please find your attached official PESO Referral Letter PDF.\n\n"
                        f"Best regards,\n"
                        f"Public Employment Service Office (PESO)"
                    ),
                    attachment_pdf=pdf_bytes,
                    filename=f"PESO_Referral_Letter_{jobseeker.last_name}.pdf",
                )

        if employer:
            employer_user_id = getattr(employer, "user_id", None) or (
                employer.user.user_id
                if getattr(employer, "user", None)
                else None
            )

            if employer_user_id:
                notify_user(
                    employer_user_id,
                    "New Jobseeker Referral Received",
                    f"PESO has referred {js_full_name} for the position of {job.job_title}.",
                    "Referral",
                )
                commit_notifications("employer referral-received notification")

            employer_email = getattr(employer, "company_email", None) or (
                employer.user.email
                if getattr(employer, "user", None)
                else None
            )

            if employer_email:
                employer_body = (
                    f"Dear {employer_info['contact_person']},\n\n"
                    f"Warm Greetings!\n\n"
                    f"The Public Employment Service Office (PESO) respectfully endorses "
                    f"{js_full_name} for the position of {job.job_title} at "
                    f"{employer.company_name}.\n\n"
                    f"Please find attached the official Referral Letter PDF for your review and reference.\n\n"
                    f"Best regards,\n\n"
                    f"{admin_fullname}\n"
                    f"Public Employment Service Office (PESO)"
                )

                send_email(
                    recipient=employer_email,
                    subject=f"PESO Applicant Referral - {js_full_name} ({job.job_title})",
                    body=employer_body,
                    attachment_pdf=pdf_bytes,
                    filename=f"Referral_Letter_{jobseeker.last_name}_{referral.referral_id}.pdf",
                )

        flash(
            "Applicant referred successfully. Referral PDF letter emailed to both jobseeker and employer.",
            "success",
        )

    except Exception as e:
        current_app.logger.exception(
            "Referral created, but email/notification delivery failed: %s",
            e,
        )
        flash(
            f"Referral created, but email/notification delivery failed: {str(e)}",
            "warning",
        )

    return redirect(
        url_for("admin.job_rankings", job_id=application.job_id)
    )

@admin.route("/referral/<int:referral_id>/send-email", methods=["POST"])
@login_required
@role_required("admin")
def send_referral_email(referral_id):
    if not check_admin_access():
        flash("Unauthorized access.", "danger")
        return redirect(url_for("admin.dashboard"))

    referral = Referral.query.get_or_404(referral_id)
    application = referral.application

    if not application:
        flash("Application record not found for this referral.", "danger")
        return redirect(url_for("admin.job_referral"))

    employer = application.job.employer if application.job else None
    jobseeker = application.job_seeker
    job = application.job

    employer_email = getattr(employer, 'company_email', None) or (employer.user.email if getattr(employer, 'user', None) else None)
    if not employer or not employer_email:
        flash("Employer does not have a valid email address.", "warning")
        return redirect(url_for("admin.job_referral"))

    referring_admin = Admin.query.filter_by(admin_id=referral.referred_by).first()
    if referring_admin:
        name_parts = [
            getattr(referring_admin, "first_name", None),
            getattr(referring_admin, "middle_name", None),
            getattr(referring_admin, "last_name", None),
            getattr(referring_admin, "suffix", None),
        ]
        admin_name = " ".join(part for part in name_parts if part) or "PESO ADMIN"
    else:
        admin_name = "PESO ADMIN"

    employer_info = _referral_employer_info(employer)  # standardized

    js_address_parts = [getattr(jobseeker, 'street', ''), getattr(jobseeker, 'barangay', ''), getattr(jobseeker, 'city', ''), getattr(jobseeker, 'province', '')]
    js_address = ", ".join([p for p in js_address_parts if p]) or "San Jose del Monte, Bulacan"

    jobseeker_info = {
        'first_name': jobseeker.first_name,
        'last_name': jobseeker.last_name,
        'degree': getattr(jobseeker, 'tertiary_course', None) or getattr(jobseeker, 'educational_attainment', 'Graduate'),
        'address': js_address
    }

    date_today = datetime.now().strftime("%B %d, %Y")
    
    pdf_bytes = generate_referral_pdf(
        date_str=date_today,
        employer_info=employer_info,
        jobseeker_info=jobseeker_info,
        job_title=job.job_title,
        admin_name=admin_name
    )

    js_full_name = f"{jobseeker.first_name} {jobseeker.last_name}"
    
    # Notify employer inside the website
    employer_user_id = getattr(employer, 'user_id', None) or (employer.user.user_id if getattr(employer, 'user', None) else None)
    if employer_user_id:
        notify_user(
            employer_user_id,
            "New Jobseeker Referral Received",
            f"PESO has referred {js_full_name} for the position of {job.job_title}.",
            "Referral",
        )
        commit_notifications("referral resend employer notification")

    # Email Employer
    email_body = (
        f"Dear {employer_info['contact_person']},\n\n"
        f"Warm Greetings!\n\n"
        f"The Public Employment Service Office (PESO) respectfully endorses {js_full_name} "
        f"for the position of {job.job_title} at {employer.company_name}.\n\n"
        f"Please find attached the official Referral Letter PDF for your review and reference.\n\n"
        f"Best regards,\n\n"
        f"{admin_name}\n"
        f"Public Employment Service Office (PESO)"
    )

    try:
        send_email(
            recipient=employer_email,
            subject=f"PESO Applicant Referral - {js_full_name} ({job.job_title})",
            body=email_body,
            attachment_pdf=pdf_bytes,
            filename=f"Referral_Letter_{jobseeker.last_name}_{referral.referral_id}.pdf"
        )
        flash(f"Referral email and website notification sent successfully to {employer.company_name}.", "success")
    except Exception as e:
        flash(f"Failed to send email: {str(e)}", "danger")

    return redirect(url_for("admin.job_referral"))


@admin.route("/referral/<int:referral_id>/pdf", methods=["GET"])
@login_required
@role_required("admin")
def download_referral_letter(referral_id):
    referral = Referral.query.get_or_404(referral_id)
    application = referral.application

    if not application:
        flash("Application record not found for this referral.", "danger")
        return redirect(url_for("admin.referrals_list"))

    employer = application.job.employer
    jobseeker = application.job_seeker
    job = application.job

    referring_admin = (
        Admin.query.filter_by(admin_id=referral.referred_by).first()
    )

    admin_name = (
        " ".join(
            part
            for part in [
                getattr(referring_admin, "first_name", None),
                getattr(referring_admin, "middle_name", None),
                getattr(referring_admin, "last_name", None),
                getattr(referring_admin, "suffix", None),
            ]
            if part
        )
        if referring_admin
        else "PESO ADMIN"
    )

    employer_info = _referral_employer_info(employer)  # standardized

    js_address_parts = [jobseeker.street, jobseeker.barangay, jobseeker.city, jobseeker.province]
    js_address = ", ".join([p for p in js_address_parts if p]) or "San Jose del Monte, Bulacan"

    jobseeker_info = {
        'first_name': jobseeker.first_name,
        'last_name': jobseeker.last_name,
        'degree': jobseeker.tertiary_course or jobseeker.education_display,
        'address': js_address
    }

    date_today = datetime.now().strftime("%B %d, %Y")
    
    # Pass admin_name to the PDF generator
    pdf_bytes = generate_referral_pdf(
        date_str=date_today,
        employer_info=employer_info,
        jobseeker_info=jobseeker_info,
        job_title=job.job_title,
        admin_name=admin_name
    )

    filename = f"Referral_Letter_{jobseeker.last_name}_{referral.referral_id}.pdf"

    return send_file(
        BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename
    )
    

@admin.route("/referral/reject/<int:application_id>", methods=["POST"])
@login_required
@role_required("admin")
def reject_referral(application_id):
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    # Serialize competing Issue/Reject requests for this application.
    # InnoDB holds this lock until the transaction commits or rolls back.
    application = (
        db.session.query(Application)
        .filter(Application.application_id == application_id)
        .with_for_update()
        .first_or_404()
    )
    redirect_url = url_for("admin.job_rankings", job_id=application.job_id)

    can_reject, reason, referral = (
        validate_referral_for_rejection(
            application
        )
    )

    if not can_reject:
        db.session.rollback()
        flash(reason, "warning")
        return redirect(redirect_url)

    now = datetime.now(timezone.utc)
    remarks = (request.form.get("remarks") or "").strip() or "Referral request rejected by PESO Admin."
    if len(remarks) > 2000:
        db.session.rollback()
        flash("Referral remarks cannot exceed 2000 characters.", "danger")
        return redirect(redirect_url)

    try:
        referral.status = "Rejected"
        referral.referred_by = current_user.admin.admin_id
        referral.reviewed_at = now
        referral.remarks = remarks
        record_audit(
            f"REFERRAL_REJECTED application_id={application.application_id} referral_id={referral.referral_id}"
        )

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("Unable to reject referral for application %s: %s", application.application_id, e)
        flash("Unable to reject the referral request.", "danger")
        return redirect(redirect_url)

    # In-App Notification
    jobseeker = application.job_seeker
    job = application.job
    job_title = job.job_title if job else "the selected job"

    if jobseeker and jobseeker.user_id:
        notify_user(
            jobseeker.user_id,
            "Referral Request Rejected",
            f"Your referral request for {job_title} was not approved by PESO.",
            "Referral",
        )
        commit_notifications("referral rejection jobseeker notification")

    # Email
    jobseeker_email = jobseeker.user.email if (jobseeker and jobseeker.user) else None

    if jobseeker_email:
        email_body = f"""Dear {jobseeker.first_name},

Your PESO referral request for the position of {job_title} was reviewed and was not approved.

Remarks:
{remarks}

Please note that this decision applies only to the PESO referral request and does not automatically mean that your overall job application has been rejected.

You may log in to PESO-IMS for more information.

Best regards,
Public Employment Service Office (PESO)"""

        try:
            send_email(
                recipient=jobseeker_email,
                subject=f"PESO Referral Request Update - {job_title}",
                body=email_body,
            )
            flash("Referral request rejected successfully. The job seeker was notified by email.", "success")
        except Exception as e:
            current_app.logger.exception("Referral rejection email failed for application %s: %s", application.application_id, e)
            flash("Referral request was rejected, but the email notification could not be sent.", "warning")
    else:
        current_app.logger.warning("Referral rejection email skipped for application %s because the job seeker has no email address.", application.application_id)
        flash("Referral request rejected successfully, but the job seeker has no email address.", "warning")

    return redirect(redirect_url)



@admin.route("/application/<int:application_id>/status", methods=["POST"])
@login_required
@role_required("admin")
def update_application_status(application_id):
    """
    PESO Admin may mark a pending application as Reviewed.

    Employer-owned hiring states (Interview, Accepted, Rejected)
    must be changed through the employer workflow so interview,
    source, and vacancy rules cannot be bypassed.
    """
    if not check_admin_access():
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        }), 403

    application = Application.query.get_or_404(
        application_id
    )

    data = request.get_json(silent=True) if request.is_json else request.form
    if not hasattr(data, "get"):
        return jsonify({"success": False, "message": "Invalid request data."}), 400
    status = data.get("status")
    if not isinstance(status, str):
        return jsonify({"success": False, "message": "Invalid status value."}), 400
    status = status.strip()

    if status != "Reviewed":
        return jsonify({
            "success": False,
            "message": (
                "PESO Admin may only mark an application as Reviewed. "
                "Interview, Accepted, and Rejected are controlled by "
                "the employer workflow."
            ),
        }), 400

    if application.status == "Reviewed":
        return jsonify({
            "success": True,
            "status": "Reviewed",
            "message": "Application is already reviewed.",
        })

    if application.status != "Pending":
        return jsonify({
            "success": False,
            "message": (
                f"Application cannot be changed from "
                f"{application.status} to Reviewed."
            ),
        }), 409

    mark_application_reviewed(
        application
    )
    record_audit(
        f"APPLICATION_REVIEWED application_id={application.application_id} job_id={application.job_id}"
    )

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(
            "Unable to mark application %s as reviewed: %s",
            application_id,
            e,
        )
        return jsonify({
            "success": False,
            "message": "Unable to update application status."
        }), 500

    jobseeker = application.job_seeker
    job = application.job
    job_title = (
        job.job_title
        if job
        else "the selected job"
    )

    if jobseeker and jobseeker.user_id:
        notify_user(
            jobseeker.user_id,
            "Application Reviewed",
            (
                f"Your application to {job_title} "
                "was marked reviewed by PESO."
            ),
            "Application",
        )
        commit_notifications("application reviewed jobseeker notification")

    return jsonify({
        "success": True,
        "status": "Reviewed",
        "message": "Application marked as reviewed.",
    })



# ==========================================
# JOBSEEKERS & APPLICANTS
# ==========================================


@admin.route("/jobseekers")
@login_required
@role_required("admin")
def jobseekers():
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    job_seekers = JobSeeker.query.all()
    return render_template(
        "admin/admin_jobseekerlist.html", job_seekers=job_seekers
    )


@admin.route("/jobseeker/<string:public_id>")
@login_required
@role_required("admin")
def jobseeker_profile(public_id):
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    seeker = (JobSeeker.query.join(User).filter(User.public_id == public_id).first_or_404())

    # Repair profiles hired before automatic employment-status syncing.
    if sync_jobseeker_employment_status(seeker):
        db.session.commit()

    # Fetch applications directly
    applications = (
        Application.query.filter_by(job_seeker_id=seeker.job_seeker_id)
        .order_by(Application.application_date.desc())
        .all()
    )

    formatted_applications = []
    for app in applications:
        # ---------------------------------------------------------
        # DETERMINE APPLICATION SOURCE
        # ---------------------------------------------------------
        is_job_fair_direct = getattr(app, "job_fair_evaluation", None) is not None
        referrals = getattr(app, "referrals", None)
        referral = referrals[0] if referrals else None

        if is_job_fair_direct:
            application_source = "Direct"
        elif referral:
            status_map = {
                "Referred": "Referred",
                "Pending": "Referral (Admin)",
                "Rejected": "Rejected",
            }
            application_source = status_map.get(referral.status, "Referral")
        else:
            application_source = "Referral"

        formatted_applications.append({
            "application_id": app.application_id,
            "job_id": app.job_id,
            "job_title": app.job.job_title if app.job else "N/A",
            "company_name": app.job.employer.company_name if (app.job and app.job.employer) else "N/A",
            "status": app.status,
            "source": application_source,
            "applied_on": app.application_date.strftime("%B %d, %Y") if getattr(app, "application_date", None) else "N/A"
        })

    referrals = (
        Referral.query.join(Application, Referral.application_id == Application.application_id)
        .filter(Application.job_seeker_id == seeker.job_seeker_id)
        .order_by(Referral.created_at.desc())
        .all()
    )

    return render_template(
        "admin/admin_jobseeker_profile.html",
        seeker=seeker,
        referrals=referrals,
        applications=formatted_applications,
        format_work_date=format_work_date
    )


# ==========================================
# JOB FAIR MANAGEMENT
# ==========================================


@admin.route("/job-fairs", methods=["GET"])
@login_required
@role_required("admin")
def job_fairs():
    if not check_admin_access():
        flash("Unauthorized access.", "danger")
        return redirect(url_for("admin.dashboard"))

    fairs = JobFair.query.order_by(JobFair.start_datetime.desc()).all()
    registered_agencies = (
        Employer.query.filter_by(verified="approved")
        .order_by(Employer.company_name.asc())
        .all()
    )

    total_fairs = len(fairs)

    # Headline cards use unique people/organizations so repeat participation
    # across several fairs does not inflate the overall totals.
    participating_employers_count = (
        db.session.query(func.count(func.distinct(JobFairEmployerRegistration.employer_id)))
        .filter(JobFairEmployerRegistration.status == "Approved")
        .scalar()
        or 0
    )
    registered_jobseekers_count = (
        db.session.query(func.count(func.distinct(JobFairRegistration.job_seeker_id)))
        .scalar()
        or 0
    )
    total_hired_count = (
        db.session.query(func.count(func.distinct(JobFairRegistration.job_seeker_id)))
        .join(
            JobFairCandidateEvaluation,
            JobFairCandidateEvaluation.job_fair_registration_id == JobFairRegistration.registration_id,
        )
        .filter(func.lower(JobFairCandidateEvaluation.status) == "hired")
        .scalar()
        or 0
    )

    # Years are based on fairs that have started; future fairs are not historical data.
    occurred_fairs = [fair for fair in fairs if fair.start_datetime and fair.start_datetime <= datetime.now()]
    metric_years = sorted({fair.start_datetime.year for fair in occurred_fairs if fair.start_datetime}, reverse=True)
    if not metric_years:
        metric_years = [datetime.now().year]

    return render_template(
        "admin/admin_job_fair_management.html",
        job_fairs=fairs,
        registered_agencies=registered_agencies,
        total_fairs=total_fairs,
        participating_employers_count=int(participating_employers_count),
        registered_jobseekers_count=int(registered_jobseekers_count),
        total_hired_count=int(total_hired_count),
        metric_years=metric_years,
    )


@admin.route("/job-fairs/metrics", methods=["GET"])
@login_required
@role_required("admin")
def job_fair_metrics():
    if not check_admin_access():
        return jsonify({"error": "Unauthorized access."}), 403

    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Manila")).replace(tzinfo=None)
    try:
        year = int(request.args.get("year", now.year))
        period = int(request.args.get("period", 3))
        end_month = int(request.args.get("end_month", 12))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid analytics filters."}), 400

    if period not in (3, 6, 12) or not 1 <= end_month <= 12 or not 2000 <= year <= 2100:
        return jsonify({"error": "Invalid analytics filters."}), 400

    if period == 12:
        start_month, end_month = 1, 12
    else:
        start_month = max(1, end_month - period + 1)

    window_start = datetime(year, start_month, 1)
    window_end = datetime(year + 1, 1, 1) if end_month == 12 else datetime(year, end_month + 1, 1)

    # Historical analytics include fairs that have started. Future/upcoming fairs
    # never inflate performance data, while an ongoing fair may contribute data
    # already recorded during the event.
    fairs = (
        JobFair.query
        .filter(
            JobFair.start_datetime >= window_start,
            JobFair.start_datetime < window_end,
            JobFair.start_datetime <= now,
        )
        .order_by(JobFair.start_datetime.asc())
        .all()
    )

    fairs_by_month = {month: [] for month in range(start_month, end_month + 1)}
    for fair in fairs:
        fairs_by_month[fair.start_datetime.month].append(fair)

    labels = []
    fair_counts = []
    fair_names = []
    registration_counts = []
    attendance_counts = []
    employer_counts = []
    hired_counts = []

    for month in range(start_month, end_month + 1):
        labels.append(datetime(year, month, 1).strftime("%b"))
        month_fairs = fairs_by_month[month]
        fair_ids = [fair.job_fair_id for fair in month_fairs]
        fair_counts.append(len(fair_ids))
        fair_names.append([fair.title for fair in month_fairs])

        if not fair_ids:
            registration_counts.append(0)
            attendance_counts.append(0)
            employer_counts.append(0)
            hired_counts.append(0)
            continue

        registered = (
            db.session.query(func.count(func.distinct(JobFairRegistration.job_seeker_id)))
            .filter(JobFairRegistration.job_fair_id.in_(fair_ids))
            .scalar()
            or 0
        )
        attended = (
            db.session.query(func.count(func.distinct(JobFairRegistration.job_seeker_id)))
            .filter(
                JobFairRegistration.job_fair_id.in_(fair_ids),
                JobFairRegistration.attendance_status == "Attended",
            )
            .scalar()
            or 0
        )
        employers = (
            db.session.query(func.count(func.distinct(JobFairEmployerRegistration.employer_id)))
            .filter(
                JobFairEmployerRegistration.job_fair_id.in_(fair_ids),
                JobFairEmployerRegistration.status == "Approved",
            )
            .scalar()
            or 0
        )
        hired = (
            db.session.query(func.count(func.distinct(JobFairRegistration.job_seeker_id)))
            .join(
                JobFairCandidateEvaluation,
                JobFairCandidateEvaluation.job_fair_registration_id == JobFairRegistration.registration_id,
            )
            .filter(
                JobFairCandidateEvaluation.job_fair_id.in_(fair_ids),
                func.lower(JobFairCandidateEvaluation.status) == "hired",
            )
            .scalar()
            or 0
        )

        registration_counts.append(int(registered))
        attendance_counts.append(int(attended))
        employer_counts.append(int(employers))
        hired_counts.append(int(hired))

    return jsonify({
        "year": year,
        "period": period,
        "start_month": start_month,
        "end_month": end_month,
        "labels": labels,
        "job_fairs": fair_counts,
        "fair_names": fair_names,
        "registered": registration_counts,
        "attended": attendance_counts,
        "employers": employer_counts,
        "hired": hired_counts,
    })


@admin.route("/job-fair/create", methods=["POST"])
@login_required
@role_required("admin")
def create_job_fair():
    if not check_admin_access():
        flash("Unauthorized access.", "danger")
        return redirect(url_for("admin.dashboard"))

    allowed_event_types = {
        "Mega Job Fair",
        "BSRA",
        "SRA Overseas",
        "SRA Local",
    }

    try:
        title = request.form.get("title", "").strip()
        venue = request.form.get("venue", "").strip()
        description = request.form.get("description", "").strip()
        event_type = request.form.get("event_type", "Mega Job Fair").strip()

        if not title:
            flash("Event title is required.", "danger")
            return redirect(url_for("admin.job_fairs"))
        if len(title) > 150:
            flash("Event title must not exceed 150 characters.", "danger")
            return redirect(url_for("admin.job_fairs"))
        if not venue:
            flash("Venue is required.", "danger")
            return redirect(url_for("admin.job_fairs"))
        if len(venue) > 200:
            flash("Venue must not exceed 200 characters.", "danger")
            return redirect(url_for("admin.job_fairs"))
        if len(description) > 10000:
            flash("Vacancies and instructions must not exceed 10,000 characters.", "danger")
            return redirect(url_for("admin.job_fairs"))
        if event_type not in allowed_event_types:
            flash("Invalid event classification selected.", "danger")
            return redirect(url_for("admin.job_fairs"))

        capacity_raw = request.form.get("capacity", "").strip()
        try:
            capacity = int(capacity_raw)
        except (TypeError, ValueError):
            flash("Maximum capacity must be a whole number.", "danger")
            return redirect(url_for("admin.job_fairs"))
        if capacity < 1 or capacity > 100000:
            flash("Maximum capacity must be between 1 and 100,000.", "danger")
            return redirect(url_for("admin.job_fairs"))

        lead_agency_id = None
        lead_agency_val = request.form.get("lead_agency_id", "").strip()
        if lead_agency_val:
            lead_agency = (
                Employer.query.join(User, Employer.user_id == User.user_id)
                .filter(
                    User.public_id == lead_agency_val,
                    Employer.verified == "approved",
                )
                .first()
            )
            if lead_agency:
                lead_agency_id = lead_agency.employer_id
            if not lead_agency:
                flash("The selected lead recruitment agency is not available or approved.", "danger")
                return redirect(url_for("admin.job_fairs"))

        start_datetime_str = request.form.get("start_datetime", "").strip()
        end_datetime_str = request.form.get("end_datetime", "").strip()
        reg_deadline_str = request.form.get("registration_deadline", "").strip()
        queue_deadline_str = request.form.get("queue_entry_deadline", "").strip()

        if not start_datetime_str or not end_datetime_str or not reg_deadline_str or not queue_deadline_str:
            flash("Start date, end date, registration cutoff, and queue entry cutoff are required.", "danger")
            return redirect(url_for("admin.job_fairs"))

        try:
            start_datetime = datetime.strptime(start_datetime_str, "%Y-%m-%dT%H:%M")
            end_datetime = datetime.strptime(end_datetime_str, "%Y-%m-%dT%H:%M")
            registration_deadline = datetime.strptime(reg_deadline_str, "%Y-%m-%dT%H:%M")
            queue_entry_deadline = datetime.strptime(queue_deadline_str, "%Y-%m-%dT%H:%M")
        except ValueError:
            flash("One or more event dates are invalid. Please select valid dates and times.", "danger")
            return redirect(url_for("admin.job_fairs"))

        now = datetime.now()
        if start_datetime <= now:
            flash("Job Fair start date and time must be in the future.", "danger")
            return redirect(url_for("admin.job_fairs"))
        if end_datetime <= start_datetime:
            flash("Job Fair end date and time must be after the start date and time.", "danger")
            return redirect(url_for("admin.job_fairs"))
        if registration_deadline < now:
            flash("Registration deadline cannot be in the past.", "danger")
            return redirect(url_for("admin.job_fairs"))
        if queue_entry_deadline < start_datetime:
            flash("Queue entry cutoff cannot be before the Job Fair starts.", "danger")
            return redirect(url_for("admin.job_fairs"))
        if registration_deadline > queue_entry_deadline:
            flash("Registration cutoff cannot be after the queue entry cutoff.", "danger")
            return redirect(url_for("admin.job_fairs"))
        if queue_entry_deadline > end_datetime:
            flash("Queue entry cutoff cannot be after the Job Fair end time.", "danger")
            return redirect(url_for("admin.job_fairs"))

        enforce_dress_code = "enforce_dress_code" in request.form
        require_gov_ids = "require_gov_ids" in request.form
        require_srs_id = "require_srs_id" in request.form

        existing_fair = JobFair.query.filter(
            func.lower(JobFair.title) == title.lower(),
            JobFair.start_datetime == start_datetime,
        ).first()
        if existing_fair:
            flash(
                "A job fair with the same title and start date/time already exists.",
                "warning",
            )
            return redirect(url_for("admin.job_fairs"))

        poster_filename = None
        poster = request.files.get("poster")
        if poster and poster.filename:
            upload_folder = os.path.join(
                current_app.root_path,
                "static",
                "uploads",
                "posters",
            )
            try:
                poster_filename = save_job_fair_banner(poster, upload_folder)
            except ValueError as error:
                flash(str(error), "danger")
                return redirect(url_for("admin.job_fairs"))

        new_fair = JobFair(
            title=title,
            event_type=event_type,
            lead_agency_id=lead_agency_id,
            venue=venue,
            description=description,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
            registration_deadline=registration_deadline,
            queue_entry_deadline=queue_entry_deadline,
            registration_manually_closed=False,
            queue_entry_manually_closed=False,
            capacity=capacity,
            poster_image=poster_filename,
            enforce_dress_code=enforce_dress_code,
            require_gov_ids=require_gov_ids,
            require_srs_id=require_srs_id,
            status="Upcoming",
        )
        db.session.add(new_fair)
        db.session.flush()
        record_audit(
            f"JOB_FAIR_CREATED job_fair_id={new_fair.job_fair_id} title={new_fair.title}"
        )
        db.session.commit()

        job_fair_date = start_datetime.strftime("%B %d, %Y at %I:%M %p")
        job_fair_venue = venue
        notification_title = f"New Job Fair: {title}"
        notification_message = (
            f"A new job fair event '{title}' has been published! "
            f"Date: {job_fair_date}, Venue: {job_fair_venue}. "
            f"Click to register or learn more."
        )
        notify_role(
            "jobseeker",
            notification_title,
            notification_message,
            "Job Fair",
        )
        notify_role(
            "employer",
            notification_title,
            notification_message,
            "Job Fair",
        )
        commit_notifications("new job fair website notifications")

        email_subject = f"New Job Fair Published: {title}"
        email_message = (
            "Hello,\n\n"
            "A new job fair event has been published!\n\n"
            f"Event: {title}\n"
            f"Type: {event_type}\n"
            f"Date: {job_fair_date}\n"
            f"Venue: {job_fair_venue}\n"
            f"Description: {description or 'N/A'}\n"
            f"Registration Deadline: {registration_deadline.strftime('%B %d, %Y at %I:%M %p')}\n\n"
            "Visit the PESO IMS system to register or view more details.\n\n"
            "Best regards,\nPESO-IMS Team"
        )
        recipient_rows = (
            db.session.query(User.email)
            .filter(
                User.role.in_(["jobseeker", "employer"]),
                User.is_deleted.is_(False),
                User.status == "active",
            )
            .all()
        )
        recipients = [email for (email,) in recipient_rows if email]
        send_bulk_email_async(
            current_app._get_current_object(),
            recipients,
            email_subject,
            email_message,
        )

        flash("PESO Event created and published successfully!", "success")
    except Exception as error:
        db.session.rollback()
        current_app.logger.exception("Failed to create Job Fair: %s", error)
        flash("Failed to create event. Please review the details and try again.", "danger")

    return redirect(url_for("admin.job_fairs"))


@admin.route("/job-fair/<int:fair_id>/registration/<action>", methods=["POST"])
@login_required
@role_required("admin")
def set_job_fair_registration_availability(fair_id, action):
    if not check_admin_access():
        abort(403)

    fair = JobFair.query.get_or_404(fair_id)
    now = datetime.now()

    if action == "close":
        fair.registration_manually_closed = True
        audit_action = "JOB_FAIR_REGISTRATION_CLOSED"
        message = "Job Fair registration has been closed."
    elif action == "reopen":
        if fair.status == "Finished" or (fair.registration_deadline and now > fair.registration_deadline) or (fair.end_datetime and now >= fair.end_datetime):
            flash("Registration cannot be reopened because its scheduled availability has already ended.", "warning")
            return redirect(url_for("admin.edit_job_fair", fair_id=fair_id))
        fair.registration_manually_closed = False
        audit_action = "JOB_FAIR_REGISTRATION_REOPENED"
        message = "Job Fair registration has been reopened."
    else:
        abort(404)

    record_audit(f"{audit_action} job_fair_id={fair.job_fair_id} title={fair.title}")
    db.session.commit()
    flash(message, "success")
    return redirect(url_for("admin.edit_job_fair", fair_id=fair_id))


@admin.route("/job-fair/<int:fair_id>/queue-entry/<action>", methods=["POST"])
@login_required
@role_required("admin")
def set_job_fair_queue_entry_availability(fair_id, action):
    if not check_admin_access():
        abort(403)

    fair = JobFair.query.get_or_404(fair_id)
    now = datetime.now()

    if action == "close":
        fair.queue_entry_manually_closed = True
        audit_action = "JOB_FAIR_QUEUE_ENTRY_CLOSED"
        message = "Job Fair queue entry has been closed. Existing queues can continue processing."
    elif action == "reopen":
        if fair.status != "Ongoing" or (fair.queue_entry_deadline and now > fair.queue_entry_deadline) or (fair.end_datetime and now >= fair.end_datetime):
            flash("Queue entry cannot be reopened because its scheduled availability has already ended or the fair is not ongoing.", "warning")
            return redirect(url_for("admin.edit_job_fair", fair_id=fair_id))
        fair.queue_entry_manually_closed = False
        audit_action = "JOB_FAIR_QUEUE_ENTRY_REOPENED"
        message = "Job Fair queue entry has been reopened."
    else:
        abort(404)

    record_audit(f"{audit_action} job_fair_id={fair.job_fair_id} title={fair.title}")
    db.session.commit()
    flash(message, "success")
    return redirect(url_for("admin.edit_job_fair", fair_id=fair_id))


@admin.route("/job-fair/<int:fair_id>/finish", methods=["POST"])
@login_required
@role_required("admin")
def finish_job_fair(fair_id):
    if not check_admin_access():
        abort(403)

    fair = JobFair.query.get_or_404(fair_id)
    if fair.status == "Finished":
        flash("This Job Fair is already finished.", "info")
        return redirect(url_for("admin.edit_job_fair", fair_id=fair_id))

    unresolved = JobFairQueue.query.filter(
        JobFairQueue.job_fair_id == fair.job_fair_id,
        JobFairQueue.status.in_(["Active", "Dormant"]),
    ).all()

    serving_count = JobFairQueue.query.filter(
        JobFairQueue.job_fair_id == fair.job_fair_id,
        JobFairQueue.status == "Active",
        JobFairQueue.assigned_station_id.isnot(None),
    ).count()
    if serving_count:
        flash(f"Cannot finish this Job Fair while {serving_count} applicant(s) are currently being served at interview stations.", "danger")
        return redirect(url_for("admin.edit_job_fair", fair_id=fair_id))

    confirmed = request.form.get("confirm_unresolved") == "1"
    if unresolved and not confirmed:
        flash(
            f"{len(unresolved)} unresolved queue(s) remain. Confirm Finish Job Fair again to cancel them as Event Ended. Make sure no interview is currently in progress.",
            "warning",
        )
        return redirect(url_for("admin.edit_job_fair", fair_id=fair_id, confirm_finish=1))

    cancelled_count = 0
    for queue in unresolved:
        queue.status = "Cancelled"
        queue.cancellation_reason = "Event Ended"
        cancelled_count += 1

    fair.registration_manually_closed = True
    fair.queue_entry_manually_closed = True
    fair.status = "Finished"
    close_exclusive_jobs_for_fair(fair, reason="Job Fair finished.")
    record_audit(
        f"JOB_FAIR_FINISHED job_fair_id={fair.job_fair_id} title={fair.title} "
        f"event_ended_queues={cancelled_count}"
    )
    db.session.commit()

    notification_title = f"Job Fair Completed: {fair.title}"
    notification_message = (
        f"The job fair '{fair.title}' has been completed. Thank you for attending!"
    )
    notify_role("jobseeker", notification_title, notification_message, "Job Fair")
    notify_role("employer", notification_title, notification_message, "Job Fair")
    commit_notifications("job fair formal completion notifications")

    recipients = [
        row.email
        for row in User.query.filter(
            User.role.in_(["jobseeker", "employer"]),
            User.is_deleted.is_(False),
            User.status == "active",
        ).all()
        if row.email
    ]
    send_bulk_email_async(
        current_app._get_current_object(),
        recipients,
        notification_title,
        f"Hello,\n\nThe job fair '{fair.title}' has been completed.\n\n"
        "Thank you for your participation!\n\nBest regards,\nPESO-IMS Team",
    )

    flash(
        f"Job Fair finished successfully. {cancelled_count} unresolved queue(s) were cancelled as Event Ended.",
        "success",
    )
    return redirect(url_for("admin.edit_job_fair", fair_id=fair_id))


@admin.route("/job-fair/<int:fair_id>/supervised-registration", methods=["GET", "POST"])
@login_required
@role_required("admin")
def supervised_registration_station(fair_id):
    """Staff-only station authorization. Never exposes an admin session to applicants."""
    fair = JobFair.query.get_or_404(fair_id)
    station_url = None
    if request.method == "POST":
        # Station authorization is an event-day operation, unlike ordinary
        # web pre-registration, which may open before the event begins.
        from website.utils.job_fair_lifecycle import _local_now_naive
        if fair.start_datetime and _local_now_naive() < fair.start_datetime:
            flash("This Job Fair has not started yet. Supervised registration will be available when the event begins.", "warning")
            return redirect(url_for("admin.supervised_registration_station", fair_id=fair_id))
        if not is_registration_open(fair):
            flash("Job Fair registration is not currently open.", "warning")
            return redirect(url_for("admin.supervised_registration_station", fair_id=fair_id))
        token = secrets.token_urlsafe(32)
        station = SupervisedRegistrationSession(
            token_hash=__import__("hashlib").sha256(token.encode()).hexdigest(),
            authorized_by_user_id=current_user.user_id,
            job_fair_id=fair_id,
            expires_at=datetime.utcnow() + timedelta(minutes=15),
        )
        db.session.add(station)
        db.session.commit()
        station_url = url_for("supervised_registration.open_station", token=token, _external=True)
    return render_template("admin/supervised_registration_station.html", fair=fair, station_url=station_url)


@admin.route("/job-fair/<int:fair_id>")
@login_required
@role_required("admin")
def view_job_fair(fair_id):
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    job_fair = JobFair.query.get_or_404(fair_id)
    employer_apps = JobFairEmployerRegistration.query.filter_by(
        job_fair_id=fair_id
    ).all()
    jobseeker_apps = JobFairRegistration.query.filter_by(
        job_fair_id=fair_id
    ).all()

    return render_template(
        "admin/admin_job_fair_detail.html",
        job_fair=job_fair,
        employer_apps=employer_apps,
        jobseeker_apps=jobseeker_apps,
    )


@admin.route("/job-fair/edit/<int:fair_id>", methods=["GET", "POST"])
@login_required
@role_required("admin")
def edit_job_fair(fair_id):
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    job_fair = JobFair.query.get_or_404(fair_id)
    registered_agencies = (
        Employer.query.filter_by(verified="approved")
        .order_by(Employer.company_name.asc())
        .all()
    )

    if request.method == "POST":
        old_poster_path = None
        new_poster_path = None

        try:
            allowed_event_types = {
                "Mega Job Fair",
                "BSRA",
                "SRA Overseas",
                "SRA Local",
            }
            allowed_statuses = {"Upcoming", "Ongoing", "Finished"}
            allowed_transitions = {
                "Upcoming": {"Upcoming", "Ongoing"},
                "Ongoing": {"Ongoing"},
                "Finished": {"Finished"},
            }

            old_status = job_fair.status
            now = datetime.now()

            title = request.form.get("title", "").strip()
            venue = request.form.get("venue", "").strip()
            description = request.form.get("description", "").strip()
            event_type = request.form.get("event_type", "").strip()
            status_val = request.form.get("status", "").strip()

            if not title:
                raise ValueError("Event title is required.")
            if len(title) > 150:
                raise ValueError("Event title must not exceed 150 characters.")
            if not venue:
                raise ValueError("Venue is required.")
            if len(venue) > 200:
                raise ValueError("Venue must not exceed 200 characters.")
            if len(description) > 10000:
                raise ValueError("Vacancies and instructions must not exceed 10,000 characters.")
            if event_type not in allowed_event_types:
                raise ValueError("Invalid event classification selected.")
            if status_val not in allowed_statuses:
                raise ValueError("Invalid event status selected.")
            if status_val not in allowed_transitions.get(old_status, {old_status}):
                raise ValueError(
                    f"A {old_status.lower()} Job Fair cannot be changed back to {status_val}."
                )

            capacity_raw = request.form.get("capacity", "").strip()
            try:
                capacity = int(capacity_raw)
            except (TypeError, ValueError):
                raise ValueError("Maximum capacity must be a whole number.")
            if capacity < 1 or capacity > 100000:
                raise ValueError("Maximum capacity must be between 1 and 100,000.")

            lead_agency_id = None
            lead_agency_val = request.form.get("lead_agency_id", "").strip()
            if lead_agency_val:
                lead_agency = (Employer.query.join(User, Employer.user_id == User.user_id).filter(
                    User.public_id == lead_agency_val,
                    Employer.verified == "approved",
                ).first())
                if lead_agency:
                    lead_agency_id = lead_agency.employer_id
                if not lead_agency:
                    raise ValueError(
                        "The selected lead recruitment agency is not available or approved."
                    )

            start_datetime_str = request.form.get("start_datetime", "").strip()
            end_datetime_str = request.form.get("end_datetime", "").strip()
            reg_deadline_str = request.form.get("registration_deadline", "").strip()
            queue_deadline_str = request.form.get("queue_entry_deadline", "").strip()
            if not start_datetime_str or not end_datetime_str or not reg_deadline_str or not queue_deadline_str:
                raise ValueError(
                    "Start date, end date, registration cutoff, and queue entry cutoff are required."
                )

            try:
                start_datetime = datetime.strptime(start_datetime_str, "%Y-%m-%dT%H:%M")
                end_datetime = datetime.strptime(end_datetime_str, "%Y-%m-%dT%H:%M")
                registration_deadline = datetime.strptime(reg_deadline_str, "%Y-%m-%dT%H:%M")
                queue_entry_deadline = datetime.strptime(queue_deadline_str, "%Y-%m-%dT%H:%M")
            except ValueError:
                raise ValueError(
                    "One or more event dates are invalid. Please select valid dates and times."
                )

            if end_datetime <= start_datetime:
                raise ValueError("End date and time must be after the Job Fair start date and time.")
            if queue_entry_deadline < start_datetime:
                raise ValueError("Queue entry cutoff cannot be before the Job Fair starts.")
            if registration_deadline > queue_entry_deadline:
                raise ValueError("Registration cutoff cannot be after the queue entry cutoff.")
            if queue_entry_deadline > end_datetime:
                raise ValueError("Queue entry cutoff cannot be after the Job Fair end time.")

            # Upcoming fairs may be rescheduled, but they still cannot be moved
            # into the past. Once a fair has started/finished, schedule and
            # participation-defining fields are locked server-side. This cannot
            # be bypassed by removing readonly/disabled attributes in DevTools.
            event_has_started = old_status in {"Ongoing", "Finished"} or (
                job_fair.start_datetime is not None and job_fair.start_datetime <= now
            )

            if not event_has_started and start_datetime <= now:
                raise ValueError("An upcoming Job Fair start date and time must be in the future.")

            submitted_additional_venues = []
            venue_dates = request.form.getlist("add_venue_date[]")
            venue_locations = request.form.getlist("add_venue_location[]")
            venue_times = request.form.getlist("add_venue_time[]")
            venue_row_count = max(len(venue_dates), len(venue_locations), len(venue_times), 0)
            if venue_row_count > 20:
                raise ValueError("A maximum of 20 additional event days/locations is allowed.")

            for index in range(venue_row_count):
                date_raw = venue_dates[index].strip() if index < len(venue_dates) else ""
                location_raw = venue_locations[index].strip() if index < len(venue_locations) else ""
                time_raw = venue_times[index].strip() if index < len(venue_times) else ""

                if not date_raw and not location_raw and not time_raw:
                    continue
                if not date_raw or not location_raw or not time_raw:
                    raise ValueError(
                        f"Additional venue row {index + 1} must include a date, location, and time."
                    )
                try:
                    additional_date = datetime.strptime(date_raw, "%Y-%m-%d").date()
                except ValueError:
                    raise ValueError(f"Additional venue row {index + 1} has an invalid date.")
                if len(location_raw) > 200:
                    raise ValueError(
                        f"Additional venue row {index + 1} location must not exceed 200 characters."
                    )
                if len(time_raw) > 100:
                    raise ValueError(
                        f"Additional venue row {index + 1} time must not exceed 100 characters."
                    )
                if additional_date < start_datetime.date() or additional_date > end_datetime.date():
                    raise ValueError(
                        f"Additional venue row {index + 1} date must fall within the Job Fair schedule."
                    )
                submitted_additional_venues.append({
                    "date": date_raw,
                    "venue": location_raw,
                    "time_range": time_raw,
                })

            if event_has_started:
                protected_changes = []
                if start_datetime != job_fair.start_datetime:
                    protected_changes.append("start date/time")
                if end_datetime != job_fair.end_datetime:
                    protected_changes.append("end date/time")
                if capacity != job_fair.capacity:
                    protected_changes.append("maximum capacity")
                if event_type != job_fair.event_type:
                    protected_changes.append("event classification")
                if lead_agency_id != job_fair.lead_agency_id:
                    protected_changes.append("lead recruitment agency")
                if submitted_additional_venues != (job_fair.additional_venues or []):
                    protected_changes.append("additional event days/locations")
                if protected_changes:
                    raise ValueError(
                        "Once a Job Fair has started, these fields can no longer be changed: "
                        + ", ".join(protected_changes)
                        + "."
                    )

            # Duplicate protection also applies when an existing fair is edited.
            duplicate_fair = JobFair.query.filter(
                JobFair.job_fair_id != job_fair.job_fair_id,
                func.lower(JobFair.title) == title.lower(),
                JobFair.start_datetime == start_datetime,
            ).first()
            if duplicate_fair:
                raise ValueError(
                    "Another Job Fair with the same title and start date/time already exists."
                )

            poster = request.files.get("poster")
            poster_filename = None
            if poster and poster.filename:
                upload_folder = os.path.join(
                    current_app.root_path,
                    "static",
                    "uploads",
                    "posters",
                )
                poster_filename = save_job_fair_banner(poster, upload_folder)
                new_poster_path = os.path.join(upload_folder, poster_filename)
                if job_fair.poster_image:
                    old_poster_path = os.path.join(upload_folder, job_fair.poster_image)

            job_fair.title = title
            job_fair.venue = venue
            job_fair.description = description
            job_fair.status = status_val
            job_fair.event_type = event_type
            job_fair.lead_agency_id = lead_agency_id
            job_fair.capacity = capacity
            job_fair.start_datetime = start_datetime
            job_fair.end_datetime = end_datetime
            job_fair.registration_deadline = registration_deadline
            job_fair.queue_entry_deadline = queue_entry_deadline
            job_fair.additional_venues = submitted_additional_venues or None
            job_fair.enforce_dress_code = "enforce_dress_code" in request.form
            job_fair.require_gov_ids = "require_gov_ids" in request.form
            job_fair.require_srs_id = "require_srs_id" in request.form
            if poster_filename:
                job_fair.poster_image = poster_filename

            sync_exclusive_job_deadlines(job_fair)
            if job_fair.status == "Finished":
                close_exclusive_jobs_for_fair(job_fair, reason="Job Fair finished.")

            record_audit(
                f"JOB_FAIR_UPDATED job_fair_id={job_fair.job_fair_id} "
                f"title={job_fair.title} from_status={old_status} to_status={job_fair.status}"
            )
            db.session.commit()

            if old_poster_path and os.path.isfile(old_poster_path):
                try:
                    os.remove(old_poster_path)
                except OSError:
                    current_app.logger.warning(
                        "Unable to remove old job fair poster: %s", old_poster_path
                    )

            if old_status != job_fair.status and job_fair.status in {"Ongoing", "Finished"}:
                job_fair_date = (
                    job_fair.start_datetime.strftime("%B %d, %Y at %I:%M %p")
                    if job_fair.start_datetime
                    else "TBA"
                )

                if job_fair.status == "Ongoing":
                    notification_title = f"Job Fair Now Ongoing: {job_fair.title}"
                    notification_message = (
                        f"The job fair '{job_fair.title}' is now ongoing! "
                        f"Date: {job_fair_date}. Visit the venue or view the event details."
                    )
                    email_subject = f"Job Fair Is Now Ongoing: {job_fair.title}"
                    email_message = (
                        f"Hello,\n\nThe job fair '{job_fair.title}' is now ongoing!\n\n"
                        f"Date: {job_fair_date}\nVenue: {job_fair.venue or 'TBA'}\n\n"
                        "Visit the PESO IMS system for event details.\n\n"
                        "Best regards,\nPESO-IMS Team"
                    )
                    notification_context = "job fair ongoing website notifications"
                else:
                    notification_title = f"Job Fair Completed: {job_fair.title}"
                    notification_message = (
                        f"The job fair '{job_fair.title}' has been completed. "
                        "Thank you for attending!"
                    )
                    email_subject = f"Job Fair Completed: {job_fair.title}"
                    email_message = (
                        f"Hello,\n\nThe job fair '{job_fair.title}' has been completed.\n\n"
                        "Thank you for your participation!\n\n"
                        "Best regards,\nPESO-IMS Team"
                    )
                    notification_context = "job fair completed website notifications"

                notify_role("jobseeker", notification_title, notification_message, "Job Fair")
                notify_role("employer", notification_title, notification_message, "Job Fair")
                commit_notifications(notification_context)

                recipients = [
                    row.email
                    for row in User.query.filter(
                        User.role.in_(["jobseeker", "employer"]),
                        User.is_deleted.is_(False),
                        User.status == "active",
                    ).all()
                    if row.email
                ]
                send_bulk_email_async(
                    current_app._get_current_object(),
                    recipients,
                    email_subject,
                    email_message,
                )

            flash("Job fair updated successfully!", "success")
            return redirect(url_for("admin.job_fairs"))

        except ValueError as error:
            db.session.rollback()
            if new_poster_path and os.path.isfile(new_poster_path):
                try:
                    os.remove(new_poster_path)
                except OSError:
                    pass
            flash(str(error), "danger")
            return redirect(url_for("admin.edit_job_fair", fair_id=job_fair.job_fair_id))
        except Exception as error:
            db.session.rollback()
            if new_poster_path and os.path.isfile(new_poster_path):
                try:
                    os.remove(new_poster_path)
                except OSError:
                    pass
            current_app.logger.exception("Failed to update Job Fair %s", fair_id)
            flash("Failed to update Job Fair. Please review the submitted information and try again.", "danger")
            return redirect(url_for("admin.edit_job_fair", fair_id=job_fair.job_fair_id))

    return render_template(
        "admin/admin_job_fair_edit.html",
        job_fair=job_fair,
        registered_agencies=registered_agencies,
    )

@admin.route("/job-fair/<int:fair_id>/employers")
@login_required
@role_required("admin")
def manage_employer_applications(fair_id):
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    job_fair = JobFair.query.get_or_404(fair_id)
    applications = JobFairEmployerRegistration.query.filter_by(
        job_fair_id=fair_id
    ).all()

    for app in applications:
        selected_jobs = [
            entry.job for entry in app.selected_jobs if entry.job is not None
        ]
        app.job_vacancies = selected_jobs
        app.exclusive_jobs = [
            job for job in selected_jobs if job.status == "Job Fair"
        ]

    total_employers = len(applications)
    max_capacity = job_fair.capacity or 0

    return render_template(
        "admin/admin_job_fair_employers.html",
        job_fair=job_fair,
        applications=applications,
        total_employers=total_employers,
        max_capacity=max_capacity,
    )


@admin.route(
    "/job-fairs/<int:fair_id>/export-employers"
)
@login_required
@role_required("admin")
def export_job_fair_employers(fair_id):
    if not check_admin_access():
        return render_template(
            "errors/404.html"
        ), 404

    job_fair = JobFair.query.get_or_404(
        fair_id
    )

    employer_registrations = (
        JobFairEmployerRegistration.query
        .filter_by(
            job_fair_id=fair_id
        )
        .order_by(
            JobFairEmployerRegistration
            .registration_date
            .asc()
        )
        .all()
    )

    report = build_job_fair_employer_report(
        job_fair,
        employer_registrations,
    )

    output = (
        generate_job_fair_employer_report_excel(
            report
        )
    )

    safe_title = "".join(
        character
        if character.isalnum()
        or character in ("-", "_")
        else "_"
        for character in (
            job_fair.title
            or f"job_fair_{fair_id}"
        )
    ).strip("_")

    filename = (
        f"{safe_title or 'job_fair'}"
        "_employer_booth_report.xlsx"
    )

    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype=(
            "application/vnd.openxmlformats-"
            "officedocument.spreadsheetml.sheet"
        ),
    )


@admin.route("/job-fair/employer-app/<int:app_id>")
@login_required
@role_required("admin")
def manage_single_employer_application(app_id):
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    app_record = JobFairEmployerRegistration.query.get_or_404(app_id)

    selected_jobs = [
        entry.job for entry in app_record.selected_jobs if entry.job is not None
    ]
    exclusive_jobs = [
        job
        for job in selected_jobs
        if job.job_type == "Job Fair"
    ]

    return render_template(
        "admin/admin_employer_application_detail.html",
        app=app_record,
        selected_jobs=selected_jobs,
        exclusive_jobs=exclusive_jobs,
    )


# --- NEW ACTION ENDPOINTS FOR EMPLOYER JOB FAIR REGISTRATION ---


@admin.route("/job-fair/employer-app/<int:app_id>/approve", methods=["POST"])
@login_required
@role_required("admin")
def approve_employer_job_fair_application(app_id):
    if not check_admin_access():
        flash("Unauthorized access.", "danger")
        return redirect(url_for("admin.dashboard"))

    app_record = JobFairEmployerRegistration.query.get_or_404(app_id)
    redirect_url = url_for("admin.manage_employer_applications", fair_id=app_record.job_fair_id)

    if app_record.status != "Pending":
        flash("This employer registration has already been processed.", "warning")
        return redirect(redirect_url)

    if app_record.job_fair and app_record.job_fair.status == "Finished":
        flash(
            "A finished Job Fair can no longer accept employer registrations.",
            "danger",
        )
        return redirect(redirect_url)

    selected_jobs = [entry.job for entry in app_record.selected_jobs if entry.job is not None]
    unapproved_jobs = [job for job in selected_jobs if job.approval_status != "Approved"]

    if unapproved_jobs:
        flash(
            "Cannot approve job fair registration. Please review and approve all submitted job vacancies first.",
            "danger",
        )
        return redirect(redirect_url)

    booth_number = (request.form.get("booth_number") or "").strip()
    if len(booth_number) > 50:
        flash("Booth number cannot exceed 50 characters.", "danger")
        return redirect(redirect_url)
    if booth_number:
        existing_booth = JobFairEmployerRegistration.query.filter(
            JobFairEmployerRegistration.job_fair_id == app_record.job_fair_id,
            func.lower(func.trim(JobFairEmployerRegistration.booth_number)) == booth_number.lower(),
            JobFairEmployerRegistration.registration_id != app_record.registration_id,
        ).first()
        if existing_booth:
            flash(
                f"Booth number '{booth_number}' is already assigned to another employer for this job fair.",
                "danger",
            )
            return redirect(redirect_url)
        app_record.booth_number = booth_number

    app_record.status = "Approved"
    record_audit(
        f"JOB_FAIR_EMPLOYER_APPROVED registration_id={app_record.registration_id} job_fair_id={app_record.job_fair_id}"
    )

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("Failed to approve Job Fair employer application %s: %s", app_id, e)
        flash("Unable to approve this application right now. Please try again.", "danger")
        return redirect(redirect_url)

    employer = app_record.employer
    company_name = employer.company_name if employer else app_record.company_name
    employer_user = employer.user if employer else None

    # Website Notification
    if employer_user:
        notify_user(
            employer_user.user_id,
            "Job Fair Registration Approved",
            "Your registration for the Job Fair has been approved.",
            "Job Fair",
        )
        commit_notifications("job fair employer registration notification")

    # Email
    employer_email = getattr(employer, "company_email", None) if employer else None
    if not employer_email and employer_user:
        employer_email = employer_user.email

    job_fair = getattr(app_record, "job_fair", None)
    fair_title = job_fair.title if job_fair else "the PESO Job Fair"

    if employer_email:
        email_body = f"""Hello {company_name},

Your employer registration for {fair_title} has been approved.

Booth Number:
{app_record.booth_number or 'To be announced'}

Please log in to PESO-IMS for the complete event information and instructions.

Best regards,
Public Employment Service Office (PESO)"""

        try:
            send_email(
                recipient=employer_email,
                subject=f"Job Fair Registration Approved - {fair_title}",
                body=email_body,
            )
            flash(f"Application for '{company_name}' has been approved and the employer was notified by email.", "success")
        except Exception as e:
            current_app.logger.exception("Job Fair approval email failed for registration %s: %s", app_record.registration_id, e)
            flash(f"Application for '{company_name}' was approved, but the email could not be sent.", "warning")
    else:
        flash(f"Application for '{company_name}' was approved, but no employer email address is available.", "warning")

    return redirect(redirect_url)



@admin.route("/job-fair/employer-app/<int:app_id>/reject", methods=["POST"])
@login_required
@role_required("admin")
def reject_employer_job_fair_application(app_id):
    if not check_admin_access():
        flash("Unauthorized access.", "danger")
        return redirect(url_for("admin.dashboard"))

    app_record = JobFairEmployerRegistration.query.get_or_404(app_id)
    redirect_url = url_for("admin.manage_employer_applications", fair_id=app_record.job_fair_id)

    if app_record.status != "Pending":
        flash("This employer registration has already been processed.", "warning")
        return redirect(redirect_url)

    if app_record.job_fair and app_record.job_fair.status == "Finished":
        flash("A finished Job Fair can no longer change employer registrations.", "danger")
        return redirect(redirect_url)

    app_record.status = "Rejected"
    record_audit(
        f"JOB_FAIR_EMPLOYER_REJECTED registration_id={app_record.registration_id} job_fair_id={app_record.job_fair_id}"
    )

    close_exclusive_jobs_for_registration(
        app_record,
        reason=(
            "PESO rejected the employer Job Fair registration."
        ),
    )

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("Failed to reject Job Fair employer application %s: %s", app_id, e)
        flash("Unable to reject this application right now. Please try again.", "danger")
        return redirect(redirect_url)

    employer = app_record.employer
    company_name = employer.company_name if employer else app_record.company_name
    employer_user = employer.user if employer else None

    # Website Notification
    if employer_user:
        notify_user(
            employer_user.user_id,
            "Job Fair Registration Rejected",
            "Your employer registration for the Job Fair was not approved.",
            "Job Fair",
        )
        commit_notifications("job fair employer registration rejection notification")

    employer_email = getattr(employer, "company_email", None) if employer else None
    if not employer_email and employer_user:
        employer_email = employer_user.email

    job_fair = getattr(app_record, "job_fair", None)
    fair_title = job_fair.title if job_fair else "the PESO Job Fair"

    # Email
    if employer_email:
        email_body = f"""Hello {company_name},

Your employer registration for {fair_title} was reviewed and was not approved.

Please contact the PESO office if you require clarification or additional information.

Best regards,
Public Employment Service Office (PESO)"""

        try:
            send_email(
                recipient=employer_email,
                subject=f"Job Fair Registration Update - {fair_title}",
                body=email_body,
            )
            flash(f"Application for '{company_name}' has been rejected and the employer was notified by email.", "warning")
        except Exception as e:
            current_app.logger.exception("Job Fair rejection email failed for registration %s: %s", app_record.registration_id, e)
            flash(f"Application for '{company_name}' was rejected, but the email could not be sent.", "warning")
    else:
        flash(f"Application for '{company_name}' was rejected, but no employer email address is available.", "warning")

    return redirect(redirect_url)



@admin.route("/job-fair/<int:fair_id>/attendees")
@login_required
@role_required("admin")
def view_jobfair_attendees(fair_id):
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    job_fair = JobFair.query.get_or_404(fair_id)

    attendees = (
        JobFairRegistration.query
        .filter_by(job_fair_id=fair_id)
        .order_by(JobFairRegistration.registration_date.asc())
        .all()
    )

    report = build_job_fair_report(
        job_fair,
        attendees,
    )

    participant_rows = {
        row["registration_id"]: row
        for row in report["participants"]
    }

    return render_template(
        "admin/admin_job_fair_attendees.html",
        job_fair=job_fair,
        attendees=attendees,
        report_summary=report["summary"],
        participant_rows=participant_rows,
        registration_datetime_ph=registration_datetime_ph,
    )

def _parse_interview_capacity(raw):
    """Accept only an unsigned whole-number form value, never floats or signs."""
    value = str(raw or "").strip()
    if not value or not value.isascii() or not value.isdecimal():
        return None
    return int(value)


def _enforce_station_capacity(registration, capacity):
    """Revoke stations above a newly approved capacity and return their applicant to waiting."""
    excess = JobFairInterviewStation.query.filter(
        JobFairInterviewStation.employer_registration_id == registration.registration_id,
        JobFairInterviewStation.station_number > capacity
    ).all()
    for station in excess:
        JobFairQueue.query.filter_by(assigned_station_id=station.station_id, status="Active").update(
            {"assigned_station_id": None, "serving_started_at": None}, synchronize_session=False
        )
        station.is_revoked = True
        station.access_token_hash = None
        station.short_code_hash = None


@admin.route("/employer-reg/<int:reg_id>/interview-capacity", methods=["POST"])
@login_required
@role_required("admin")
def update_employer_interview_capacity(reg_id):
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    registration = JobFairEmployerRegistration.query.get_or_404(reg_id)
    redirect_url = url_for(
        "admin.manage_employer_applications", fair_id=registration.job_fair_id
    )

    if registration.status != "Approved":
        flash("Interview capacity can only be adjusted for an approved employer.", "warning")
        return redirect(redirect_url)

    representatives = registration.representatives or 1
    capacity = _parse_interview_capacity(request.form.get("simultaneous_interview_capacity"))
    if capacity is None or capacity < 1 or capacity > representatives or capacity > 15:
        flash(
            f"Interview capacity must be between 1 and {min(representatives, 15)} for this employer.",
            "danger",
        )
        return redirect(redirect_url)

    old_capacity = registration.simultaneous_interview_capacity or 1
    registration.simultaneous_interview_capacity = capacity
    _enforce_station_capacity(registration, capacity)
    record_audit(
        "JOB_FAIR_INTERVIEW_CAPACITY_UPDATED "
        f"registration_id={registration.registration_id} "
        f"job_fair_id={registration.job_fair_id} "
        f"old_capacity={old_capacity} new_capacity={capacity}"
    )

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(
            "Failed to update interview capacity for registration %s: %s",
            reg_id,
            exc,
        )
        flash("Unable to update interview capacity right now.", "danger")
        return redirect(redirect_url)

    flash("Simultaneous interview capacity updated.", "success")
    return redirect(redirect_url)


@admin.route("/employer-reg/<int:reg_id>/update-status", methods=["POST"])
@login_required
@role_required("admin")
def update_employer_reg_status(reg_id):
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    registration = JobFairEmployerRegistration.query.get_or_404(reg_id)
    redirect_url = url_for("admin.manage_employer_applications", fair_id=registration.job_fair_id)

    status = (request.form.get("status") or "").strip()

    if registration.status != "Pending":
        flash("This employer registration has already been processed.", "warning")
        return redirect(redirect_url)

    if (
        status == "Approved"
        and registration.job_fair
        and registration.job_fair.status == "Finished"
    ):
        flash(
            "A finished Job Fair can no longer accept employer registrations.",
            "danger",
        )
        return redirect(redirect_url)
    booth_number = (request.form.get("booth_number", "")).strip()

    if len(booth_number) > 50:
        flash("Booth number cannot exceed 50 characters.", "danger")
        return redirect(redirect_url)

    if status not in ["Approved", "Rejected"]:
        flash("Invalid status update.", "danger")
        return redirect(redirect_url)

    # Approval Validation
    if status == "Approved":
        if not booth_number:
            flash("Booth number is required to approve a registration.", "danger")
            return redirect(redirect_url)

        existing_booth = JobFairEmployerRegistration.query.filter(
            JobFairEmployerRegistration.job_fair_id == registration.job_fair_id,
            func.lower(func.trim(JobFairEmployerRegistration.booth_number)) == booth_number.lower(),
            JobFairEmployerRegistration.registration_id != registration.registration_id,
        ).first()

        if existing_booth:
            flash(
                f"Booth number '{booth_number}' is already assigned to another employer for this job fair.",
                "danger",
            )
            return redirect(redirect_url)

        selected_jobs = [entry.job for entry in registration.selected_jobs if entry.job is not None]
        unapproved_jobs = [job for job in selected_jobs if job.approval_status != "Approved"]

        if unapproved_jobs:
            flash(
                "Cannot approve registration. Please ensure all submitted job vacancies are approved first.",
                "danger",
            )
            return redirect(redirect_url)

    # Validate PESO-confirmed simultaneous interview capacity.
    capacity = _parse_interview_capacity(
        request.form.get(
            "simultaneous_interview_capacity",
            str(registration.simultaneous_interview_capacity or 1),
        )
    )
    representatives = registration.representatives or 1
    if capacity is None or capacity < 1 or capacity > representatives or capacity > 15:
        flash(
            f"Interview capacity must be between 1 and {min(representatives, 15)} for this employer.",
            "danger",
        )
        return redirect(redirect_url)

    # Update Database
    registration.status = status
    registration.simultaneous_interview_capacity = capacity
    _enforce_station_capacity(registration, capacity)

    if status == "Approved":
        registration.booth_number = booth_number
    else:
        registration.booth_number = None

        close_exclusive_jobs_for_registration(
            registration,
            reason=(
                "PESO rejected the employer Job Fair registration."
            ),
        )

    employer = registration.employer
    employer_user = employer.user if employer else None

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(
            "Failed to update Job Fair registration %s to %s: %s",
            registration.registration_id,
            status,
            e,
        )
        flash("Unable to update employer registration.", "danger")
        return redirect(redirect_url)

    # Website Notification
    if employer_user:
        notify_user(
            employer_user.user_id,
            f"Job Fair Registration {status}",
            f"Your booth registration has been {status.lower()}.",
            "Job Fair",
        )
        commit_notifications("job fair employer registration notification")

    # Email
    employer_email = getattr(employer, "company_email", None) if employer else None
    if not employer_email and employer_user:
        employer_email = employer_user.email

    company_name = employer.company_name if employer else getattr(registration, "company_name", "Employer")
    job_fair = getattr(registration, "job_fair", None)
    event_title = job_fair.title if job_fair else "PESO Job Fair"

    if employer_email:
        if status == "Approved":
            email_body = f"""Hello {company_name},

Your registration for {event_title} has been approved.

Booth Number:
{registration.booth_number or 'To be announced'}

Please log in to PESO-IMS for complete event details.

Best regards,
Public Employment Service Office (PESO)"""
        else:
            email_body = f"""Hello {company_name},

Your registration for {event_title} was reviewed and was not approved.

Please contact the PESO office if you require clarification or additional information.

Best regards,
Public Employment Service Office (PESO)"""

        try:
            send_email(
                recipient=employer_email,
                subject=f"Job Fair Registration {status} - {event_title}",
                body=email_body,
            )
            flash(f"Employer registration has been {status.lower()} and an email notification was sent.", "success")
        except Exception as e:
            current_app.logger.exception(
                "Job Fair registration email failed for registration %s: %s",
                registration.registration_id,
                e,
            )
            flash(f"Employer registration has been {status.lower()}, but the email could not be sent.", "warning")
    else:
        flash(f"Employer registration has been {status.lower()}, but no email address is available.", "warning")

    return redirect(redirect_url)


@admin.route("/job-fairs/<int:fair_id>/export-attendees")
@login_required
@role_required("admin")
def export_job_fair_attendees(fair_id):
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    job_fair = JobFair.query.get_or_404(fair_id)

    attendees = (
        JobFairRegistration.query
        .filter_by(job_fair_id=fair_id)
        .order_by(JobFairRegistration.registration_date.asc())
        .all()
    )

    report = build_job_fair_report(
        job_fair,
        attendees,
    )

    output = generate_job_fair_report_excel(
        report
    )

    safe_title = "".join(
        character
        if character.isalnum() or character in ("-", "_")
        else "_"
        for character in (job_fair.title or f"job_fair_{fair_id}")
    ).strip("_")

    filename = (
        f"{safe_title or 'job_fair'}"
        f"_registration_report.xlsx"
    )

    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype=(
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        ),
    )


def _format_audit_action(raw_action):
    """Convert stored audit text into a readable UI representation."""
    raw_action = (raw_action or "").strip()
    if not raw_action:
        return {"title": "System Activity", "details": [], "transition": None}

    parts = raw_action.split()
    event = parts[0]
    values = {}
    for token in parts[1:]:
        if "=" in token:
            key, value = token.split("=", 1)
            values[key] = value.replace("_", " ")

    titles = {
        "LOGIN_SUCCESS": "Signed In",
        "LOGOUT": "Signed Out",
        "ADMIN_CREATED": "Administrator Created",
        "ADMIN_UPDATED": "Administrator Updated",
        "EMPLOYER_REGISTRATION_APPROVED": "Employer Registration Approved",
        "EMPLOYER_REGISTRATION_REJECTED": "Employer Registration Rejected",
        "JOB_STATUS_CHANGED": "Job Status Changed",
        "REFERRAL_ISSUED": "Referral Issued",
        "REFERRAL_REJECTED": "Referral Rejected",
        "APPLICATION_REVIEWED": "Application Reviewed",
        "APPLICATION_HIRED": "Applicant Hired",
        "JOB_FAIR_CREATED": "Job Fair Created",
        "JOB_FAIR_EMPLOYER_APPROVED": "Job Fair Employer Approved",
        "JOB_FAIR_EMPLOYER_REJECTED": "Job Fair Employer Rejected",
        "JOB_FAIR_EVALUATION": "Job Fair Evaluation",
        "REFERRAL_REQUESTED": "Referral Requested",
        "INTERVIEW_CONFIRMED": "Interview Confirmed",
        "INTERVIEW_UNABLE_TO_ATTEND": "Unable to Attend Interview",
        "JOB_FAIR_REGISTERED": "Job Fair Registration",
        "INTERVIEW_SCHEDULED": "Interview Scheduled",
        "INTERVIEW_BATCH_SCHEDULED": "Bulk Interviews Scheduled",
    }

    labels = {
        "job_id": "Job",
        "application_id": "Application",
        "referral_id": "Referral",
        "request_id": "Request",
        "registration_id": "Registration",
        "job_fair_id": "Job Fair",
        "user_id": "User",
        "username": "Username",
        "role": "Role",
        "mfa": "MFA",
        "title": "Title",
    }

    transition = None
    if "from" in values or "to" in values:
        transition = {
            "from": values.pop("from", "—"),
            "to": values.pop("to", "—"),
        }

    details = []
    preferred_order = [
        "job_id", "application_id", "referral_id", "request_id",
        "registration_id", "job_fair_id", "user_id", "username",
        "role", "mfa", "title",
    ]
    for key in preferred_order:
        if key in values:
            value = values.pop(key)
            if key.endswith("_id"):
                value = f"#{value}"
            details.append({"label": labels.get(key, key.replace("_", " ").title()), "value": value})

    for key, value in values.items():
        details.append({"label": key.replace("_", " ").title(), "value": value})

    # Keep the table concise: one useful reference is enough because actor,
    # role, time, and IP already have dedicated columns.
    summary = None
    if details and event not in {"LOGIN_SUCCESS", "LOGOUT"}:
        first = details[0]
        summary = f"{first['label']} {first['value']}"

    job_id = None
    raw_job_id = next((item["value"] for item in details if item["label"] == "Job"), None)
    if raw_job_id:
        try:
            job_id = int(str(raw_job_id).lstrip("#"))
        except (TypeError, ValueError):
            job_id = None

    return {
        "event": event,
        "title": titles.get(event, event.replace("_", " ").title()),
        "details": details,
        "transition": transition,
        "summary": summary,
        "job_id": job_id,
        "job_title": None,
    }


def _attach_audit_job_titles(action_display):
    """Resolve all referenced jobs in one query and attach their titles."""
    job_ids = {
        action.get("job_id")
        for action in action_display.values()
        if action.get("job_id") is not None
    }
    if not job_ids:
        return action_display

    job_titles = dict(
        db.session.query(Job.job_id, Job.job_title)
        .filter(Job.job_id.in_(job_ids))
        .all()
    )
    for action in action_display.values():
        if action.get("job_id") is not None:
            action["job_title"] = job_titles.get(action["job_id"])
    return action_display


def _audit_period_bounds(year_raw, month_raw):
    """Interpret audit periods in Philippine local time; stored audit timestamps are UTC."""
    year_raw = (year_raw or "").strip()
    month_raw = (month_raw or "").strip()
    if not year_raw and not month_raw:
        return None, None
    if not re.fullmatch(r"[0-9]{4}", year_raw):
        raise ValueError("Select a valid year.")
    year = int(year_raw)
    if year < 2000 or year > datetime.now(ZoneInfo("Asia/Manila")).year:
        raise ValueError("Year is outside the permitted range.")
    if month_raw and (not re.fullmatch(r"0?[1-9]|1[0-2]", month_raw)):
        raise ValueError("Select a valid month.")
    month = int(month_raw) if month_raw else 1
    end_year, end_month = (year + 1, 1) if not month_raw or month == 12 else (year, month + 1)
    if month_raw and month != 12:
        pass
    elif month_raw and month == 12:
        end_year, end_month = year + 1, 1
    ph = ZoneInfo("Asia/Manila")
    start = datetime(year, month, 1, tzinfo=ph).astimezone(timezone.utc).replace(tzinfo=None)
    end = datetime(end_year, end_month, 1, tzinfo=ph).astimezone(timezone.utc).replace(tzinfo=None)
    return start, end


def _audit_period_query(year_raw, month_raw):
    start, end = _audit_period_bounds(year_raw, month_raw)
    query = AuditLog.query
    if start is not None:
        query = query.filter(AuditLog.created_at >= start, AuditLog.created_at < end)
    return query


@admin.route("/audit-logs")
@role_required("admin")
def audit_logs():
    # Match the other Admin list pages: load the audit rows once and let the
    # page search/filter them instantly in the browser without form submits.
    logs = (
        AuditLog.query
        .outerjoin(User, AuditLog.user_id == User.user_id)
        .order_by(AuditLog.created_at.desc(), AuditLog.log_id.desc())
        .all()
    )

    action_display = _attach_audit_job_titles({
        log.log_id: _format_audit_action(log.action)
        for log in logs
    })

    action_types = sorted({
        log.action.split()[0]
        for log in logs
        if log.action
    })
    action_labels = {
        item: _format_audit_action(item)["title"]
        for item in action_types
    }

    years = list(range(datetime.now(ZoneInfo("Asia/Manila")).year, 2019, -1))
    return render_template(
        "admin/admin_audit_logs.html",
        years=years,
        logs=logs,
        action_display=action_display,
        action_types=action_types,
        action_labels=action_labels,
    )


@admin.route("/audit-logs/delete-preview", methods=["GET"])
@role_required("admin")
def audit_logs_delete_preview():
    try:
        year = request.args.get("year")
        month = request.args.get("month")
        if not year:
            raise ValueError("Select a year before deleting logs.")
        query = _audit_period_query(year, month)
        # Always preserve the last 30 days of security logs.
        cutoff = datetime.utcnow() - timedelta(days=30)
        count = query.filter(AuditLog.created_at < cutoff).count()
        return jsonify(success=True, count=count, protected_recent=True)
    except ValueError as exc:
        return jsonify(success=False, message=str(exc)), 400


@admin.route("/audit-logs/delete", methods=["POST"])
@role_required("admin")
@limiter.limit("5 per hour")
def delete_audit_logs():
    from werkzeug.security import check_password_hash
    year = request.form.get("year", "")
    month = request.form.get("month", "")
    try:
        if not year:
            raise ValueError("Choose a year before deleting audit logs.")
        query = _audit_period_query(year, month)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.audit_logs"))

    if request.form.get("confirmation", "").strip() != "DELETE AUDIT LOGS":
        flash("Enter DELETE AUDIT LOGS exactly to confirm.", "error")
        return redirect(url_for("admin.audit_logs"))
    if not check_password_hash(current_user.password_hash, request.form.get("password", "")):
        flash("Password verification failed. No logs were deleted.", "error")
        return redirect(url_for("admin.audit_logs"))

    cutoff = datetime.utcnow() - timedelta(days=30)
    query = query.filter(AuditLog.created_at < cutoff)
    count = query.count()
    if not count:
        flash("No eligible logs were found. Recent 30-day logs are protected.", "info")
        return redirect(url_for("admin.audit_logs"))
    if count > 100000:
        flash("Too many logs in one deletion. Contact your system administrator.", "error")
        return redirect(url_for("admin.audit_logs"))
    try:
        query.delete(synchronize_session=False)
        db.session.commit()
        record_audit(f"AUDIT_LOG_PURGE year={year} month={month or 'all'} count={count}", commit=True)
        flash(f"Deleted {count} eligible audit logs. The last 30 days remain protected.", "success")
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Audit log deletion failed")
        flash("Unable to delete audit logs. No further changes were made.", "error")
    return redirect(url_for("admin.audit_logs"))


@admin.route("/audit-logs/export")
@role_required("admin")
def export_audit_logs():
    search = (request.args.get("search") or "").strip()
    role = (request.args.get("role") or "").strip().lower()
    action_type = (request.args.get("action_type") or "").strip().upper()

    try:
        query = _audit_period_query(request.args.get("year"), request.args.get("month")).outerjoin(
            User, AuditLog.user_id == User.user_id
        )
    except ValueError as exc:
        abort(400, description=str(exc))
    if role in {"admin", "employer", "jobseeker"}:
        query = query.filter(User.role == role)
    if action_type:
        query = query.filter(AuditLog.action.ilike(f"{action_type}%"))

    records = query.order_by(AuditLog.created_at.desc(), AuditLog.log_id.desc()).all()
    export_actions = _attach_audit_job_titles({
        log.log_id: _format_audit_action(log.action)
        for log in records
    })

    # Keep Excel export aligned with the instant client-side search.  The UI
    # searches both stored audit text and the friendly action title.
    if search:
        needle = search.lower()
        records = [
            log for log in records
            if needle in " ".join([
                log.user.username if log.user else "",
                log.user.email if log.user else "",
                log.action or "",
                log.ip_address or "",
                export_actions[log.log_id]["title"],
                export_actions[log.log_id].get("job_title") or "",
                str(export_actions[log.log_id].get("job_id") or ""),
            ]).lower()
        ]

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Audit Logs"

    primary = "0D357D"
    primary_soft = "EAF1FB"
    excel_green = "1D6F42"
    border_color = "D9E2EC"
    muted_fill = "F8FAFC"
    white = "FFFFFF"
    dark = "172033"
    thin = Side(style="thin", color=border_color)

    sheet.merge_cells("A1:H1")
    sheet["A1"] = "PESO-IMS AUDIT LOG REPORT"
    sheet["A1"].font = Font(bold=True, size=16, color=white)
    sheet["A1"].fill = PatternFill("solid", fgColor=primary)
    sheet["A1"].alignment = Alignment(horizontal="center", vertical="center")
    sheet.row_dimensions[1].height = 28

    local_now = datetime.now(ZoneInfo(current_app.config.get("USER_TIMEZONE", "Asia/Manila")))
    sheet.merge_cells("A2:H2")
    sheet["A2"] = f"Generated: {local_now.strftime('%b %d, %Y %I:%M %p')}"
    sheet["A2"].font = Font(italic=True, size=10, color="64748B")
    sheet["A2"].alignment = Alignment(horizontal="center")

    role_label = role.capitalize() if role else "All Roles"
    action_label = _format_audit_action(action_type)["title"] if action_type else "All Actions"
    search_label = search or "None"
    sheet.merge_cells("A3:H3")
    sheet["A3"] = f"Filters — Search: {search_label} | Role: {role_label} | Action: {action_label}"
    sheet["A3"].font = Font(size=10, color=dark)
    sheet["A3"].fill = PatternFill("solid", fgColor=primary_soft)
    sheet["A3"].alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.row_dimensions[3].height = 24

    headers = ["Date & Time", "Actor", "Role", "Action", "Job Title", "Job ID", "Details", "IP Address"]
    for col, header in enumerate(headers, 1):
        cell = sheet.cell(row=5, column=col, value=header)
        cell.font = Font(bold=True, color=white)
        cell.fill = PatternFill("solid", fgColor=excel_green)
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = Border(top=thin, bottom=thin, left=thin, right=thin)

    target_tz = ZoneInfo(current_app.config.get("USER_TIMEZONE", "Asia/Manila"))
    for row_index, log in enumerate(records, 6):
        action = export_actions[log.log_id]
        created = log.created_at
        if created:
            if created.tzinfo is None:
                created = created.replace(tzinfo=ZoneInfo("UTC"))
            created_text = created.astimezone(target_tz).strftime("%b %d, %Y %I:%M %p")
        else:
            created_text = ""

        transition_text = ""
        if action["transition"]:
            transition_text = f"{action['transition']['from']} → {action['transition']['to']}"
        detail_text = "; ".join(
            f"{item['label']}: {item['value']}" for item in action["details"]
        )
        if transition_text:
            detail_text = f"{transition_text}" + (f" | {detail_text}" if detail_text else "")

        action_text = action["title"]
        if action["transition"]:
            action_text = (
                f"{action['title']} from {action['transition']['from']} "
                f"to {action['transition']['to']}"
            )

        values = [
            created_text,
            log.user.username if log.user else "System / unavailable",
            log.user.role.capitalize() if log.user and log.user.role else "",
            action_text,
            action.get("job_title") or "",
            action.get("job_id") or "",
            detail_text,
            log.ip_address or "",
        ]
        for col, value in enumerate(values, 1):
            cell = sheet.cell(row=row_index, column=col, value=value)
            cell.border = Border(bottom=thin)
            cell.alignment = Alignment(vertical="top", wrap_text=(col in {4, 5, 7}))
            if row_index % 2 == 0:
                cell.fill = PatternFill("solid", fgColor=muted_fill)

    last_row = max(5, 5 + len(records))
    sheet.freeze_panes = "A6"
    sheet.auto_filter.ref = f"A5:H{last_row}"
    sheet.sheet_view.showGridLines = False
    widths = {"A": 23, "B": 24, "C": 14, "D": 36, "E": 30, "F": 12, "G": 44, "H": 18}
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width

    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    timestamp = local_now.strftime("%Y%m%d_%H%M%S")
    return send_file(
        output,
        as_attachment=True,
        download_name=f"PESO_IMS_Audit_Logs_{timestamp}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _valid_admin_contact_number(value):
    """Optional PH mobile number: 09XXXXXXXXX or +639XXXXXXXXX."""
    return not value or re.fullmatch(r"(?:09\d{9}|\+639\d{9})", value) is not None


def _valid_admin_email(value):
    """Validate account email on the server (HTML type=email is bypassable)."""
    from email_validator import EmailNotValidError, validate_email

    if not value or len(value) > 100:
        return False
    try:
        validate_email(value, check_deliverability=False)
    except EmailNotValidError:
        return False
    return True


def _valid_admin_account_name(value):
    return bool(value and re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ .\'-]*", value))


def _valid_admin_username(value):
    return bool(value and re.fullmatch(r"[A-Za-z0-9_.-]{3,50}", value))


@admin.route("/create-admin", methods=["GET", "POST"])
@role_required("admin")
def create_admin():
    # Retired Super Admin-only action. Never elevate every admin implicitly.
    abort(403, description="This retired privileged operation is unavailable.")

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password")
        confirm_password = request.form.get("confirm_password")
        first_name = request.form.get("first_name", "").strip()
        last_name = request.form.get("last_name", "").strip()
        contact_number = request.form.get("contact_number", "").strip()

        # 1. Basic Form Validation
        if not all([username, email, password, confirm_password, first_name, last_name]):
            flash("Please complete all required fields.", "danger")
            return redirect(url_for("admin.create_admin"))

        if len(username) > 50 or len(email) > 100 or len(first_name) > 100 or len(last_name) > 100 or len(contact_number) > 13:
            flash("One or more account fields exceed the allowed length.", "danger")
            return redirect(url_for("admin.create_admin"))
        if not _valid_admin_username(username):
            flash("Username must be 3–50 characters (letters, numbers, dots, underscores or hyphens).", "danger")
            return redirect(url_for("admin.create_admin"))
        if not _valid_admin_account_name(first_name) or not _valid_admin_account_name(last_name):
            flash("Names must contain letters, spaces, apostrophes, periods or hyphens only.", "danger")
            return redirect(url_for("admin.create_admin"))
        if not _valid_admin_contact_number(contact_number):
            flash("Enter a valid Philippine mobile number (09XXXXXXXXX or +639XXXXXXXXX).", "danger")
            return redirect(url_for("admin.create_admin"))
        if not _valid_admin_email(email):
            flash("Please enter a valid email address.", "danger")
            return redirect(url_for("admin.create_admin"))
        if not 8 <= len(password) <= 128:
            flash("Password must contain 8 to 128 characters.", "danger")
            return redirect(url_for("admin.create_admin"))
        if not (re.search(r"[a-z]", password) and re.search(r"[A-Z]", password)
                and re.search(r"[0-9]", password)
                and re.search(r'[!@#$%^&*(),.?":{}|<>]', password)):
            flash("Password needs uppercase and lowercase letters, a number, and a special character.", "danger")
            return redirect(url_for("admin.create_admin"))

        # Password Match Check
        if password != confirm_password:
            flash("Passwords do not match. Please try again.", "danger")
            return redirect(url_for("admin.create_admin"))

        # 2. Check for Duplicate Username or Email
        if User.query.filter((User.username == username) | (User.email == email)).first():
            flash("Username or email address is already in use.", "danger")
            return redirect(url_for("admin.create_admin"))

        try:
            # 3. Instantiate base User account
            new_user = User(
                username=username,
                email=email,
                role="admin",
                status="active",
                email_verified=True,
                created_at=datetime.utcnow()
            )
            new_user.set_password(password)

            db.session.add(new_user)
            db.session.flush()

            # 4. Instantiate corresponding Admin profile model
            new_admin = Admin(
                user_id=new_user.user_id,
                first_name=first_name,
                last_name=last_name,
                contact_number=contact_number
            )

            db.session.add(new_admin)
            record_audit(
                f"ADMIN_CREATED public_id={new_user.public_id} username={new_user.username}"
            )
            db.session.commit()

            flash(f"Admin account for '{username}' created successfully!", "success")
            return redirect(url_for("admin.create_admin"))

        except Exception as e:
            db.session.rollback()
            flash(f"An error occurred while creating the account: {str(e)}", "danger")

    admins = Admin.query.join(User, Admin.user_id == User.user_id).all()
    return render_template("admin/admin_management.html", admins=admins)


@admin.route("/update-admin/<string:public_id>", methods=["POST"])
@role_required("admin")
def update_admin(public_id):
    # Retired Super Admin-only action. Never elevate every admin implicitly.
    abort(403, description="This retired privileged operation is unavailable.")

    admin_record = (Admin.query.join(User, Admin.user_id == User.user_id).filter(User.public_id == public_id).first_or_404())
    user_record = admin_record.user

    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip().lower()
    contact_number = request.form.get("contact_number", "").strip()

    if not all([username, email]):
        flash("Username and Email are required.", "danger")
        return redirect(url_for("admin.create_admin"))
    if len(username) > 50 or len(email) > 100 or len(contact_number) > 13:
        flash("One or more account fields exceed the allowed length.", "danger")
        return redirect(url_for("admin.create_admin"))
    if not _valid_admin_username(username):
        flash("Username must be 3–50 characters (letters, numbers, dots, underscores or hyphens).", "danger")
        return redirect(url_for("admin.create_admin"))
    if not _valid_admin_contact_number(contact_number):
        flash("Enter a valid Philippine mobile number (09XXXXXXXXX or +639XXXXXXXXX).", "danger")
        return redirect(url_for("admin.create_admin"))
    if not _valid_admin_email(email):
        flash("Please enter a valid email address.", "danger")
        return redirect(url_for("admin.create_admin"))

    # Check for duplicate username or email belonging to ANOTHER user
    duplicate_user = User.query.filter(
        (User.user_id != user_record.user_id) &
        ((User.username == username) | (User.email == email))
    ).first()

    if duplicate_user:
        if duplicate_user.username == username:
            flash(f"The username '{username}' is already taken.", "danger")
        else:
            flash(f"The email address '{email}' is already in use.", "danger")
        return redirect(url_for("admin.create_admin"))

    try:
        user_record.username = username
        user_record.email = email
        admin_record.contact_number = contact_number
        record_audit(
            f"ADMIN_UPDATED public_id={user_record.public_id} username={user_record.username}"
        )

        db.session.commit()
        flash("Admin details updated successfully!", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"An error occurred while updating the account: {str(e)}", "danger")

    return redirect(url_for("admin.create_admin"))


@admin.route('/jobseekers/<string:public_id>/export-jsrf', methods=['GET'])
@login_required
@role_required("admin")
def export_jobseeker_jsrf(public_id):
    """Exports a single Jobseeker's NSRP JSRF as an Excel file."""
    seeker = (JobSeeker.query.join(User).filter(User.public_id == public_id).first_or_404())
    
    excel_stream = generate_jsrf_excel(seeker)
    filename = f"JSRF_{seeker.user.public_id}_{seeker.last_name}_{seeker.first_name}.xlsx"
    
    return send_file(
        excel_stream,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename
    )
    

@admin.route('/jobseekers/export-bulk', methods=['POST'])
@login_required
@role_required("admin")
@limiter.limit("10 per hour", methods=["POST"])
def export_bulk_jobseekers():
    # Validate the request before querying or generating a file containing PII.
    # Enforce a bounded selection to prevent excessive memory/CPU usage.
    selected_ids = request.form.getlist('selected_jobseeker_ids')
    if len(selected_ids) > 500:
        abort(400, description="You may export up to 500 job seekers at a time.")
    if any(not isinstance(value, str) or not value or len(value) > 128
           or not re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in selected_ids):
        abort(400, description="Invalid job seeker identifier.")
    selected_ids = list(dict.fromkeys(selected_ids))

    if not selected_ids:
        flash("No job seekers were selected for export.", "warning")
        return redirect(url_for('admin.jobseekers'))

    # Registration date means the jobseeker account's creation date (stored UTC).
    # Date selections refer to Philippine calendar dates, inclusive.
    from datetime import date as date_type
    from zoneinfo import ZoneInfo as TZ
    start_text = (request.form.get("registration_from") or "").strip()
    end_text = (request.form.get("registration_to") or "").strip()
    try:
        start_date = date_type.fromisoformat(start_text) if start_text else None
        end_date = date_type.fromisoformat(end_text) if end_text else None
    except ValueError:
        abort(400, description="Invalid registration date filter.")
    if start_date and end_date and start_date > end_date:
        abort(400, description="Registration start date cannot be later than end date.")
    if (start_date and start_date.year < 2000) or (end_date and end_date.year > 2100):
        abort(400, description="Registration date outside supported range.")
    registration_query = JobSeeker.query.join(User).filter(User.public_id.in_(selected_ids))
    pht = TZ("Asia/Manila")
    if start_date:
        start_utc = datetime.combine(start_date, datetime.min.time(), tzinfo=pht).astimezone(timezone.utc).replace(tzinfo=None)
        registration_query = registration_query.filter(User.created_at >= start_utc)
    if end_date:
        next_day = end_date + timedelta(days=1)
        end_utc = datetime.combine(next_day, datetime.min.time(), tzinfo=pht).astimezone(timezone.utc).replace(tzinfo=None)
        registration_query = registration_query.filter(User.created_at < end_utc)
    jobseekers = registration_query.all()
    
    if not jobseekers:
        flash("Selected job seekers could not be found.", "danger")
        return redirect(url_for('admin.jobseekers'))

    excel_file = generate_bulk_jsrf_excel(jobseekers)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"NSRP_JobSeekers_Masterlist_{timestamp}.xlsx"

    return send_file(
        excel_file,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename
    )

# Phase 1C.2C-7: PESO-verified account claim recovery.
@admin.route("/supervised-account-recovery", methods=["GET", "POST"])
@login_required
@role_required("admin")
@limiter.limit("10 per hour", methods=["POST"])
def supervised_account_recovery():
    """Reissue a private claim credential only after staff verifies identity in person."""
    import hashlib
    from website.models import SupervisedAccountClaim
    from website.utils.audit import record_audit
    from sqlalchemy.exc import IntegrityError

    claim_code = None
    reference_id = None
    if request.method == "POST":
        reference_id = (request.form.get("reference_id") or "").strip().upper()
        confirmed = request.form.get("identity_verified") == "yes"
        if not confirmed:
            flash("Verify the applicant's identity in person before reissuing a code.", "warning")
        elif not __import__("re").fullmatch(r"[A-F0-9]{16}", reference_id):
            flash("Invalid Reference ID format.", "warning")
        else:
            try:
                registration = (JobFairRegistration.query
                    .filter_by(reference_id=reference_id, registration_source="On-site")
                    .first())
                seeker = db.session.get(JobSeeker, registration.job_seeker_id) if registration else None
                user = db.session.get(User, seeker.user_id) if seeker else None
                if not user or user.role != "jobseeker" or not user.is_unclaimed or user.is_deleted:
                    flash("No eligible unclaimed on-site registration was found.", "warning")
                else:
                    existing = (SupervisedAccountClaim.query
                        .filter_by(user_id=user.user_id).with_for_update().first())
                    if existing and existing.used_at is not None:
                        flash("This claim credential has already been used. Investigate before proceeding.", "warning")
                    else:
                        claim_code = secrets.token_hex(16).upper()
                        now = datetime.utcnow()
                        if existing:
                            existing.reference_id = reference_id
                            existing.token_hash = hashlib.sha256(claim_code.encode("ascii")).hexdigest()
                            existing.created_at = now
                            existing.expires_at = now + timedelta(days=30)
                            existing.revoked_at = None
                            existing.used_at = None
                        else:
                            db.session.add(SupervisedAccountClaim(
                                user_id=user.user_id, reference_id=reference_id,
                                token_hash=hashlib.sha256(claim_code.encode("ascii")).hexdigest(),
                                created_at=now, expires_at=now + timedelta(days=30)))
                        record_audit(f"Reissued supervised account claim for user {user.public_id}")
                        db.session.commit()
            except Exception:
                db.session.rollback()
                claim_code = None
                current_app.logger.exception("Supervised claim reissue failed")
                flash("Unable to reissue a claim code. Please try again later.", "danger")
    response = make_response(render_template("admin/supervised_account_recovery.html",
                         claim_code=claim_code, reference_id=reference_id if claim_code else None))
    response.headers["Cache-Control"] = "no-store, no-cache, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@admin.route("/staff-password-recovery", methods=["GET", "POST"])
@login_required
@role_required("admin")
@limiter.limit("5 per hour", methods=["POST"])
def staff_password_recovery():
    # Retired Super Admin-only action. Never elevate every admin implicitly.
    abort(403, description="This retired privileged operation is unavailable.")
    """Only issue after independently checking physical identity in person."""
    import hashlib
    from website.models import StaffPasswordRecovery
    from sqlalchemy import update
    from sqlalchemy.exc import SQLAlchemyError

    credential = None
    if request.method == "POST":
        identifier = (request.form.get("identifier") or "").strip()
        verified = request.form.get("identity_verified") == "yes"
        if not verified or not identifier or len(identifier) > 100:
            flash("Identity verification and a valid applicant Login ID are required.", "warning")
        else:
            try:
                user = User.query.filter(db.func.lower(User.username) == identifier.lower()).first()
                if not user or user.role != "jobseeker" or user.is_unclaimed or user.is_deleted or user.status != "active":
                    flash("No eligible claimed jobseeker account was found.", "warning")
                else:
                    # Lock the account row to serialize issuance by concurrent staff.
                    locked = db.session.query(User).filter_by(user_id=user.user_id).with_for_update().one()
                    now = datetime.utcnow()
                    db.session.query(StaffPasswordRecovery).filter(
                        StaffPasswordRecovery.user_id == locked.user_id,
                        StaffPasswordRecovery.used_at.is_(None),
                        StaffPasswordRecovery.revoked_at.is_(None),
                    ).update({"revoked_at": now}, synchronize_session=False)
                    credential = secrets.token_urlsafe(32)
                    db.session.add(StaffPasswordRecovery(
                        user_id=locked.user_id, issued_by=current_user.user_id,
                        token_hash=hashlib.sha256(credential.encode("ascii")).hexdigest(),
                        created_at=now, expires_at=now + timedelta(minutes=15)))
                    db.session.add(AuditLog(user_id=current_user.user_id,
                        action=f"Staff password recovery issued for {locked.public_id}",
                        ip_address=request.remote_addr))
                    db.session.commit()
            except Exception:
                db.session.rollback()
                credential = None
                current_app.logger.exception("Staff password recovery issuance failed")
                flash("Unable to issue recovery credential.", "danger")
    response = make_response(render_template("admin/staff_password_recovery.html", credential=credential))
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
