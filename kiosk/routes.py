from datetime import datetime, timezone, date
import re

from flask import (
    abort,
    current_app,
    render_template,
    redirect,
    url_for,
    request,
    flash,
    jsonify
)

from flask_login import (
    login_required,
    current_user,
    logout_user
)

from sqlalchemy import or_

from website.kiosk.kiosk_queue_routes import (
    get_applied_job_ids_for_registration,
    get_queue_reference,
    is_job_expired
)

from . import kiosk

from website.extensions import db, limiter
from website.utils.job_fair_lifecycle import is_queue_entry_open
from website.utils.qrcode_gen import generate_qr_code

from website.models import (
    Job,
    Employer,
    JobFair,
    JobFairQueue,
    JobFairRegistration,
    JobFairEmployerRegistration,
    JobFairJob,
    JobFairJobPreselection,
    JobSeekerTVET,
    JobSeekerLicense
)



def _restore_finished_kiosk_assignment(kiosk_record):
    """Reset a kiosk when its assigned event has ended or been finished.

    Called on kiosk requests; no background worker is required. A kiosk that
    remains open without making requests will update on its next request.
    """
    if kiosk_record.mode not in ("job_fair", "queue_display") or not kiosk_record.job_fair_id:
        return
    fair = db.session.get(JobFair, kiosk_record.job_fair_id)
    from website.utils.job_fair_lifecycle import is_past_scheduled_end
    if fair is None or fair.status == "Finished" or is_past_scheduled_end(fair):
        kiosk_record.mode = "standby"
        kiosk_record.job_fair_id = None
        db.session.commit()

# ============================================================
# KIOSK ACCESS
# ============================================================

def kiosk_access_required():
    """
    Makes sure the currently logged-in account
    is an active kiosk account.

    This is used by actual kiosk functions.

    A disabled kiosk is intentionally NOT allowed
    to continue into the kiosk functions.
    """

    if not current_user.is_authenticated:
        abort(403)

    if current_user.role != "kiosk":
        abort(403)

    if not current_user.kiosk:
        abort(403)

    if current_user.kiosk.status != "active":
        abort(403)

    _restore_finished_kiosk_assignment(current_user.kiosk)


# ============================================================
# KIOSK ACCOUNT REQUIRED
# ============================================================

def kiosk_account_required():
    """
    Makes sure the currently logged-in account
    belongs to a kiosk.

    Unlike kiosk_access_required(), this does NOT
    require the kiosk to be active.

    This is used by the landing page so that a
    disabled kiosk can still display its
    'Unavailable' screen.
    """

    if not current_user.is_authenticated:
        abort(403)

    if current_user.role != "kiosk":
        abort(403)

    if not current_user.kiosk:
        abort(403)


# ============================================================
# GET KIOSK MODE
# ============================================================

def get_kiosk_mode():
    """
    Return the mode manually assigned to the kiosk.

    The kiosk mode is no longer determined by whether
    there is currently an ongoing Job Fair.

    Possible values:

        standby
        job_fair
        queue_display
    """

    if not current_user.is_authenticated:
        return "standby"

    if current_user.role != "kiosk":
        return "standby"

    if not current_user.kiosk:
        return "standby"

    return current_user.kiosk.mode or "standby"


# ============================================================
# GET ASSIGNED JOB FAIR
# ============================================================

def get_kiosk_job_fair():
    """
    Return the Job Fair manually assigned to the
    currently logged-in kiosk.

    The Job Fair is determined by:

        kiosk.job_fair_id

    NOT by the Job Fair's status.
    """

    if not current_user.is_authenticated:
        return None

    if current_user.role != "kiosk":
        return None

    if not current_user.kiosk:
        return None

    if not current_user.kiosk.job_fair_id:
        return None

    return (
        JobFair.query
        .filter(
            JobFair.job_fair_id ==
            current_user.kiosk.job_fair_id
        )
        .first()
    )


# ============================================================
# MAKE KIOSK MODE AVAILABLE TO ALL KIOSK TEMPLATES
# ============================================================

@kiosk.app_context_processor
def inject_kiosk_mode():

    try:

        if (
            current_user.is_authenticated
            and current_user.role == "kiosk"
            and current_user.kiosk
        ):

            kiosk_mode = (
                current_user.kiosk.mode
                or "standby"
            )

            kiosk_status = (
                current_user.kiosk.status
                or "active"
            )

            kiosk_job_fair = (
                get_kiosk_job_fair()
                if kiosk_mode in ("job_fair", "queue_display")
                else None
            )

            return {
                "kiosk_mode": kiosk_mode,
                "kiosk_status": kiosk_status,
                "ongoing_job_fair": kiosk_job_fair,
                "kiosk_job_fair": kiosk_job_fair
            }

        return {
            "kiosk_mode": "standby",
            "kiosk_status": "active",
            "ongoing_job_fair": None,
            "kiosk_job_fair": None
        }

    except Exception:

        current_app.logger.exception(
            "Unable to inject kiosk mode."
        )

        return {
            "kiosk_mode": "standby",
            "kiosk_status": "active",
            "ongoing_job_fair": None,
            "kiosk_job_fair": None
        }


@kiosk.route("/mode-status", methods=["GET"])
@login_required
@limiter.exempt  # Read-only kiosk navigation/display; keep transactional routes limited.
def kiosk_mode_status():
    """Read the assigned kiosk mode for the lightweight display watcher."""
    kiosk_access_required()
    _restore_finished_kiosk_assignment(current_user.kiosk)
    return jsonify({
        "mode": get_kiosk_mode(),
        "status": current_user.kiosk.status or "active",
    })


# ============================================================
# ENFORCE ADMIN-ASSIGNED KIOSK PURPOSE
# ============================================================

@kiosk.before_request
def enforce_kiosk_purpose():
    """Keep an active kiosk inside the purpose selected by PESO Admin.

    This is enforced server-side so browser Back, bookmarks, or manually
    entered kiosk URLs cannot escape the assigned operating mode.
    """
    if not current_user.is_authenticated or current_user.role != "kiosk" or not current_user.kiosk:
        return None

    # Logout must always remain reachable. Disabled kiosks are handled by
    # the dashboard's existing unavailable state.
    if request.endpoint == "kiosk.logout" or current_user.kiosk.status != "active":
        return None

    # Revalidate the assigned event before reading the mode. Otherwise an
    # ended Job Fair can retain stale access permissions for this request.
    _restore_finished_kiosk_assignment(current_user.kiosk)
    mode = get_kiosk_mode()
    endpoint = request.endpoint or ""

    homes = {
        "standby": "kiosk.jobsearch",
        "job_fair": "kiosk.registration",
        "queue_display": "kiosk.queue_display",
    }

    allowed = {
        "standby": {
            "kiosk.kiosk_mode_status",
            "kiosk.dashboard",
            "kiosk.jobsearch",
            "kiosk.qr_code_dashboard",  # Normal Job Search Apply Now -> web login QR
        },
        "job_fair": {
            "kiosk.kiosk_mode_status",
            "kiosk.dashboard",
            "kiosk.registration",
            "kiosk.jobsearch",  # Browse approved jobs within Job Fair mode
            "kiosk.personal_information",
            "kiosk.job_fair_jobs",
            "kiosk.preselect_job",
            "kiosk.queuing_number",
            "kiosk.create_queue",
            "kiosk.end_queue_participation",
            "kiosk.queuing_confirmation",
            "kiosk.qr_code_dashboard",
        },
        "queue_display": {
            "kiosk.kiosk_mode_status",
            "kiosk.dashboard",
            "kiosk.queue_display",
            "kiosk.queue_display_data",
        },
    }

    home = homes.get(mode, "kiosk.jobsearch")
    if endpoint not in allowed.get(mode, allowed["standby"]):
        return redirect(url_for(home))

    return None


@kiosk.after_request
def prevent_kiosk_history_cache(response):
    """Require kiosk history navigation to revalidate against the mode guard."""
    if current_user.is_authenticated and current_user.role == "kiosk":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ============================================================
# DASHBOARD
# ============================================================

@kiosk.route("/dashboard")
@login_required
@limiter.exempt  # Read-only kiosk navigation/display; keep transactional routes limited.
def dashboard():

    kiosk_account_required()

    kiosk = current_user.kiosk
    _restore_finished_kiosk_assignment(kiosk)

    # --------------------------------------------------------
    # DISABLED KIOSK
    # --------------------------------------------------------

    if kiosk.status != "active":

        return render_template(
            "kiosk/KioskLandingPage.html",

            kiosk_mode=kiosk.mode or "standby",

            kiosk_status=kiosk.status,

            ongoing_job_fair=(
                get_kiosk_job_fair()
                if kiosk.mode in ("job_fair", "queue_display")
                else None
            ),

            kiosk=kiosk
        )

    # --------------------------------------------------------
    # ACTIVE KIOSK
    # --------------------------------------------------------

    kiosk_mode = kiosk.mode or "standby"

    kiosk_job_fair = (
        get_kiosk_job_fair()
        if kiosk_mode in ("job_fair", "queue_display")
        else None
    )

    # Active kiosks open directly in the purpose selected by PESO Admin.
    # The landing page is retained only for disabled-kiosk messaging.
    if kiosk_mode == "standby":
        return redirect(url_for("kiosk.jobsearch"))

    if kiosk_mode == "job_fair":
        return redirect(url_for("kiosk.registration"))

    if kiosk_mode == "queue_display":
        return redirect(url_for("kiosk.queue_display"))

    return render_template(
        "kiosk/KioskLandingPage.html",

        kiosk_mode=kiosk_mode,

        kiosk_status=kiosk.status,

        ongoing_job_fair=kiosk_job_fair,

        kiosk=kiosk
    )



# ============================================================
# PUBLIC JOB FAIR QUEUE DISPLAY
# ============================================================

def build_public_queue_display(job_fair):
    """Build privacy-safe booth queue data for the public waiting-area display.

    Serving is derived from Active queues assigned to interview stations.
    Next uses the same skip/FIFO/busy-applicant eligibility used by Employer
    Call Next. Applicant names and registration/reference IDs are never exposed.
    """
    if not job_fair:
        return []

    registrations = (
        JobFairEmployerRegistration.query
        .filter_by(job_fair_id=job_fair.job_fair_id, status="Approved")
        .join(Employer, Employer.employer_id == JobFairEmployerRegistration.employer_id)
        .order_by(
            JobFairEmployerRegistration.booth_number.asc(),
            Employer.company_name.asc(),
            JobFairEmployerRegistration.registration_id.asc()
        )
        .all()
    )

    busy_registration_ids = db.session.query(
        JobFairQueue.job_fair_registration_id
    ).filter(
        JobFairQueue.job_fair_id == job_fair.job_fair_id,
        JobFairQueue.status == "Active",
        JobFairQueue.assigned_station_id.isnot(None)
    )

    booths = []
    for registration in registrations:
        serving = (
            JobFairQueue.query
            .filter_by(
                job_fair_id=job_fair.job_fair_id,
                employer_registration_id=registration.registration_id,
                status="Active"
            )
            .filter(JobFairQueue.assigned_station_id.isnot(None))
            .order_by(
                JobFairQueue.serving_started_at.asc(),
                JobFairQueue.queue_number.asc(),
                JobFairQueue.queue_group_id.asc()
            )
            .all()
        )

        next_queue = (
            JobFairQueue.query
            .filter(
                JobFairQueue.job_fair_id == job_fair.job_fair_id,
                JobFairQueue.employer_registration_id == registration.registration_id,
                JobFairQueue.status == "Active",
                JobFairQueue.assigned_station_id.is_(None),
                ~JobFairQueue.job_fair_registration_id.in_(busy_registration_ids)
            )
            .order_by(
                JobFairQueue.skip_count.asc(),
                JobFairQueue.queued_at.asc(),
                JobFairQueue.queue_number.asc(),
                JobFairQueue.queue_group_id.asc()
            )
            .first()
        )

        booths.append({
            "booth": registration.booth_number or "Unassigned",
            "employer": registration.employer.company_name if registration.employer else "Employer",
            "serving": [get_queue_reference(queue) for queue in serving],
            "next": get_queue_reference(next_queue) if next_queue else None,
        })

    return booths


@kiosk.route("/queue-display")
@login_required
@limiter.exempt  # Read-only kiosk navigation/display; keep transactional routes limited.
def queue_display():
    kiosk_access_required()
    if get_kiosk_mode() != "queue_display":
        flash("This kiosk is not assigned to Queue Display mode.", "warning")
        return redirect(url_for("kiosk.dashboard"))

    job_fair = get_kiosk_job_fair()
    if not job_fair:
        return render_template(
            "kiosk/QueueDisplayKiosk.html",
            job_fair=None,
            booths=[],
            display_unavailable_message="No Job Fair is currently assigned to this kiosk. Please ask an administrator to assign a Job Fair."
        )

    return render_template(
        "kiosk/QueueDisplayKiosk.html",
        job_fair=job_fair,
        booths=build_public_queue_display(job_fair)
    )


@kiosk.route("/queue-display/data")
@login_required
@limiter.exempt  # Read-only kiosk navigation/display; keep transactional routes limited.
def queue_display_data():
    kiosk_access_required()
    if get_kiosk_mode() != "queue_display":
        return jsonify({"success": False, "message": "Queue Display is unavailable."}), 409

    job_fair = get_kiosk_job_fair()
    if not job_fair:
        return jsonify({"success": False, "message": "No Job Fair is assigned to this kiosk."}), 404

    return jsonify({
        "success": True,
        "job_fair_id": job_fair.job_fair_id,
        "job_fair_title": job_fair.title,
        "booths": build_public_queue_display(job_fair),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds")
    })


def build_job_fair_registration_qr(job_fair):
    if not job_fair:
        return None, None
    path = url_for("jobseeker.job_fair_details", fair_id=job_fair.job_fair_id)
    public_base = (current_app.config.get("PUBLIC_BASE_URL") or "").rstrip("/")
    target_url = f"{public_base}{path}" if public_base else url_for("jobseeker.job_fair_details", fair_id=job_fair.job_fair_id, _external=True)
    return target_url, generate_qr_code(target_url)

# ============================================================
# QR CODE DASHBOARD
# ============================================================

@kiosk.route("/qr-code-dashboard")
@login_required
@limiter.exempt  # Read-only kiosk navigation/display; keep transactional routes limited.
def qr_code_dashboard():

    kiosk_access_required()

    kiosk_mode = get_kiosk_mode()

    kiosk_job_fair = (
        get_kiosk_job_fair()
        if kiosk_mode in ("job_fair", "queue_display")
        else None
    )

    # Normal job search must always display the web login QR, even when
    # another job fair exists. Fair-specific registration is a separate flow.
    source = (request.args.get("source") or "").strip().lower()
    regular_job_search = source == "jobsearch" or kiosk_mode == "standby"

    if regular_job_search:
        public_base = (current_app.config.get("PUBLIC_BASE_URL") or "").rstrip("/")
        login_path = url_for("auth.login")
        target_url = (f"{public_base}{login_path}" if public_base
                      else url_for("auth.login", _external=True))
        qr_image = generate_qr_code(target_url)
        kiosk_job_fair = None
    else:
        target_url, qr_image = build_job_fair_registration_qr(kiosk_job_fair)
        if not target_url or not qr_image:
            # A kiosk cannot issue an empty QR when no fair is assigned.
            abort(404)

    return render_template(
        "kiosk/Kiosk_qrpage.html",
        kiosk_mode=kiosk_mode,
        ongoing_job_fair=kiosk_job_fair,
        registration_url=target_url,
        registration_qr=qr_image,
    )


# ============================================================
# JOB SEARCH
# ============================================================
@kiosk.route("/job-search")
@login_required
@limiter.exempt
def jobsearch():
    kiosk_access_required()

    kiosk = current_user.kiosk
    kiosk_mode = kiosk.mode or "standby"

    kiosk_job_fair = (
        get_kiosk_job_fair()
        if kiosk_mode in ("job_fair", "queue_display")
        else None
    )

    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip()
    salary = request.args.get("salary", "").strip()
    work_type = request.args.get("work_type", "").strip()

    requested_tab = request.args.get("tab", "").strip().lower()
    if requested_tab not in ("all", "regular", "job-fair"):
        active_tab = "all"
    else:
        active_tab = requested_tab

    if kiosk_mode == "job_fair":
        active_tab = "all"

    query = (
        Job.query
        .join(
            Employer,
            Job.employer_id == Employer.employer_id
        )
        .filter(
            Job.status == "Open",
            Job.approval_status == "Approved"
        )
    )

    fair_job_ids = None
    if kiosk_mode == "job_fair":
        if kiosk_job_fair is None:
            query = query.filter(Job.job_id == -1)
        else:
            fair_job_ids = (
                db.session.query(JobFairJob.job_id)
                .join(
                    JobFairEmployerRegistration,
                    JobFairJob.registration_id == JobFairEmployerRegistration.registration_id,
                )
                .filter(
                    JobFairEmployerRegistration.job_fair_id == kiosk_job_fair.job_fair_id,
                    JobFairEmployerRegistration.status == "Approved",
                )
            )
            query = query.filter(Job.job_id.in_(fair_job_ids))
    elif active_tab == "job-fair":
        fair_job_ids = (
            db.session.query(JobFairJob.job_id)
            .join(
                JobFairEmployerRegistration,
                JobFairJob.registration_id == JobFairEmployerRegistration.registration_id,
            )
            .filter(JobFairEmployerRegistration.status == "Approved")
        )
        query = query.filter(Job.job_id.in_(fair_job_ids))

    if kiosk_mode != "job_fair" and active_tab == "regular":
        query = query.filter(Job.job_type == "Referral")

    if search:
        search_term = f"%{search}%"
        query = query.filter(
            or_(
                Job.job_title.ilike(search_term),
                Job.category.ilike(search_term),
                Job.location.ilike(search_term),
                Employer.company_name.ilike(search_term)
            )
        )

    if category:
        query = query.filter(Job.category == category)

    if work_type:
        query = query.filter(Job.employment_type == work_type)

    if salary == "below-15000":
        query = query.filter(Job.salary_max < 15000)
    elif salary == "15000-25000":
        query = query.filter(
            Job.salary_max >= 15000,
            Job.salary_min <= 25000
        )
    elif salary == "25000-40000":
        query = query.filter(
            Job.salary_max >= 25000,
            Job.salary_min <= 40000
        )
    elif salary == "above-40000":
        query = query.filter(Job.salary_max >= 40000)

    jobs = (
        query
        .order_by(
            Job.created_at.desc(),
            Job.vacancies.desc(),
            Job.job_id.desc()
        )
        .all()
    )

    if kiosk_mode == "job_fair" or active_tab == "job-fair":
        jobs = [job for job in jobs if not is_job_expired(job)]

    category_query = (
        Job.query
        .join(
            Employer,
            Job.employer_id == Employer.employer_id
        )
        .filter(
            Job.status == "Open",
            Job.approval_status == "Approved",
            Job.category.isnot(None),
            Job.category != ""
        )
    )

    if kiosk_mode == "job_fair":
        if kiosk_job_fair is None:
            category_query = category_query.filter(Job.job_id == -1)
        else:
            category_query = category_query.filter(Job.job_id.in_(fair_job_ids))
    elif active_tab == "job-fair":
        category_query = category_query.filter(Job.job_id.in_(fair_job_ids))

    if active_tab == "regular":
        category_query = category_query.filter(Job.job_type == "Referral")

    categories = (
        category_query
        .with_entities(Job.category)
        .distinct()
        .order_by(Job.category)
        .all()
    )

    categories = [item[0] for item in categories]

    current_app.logger.info(
        "Kiosk Job Search: mode=%s tab=%s search=%s category=%s salary=%s work_type=%s jobs=%s",
        kiosk_mode,
        active_tab,
        search,
        category,
        salary,
        work_type,
        len(jobs)
    )

    return render_template(
        "kiosk/JobSearchKiosk.html",
        jobs=jobs,
        search=search,
        category=category,
        salary=salary,
        work_type=work_type,
        categories=categories,
        active_tab=active_tab,
        kiosk_mode=kiosk_mode,
        ongoing_job_fair=kiosk_job_fair
    )


# ============================================================
# JOB FAIR REGISTRATION
# ============================================================
@kiosk.route("/registration", methods=["GET", "POST"])
@login_required
def registration():
    kiosk_access_required()

    kiosk = current_user.kiosk

    if kiosk.mode != "job_fair":
        flash(
            "Job Fair registration is currently unavailable.",
            "error"
        )
        return redirect(
            url_for("kiosk.dashboard")
        )

    ongoing_job_fair = get_kiosk_job_fair()

    if not ongoing_job_fair:
        flash(
            "No Job Fair has been assigned to this kiosk.",
            "error"
        )
        return redirect(
            url_for("kiosk.dashboard")
        )

    if request.method == "POST":
        reference_number = request.form.get(
            "referenceNumber",
            ""
        ).strip().upper()

        if not reference_number:
            flash(
                "Please enter your reference number.",
                "error"
            )
            return redirect(
                url_for("kiosk.registration")
            )

        registration = (
            JobFairRegistration.query
            .filter(
                JobFairRegistration.reference_id ==
                reference_number
            )
            .first()
        )

        if not registration:
            flash(
                "Reference number not found. "
                "Please check your reference number and try again.",
                "error"
            )
            return redirect(
                url_for("kiosk.registration")
            )

        job_seeker = registration.job_seeker

        if not job_seeker:
            flash(
                "This registration does not have a valid applicant record.",
                "error"
            )
            return redirect(
                url_for("kiosk.registration")
            )

        if (
            registration.job_fair_id
            != ongoing_job_fair.job_fair_id
        ):
            flash(
                "This reference number does not belong "
                "to the Job Fair assigned to this kiosk.",
                "error"
            )
            return redirect(
                url_for("kiosk.registration")
            )

        has_existing_queue = db.session.query(JobFairQueue.queue_group_id).filter(
            JobFairQueue.job_fair_id == ongoing_job_fair.job_fair_id,
            JobFairQueue.job_fair_registration_id == registration.registration_id,
        ).first() is not None
        if has_existing_queue:
            return redirect(url_for(
                "kiosk.job_fair_jobs", reference_id=reference_number
            ))

        return redirect(
            url_for(
                "kiosk.personal_information",
                reference_id=reference_number
            )
        )

    return render_template(
        "kiosk/RegistrationKiosk.html",
        ongoing_job_fair=ongoing_job_fair,
        registration_url=build_job_fair_registration_qr(ongoing_job_fair)[0],
        registration_qr=build_job_fair_registration_qr(ongoing_job_fair)[1]
    )

# ============================================================
# PERSONAL INFORMATION
# ============================================================

@kiosk.route(
    "/personal-information/<reference_id>"
)
@login_required
def personal_information(reference_id):

    kiosk_access_required()

    kiosk = current_user.kiosk

    if kiosk.mode != "job_fair":

        flash(
            "Job Fair registration is currently unavailable.",
            "error"
        )

        return redirect(
            url_for("kiosk.dashboard")
        )

    reference_id = reference_id.strip().upper()

    registration = (
        JobFairRegistration.query
        .filter(
            JobFairRegistration.reference_id ==
            reference_id
        )
        .first()
    )

    if not registration:

        flash(
            "Invalid or expired reference number.",
            "error"
        )

        return redirect(
            url_for("kiosk.registration")
        )

    kiosk_job_fair = get_kiosk_job_fair()

    if not kiosk_job_fair:

        flash(
            "No Job Fair has been assigned to this kiosk.",
            "error"
        )

        return redirect(
            url_for("kiosk.dashboard")
        )

    if (
        registration.job_fair_id
        != kiosk_job_fair.job_fair_id
    ):

        flash(
            "This registration does not belong "
            "to the Job Fair assigned to this kiosk.",
            "error"
        )

        return redirect(
            url_for("kiosk.registration")
        )

    job_seeker = registration.job_seeker

    if not job_seeker:

        flash(
            "Applicant information could not be found.",
            "error"
        )

        return redirect(
            url_for("kiosk.registration")
        )

    today = date.today()

    age = None

    if job_seeker.birthdate:

        age = (
            today.year
            - job_seeker.birthdate.year
            - (
                (today.month, today.day)
                <
                (
                    job_seeker.birthdate.month,
                    job_seeker.birthdate.day
                )
            )
        )

    certifications = (
        JobSeekerTVET.query
        .filter_by(
            job_seeker_id=
                job_seeker.job_seeker_id
        )
        .all()
    )

    licenses = (
        JobSeekerLicense.query
        .filter_by(
            job_seeker_id=
                job_seeker.job_seeker_id
        )
        .all()
    )

    return render_template(
        "kiosk/KioskPersonalInformation.html",

        registration=registration,

        job_seeker=job_seeker,

        certifications=certifications,

        licenses=licenses,

        age=age,

        ongoing_job_fair=kiosk_job_fair
    )


# ============================================================
# JOB FAIR JOB LIST
# ============================================================
#
# IMPORTANT:
#
# This page does NOT use Job.job_type to determine whether
# a job belongs to the Job Fair.
#
# Job Fair membership is determined by:
#
#     JobFairEmployerRegistration
#              +
#         JobFairJob
#
# Therefore BOTH of these can appear:
#
#     Job.job_type == "Job Fair"
#     Job.job_type == "Referral"
#
# as long as the job was actually selected/included in
# the current Job Fair.
#
# ============================================================

@kiosk.route(
    "/job-fair-jobs/<reference_id>"
)
@login_required
def job_fair_jobs(reference_id):

    kiosk_access_required()

    kiosk = current_user.kiosk

    if kiosk.mode != "job_fair":

        flash(
            "Job Fair mode is currently unavailable.",
            "error"
        )

        return redirect(
            url_for("kiosk.dashboard")
        )

    reference_id = reference_id.strip().upper()

    registration = (
        JobFairRegistration.query
        .filter(
            JobFairRegistration.reference_id ==
            reference_id
        )
        .first()
    )

    if not registration:

        flash(
            "Invalid or expired reference number.",
            "error"
        )

        return redirect(
            url_for("kiosk.registration")
        )

    kiosk_job_fair = get_kiosk_job_fair()

    if not kiosk_job_fair:

        flash(
            "No Job Fair has been assigned to this kiosk.",
            "error"
        )

        return redirect(
            url_for("kiosk.dashboard")
        )

    if (
        registration.job_fair_id
        != kiosk_job_fair.job_fair_id
    ):

        flash(
            "Your registration is not for the Job Fair "
            "assigned to this kiosk.",
            "error"
        )

        return redirect(
            url_for("kiosk.registration")
        )

    # ========================================================
    # APPLIED JOBS
    # ========================================================

    applied_job_ids = (
        get_applied_job_ids_for_registration(
            registration
        )
    )

    # ========================================================
    # GET ALL JOBS INCLUDED IN THIS JOB FAIR
    # ========================================================
    #
    # IMPORTANT:
    #
    # DO NOT FILTER BY:
    #
    #     Job.job_type
    #
    # A Referral job can also be included in a Job Fair.
    #
    # The actual Job Fair membership comes from:
    #
    #     JobFairEmployerRegistration
    #             +
    #          JobFairJob
    #
    # ========================================================

    job_fair_jobs = (
        JobFairJob.query

        # Connect JobFairJob to the employer's
        # Job Fair registration.
        .join(
            JobFairEmployerRegistration,
            JobFairJob.registration_id ==
            JobFairEmployerRegistration.registration_id
        )

        # Connect JobFairJob to the actual Job.
        .join(
            Job,
            JobFairJob.job_id ==
            Job.job_id
        )

        .filter(

            # The JobFairJob must belong to
            # the Job Fair assigned to this kiosk.
            JobFairEmployerRegistration.job_fair_id ==
            kiosk_job_fair.job_fair_id,

            # Employer registration must be approved.
            JobFairEmployerRegistration.status ==
            "Approved",

            # The actual Job must currently be open.
            Job.status ==
            "Open",

            # The actual Job must be approved.
            Job.approval_status ==
            "Approved"
        )

        .order_by(
            Job.created_at.desc(),
            Job.vacancies.desc(),
            Job.job_id.desc()
        )

        .all()
    )

    # ========================================================
    # REMOVE EXPIRED JOBS
    # ========================================================

    available_job_fair_jobs = []

    for fair_job in job_fair_jobs:

        job = fair_job.job

        if not job:
            continue

        # Keep deadline validation for the Job Fair
        # selection page.
        if is_job_expired(job):
            continue

        available_job_fair_jobs.append(
            fair_job
        )

    job_fair_jobs = available_job_fair_jobs

    # ========================================================
    # GET PRESELECTION RECORDS
    # ========================================================

    preselection_records = (
        JobFairJobPreselection.query
        .filter_by(
            job_fair_registration_id=
                registration.registration_id
        )
        .order_by(
            JobFairJobPreselection.preselected_at.asc(),

            JobFairJobPreselection.preselection_id.asc()
        )
        .all()
    )

    # ========================================================
    # GET AVAILABLE JOB FAIR JOB IDS
    # ========================================================

    available_job_fair_job_ids = {
        fair_job.job_fair_job_id
        for fair_job in job_fair_jobs
    }

    # ========================================================
    # GET PRESELECTED JOBS
    # ========================================================

    preselected_job_fair_job_ids = [

        record.job_fair_job_id

        for record in preselection_records

        if (
            record.job_fair_job_id is not None

            and

            record.job_fair_job_id
            in available_job_fair_job_ids
        )
    ]

    # ========================================================
    # GET APPLIED JOB FAIR JOB IDS
    # ========================================================

    applied_job_fair_job_ids = []

    for fair_job in job_fair_jobs:

        if fair_job.job_id in applied_job_ids:

            applied_job_fair_job_ids.append(
                fair_job.job_fair_job_id
            )

    # ========================================================
    # REMOVE APPLIED JOBS FROM PRESELECTION ORDER
    # ========================================================

    preselected_job_fair_job_ids = [

        job_fair_job_id

        for job_fair_job_id
        in preselected_job_fair_job_ids

        if job_fair_job_id
        not in applied_job_fair_job_ids
    ]

    # ========================================================
    # PRESELECTION ORDER
    # ========================================================

    preselection_order = {

        job_fair_job_id: index

        for index, job_fair_job_id

        in enumerate(
            preselected_job_fair_job_ids
        )
    }

    # ========================================================
    # SORT JOBS
    #
    # Preselected jobs appear first.
    # Remaining jobs are ordered newest first.
    # ========================================================

    job_fair_jobs.sort(
        key=lambda fair_job: (

            (
                0,

                preselection_order[
                    fair_job.job_fair_job_id
                ]
            )

            if fair_job.job_fair_job_id
            in preselection_order

            else (

                1,

                -(
                    fair_job.job.created_at.timestamp()

                    if fair_job.job.created_at

                    else 0
                )
            )
        )
    )

    # ========================================================
    # RENDER
    # ========================================================

    return render_template(
        "kiosk/JobFairJobView.html",

        registration=registration,

        job_seeker=registration.job_seeker,

        ongoing_job_fair=kiosk_job_fair,

        job_fair_jobs=job_fair_jobs,

        preselected_job_fair_job_ids=
            preselected_job_fair_job_ids,

        applied_job_ids=
            list(applied_job_ids),

        applied_job_fair_job_ids=
            applied_job_fair_job_ids,

        preselect_url=url_for(
            "kiosk.preselect_job",
            reference_id=reference_id
        )
    )


# ============================================================
# PRESELECT / REMOVE JOB FAIR JOB
# ============================================================

@kiosk.route(
    "/job-fair-jobs/<reference_id>/preselect",
    methods=["POST"]
)
@login_required
def preselect_job(reference_id):

    kiosk_access_required()

    kiosk = current_user.kiosk

    if kiosk.mode != "job_fair":

        return jsonify({
            "success": False,
            "message":
                "Job Fair mode is currently unavailable."
        }), 403

    reference_id = reference_id.strip().upper()

    current_app.logger.info(
        "Kiosk preselection request: reference=%s",
        reference_id
    )

    registration = (
        JobFairRegistration.query
        .filter(
            JobFairRegistration.reference_id ==
            reference_id
        )
        .first()
    )

    if not registration:

        return jsonify({
            "success": False,
            "message": "Invalid registration."
        }), 404

    kiosk_job_fair = get_kiosk_job_fair()

    if not kiosk_job_fair:

        return jsonify({
            "success": False,
            "message":
                "No Job Fair has been assigned to this kiosk."
        }), 400

    if (
        registration.job_fair_id
        != kiosk_job_fair.job_fair_id
    ):

        return jsonify({
            "success": False,
            "message": (
                "This registration does not belong "
                "to the Job Fair assigned to this kiosk."
            )
        }), 403

    # ========================================================
    # GET JOB FAIR JOB ID
    # ========================================================

    data = request.get_json(
        silent=True
    )

    if data:

        job_fair_job_id = data.get(
            "job_fair_job_id"
        )

    else:

        job_fair_job_id = request.form.get(
            "job_fair_job_id"
        )

    current_app.logger.info(
        "Received job_fair_job_id=%s",
        job_fair_job_id
    )

    if job_fair_job_id is None:

        return jsonify({
            "success": False,
            "message": "No job was specified."
        }), 400

    # Apply the same strict positive-ID rules as kiosk queue creation.
    if isinstance(job_fair_job_id, bool) or not (
        isinstance(job_fair_job_id, int)
        or (isinstance(job_fair_job_id, str) and re.fullmatch(r"[0-9]+", job_fair_job_id))
    ):
        return jsonify({
            "success": False,
            "message": "Invalid job ID."
        }), 400

    try:
        job_fair_job_id = int(job_fair_job_id)
        if job_fair_job_id <= 0:
            raise ValueError("Job ID must be positive")

    except (TypeError, ValueError):

        return jsonify({
            "success": False,
            "message": "Invalid job ID."
        }), 400

    # ========================================================
    # APPLIED JOBS
    # ========================================================

    applied_job_ids = (
        get_applied_job_ids_for_registration(
            registration
        )
    )

    # ========================================================
    # FIND JOB FAIR JOB
    # ========================================================
    #
    # IMPORTANT:
    #
    # DO NOT FILTER BY Job.job_type.
    #
    # A Referral job is allowed here if it was included
    # in this specific Job Fair through JobFairJob.
    #
    # ========================================================

    job_fair_job = (
        JobFairJob.query

        # Connect to the employer's Job Fair registration.
        .join(
            JobFairEmployerRegistration,
            JobFairJob.registration_id ==
            JobFairEmployerRegistration.registration_id
        )

        # Connect to the actual Job.
        .join(
            Job,
            JobFairJob.job_id ==
            Job.job_id
        )

        .filter(

            # Exact JobFairJob selected by the kiosk.
            JobFairJob.job_fair_job_id ==
            job_fair_job_id,

            # It must belong to this kiosk's Job Fair.
            JobFairEmployerRegistration.job_fair_id ==
            kiosk_job_fair.job_fair_id,

            # Employer must be approved for the fair.
            JobFairEmployerRegistration.status ==
            "Approved",

            # Job must be open.
            Job.status ==
            "Open",

            # Job must be approved.
            Job.approval_status ==
            "Approved"
        )

        .first()
    )

    # ========================================================
    # JOB FAIR JOB NOT FOUND
    # ========================================================

    if not job_fair_job:

        current_app.logger.warning(
            "Kiosk preselection rejected: "
            "job_fair_job_id=%s not available.",
            job_fair_job_id
        )

        return jsonify({
            "success": False,
            "message":
                "This job is no longer available."
        }), 404

    # ========================================================
    # PHASE 1D: QUEUE ENTRY / JOB SELECTION CUTOFF
    # ========================================================

    if not is_queue_entry_open(kiosk_job_fair):
        return jsonify({
            "success": False,
            "message": "Queue entry for this Job Fair has closed. You may continue with existing queues, but cannot add or remove job selections."
        }), 403

    # ========================================================
    # GET ACTUAL JOB
    # ========================================================

    job = job_fair_job.job

    if not job:

        return jsonify({
            "success": False,
            "message":
                "This job is no longer available."
        }), 404

    # ========================================================
    # DEADLINE CHECK
    # ========================================================

    if is_job_expired(job):

        return jsonify({
            "success": False,
            "message":
                "This job has already passed its deadline."
        }), 400

    # ========================================================
    # ALREADY APPLIED CHECK
    # ========================================================

    if job.job_id in applied_job_ids:

        return jsonify({
            "success": False,

            "selected": False,

            "already_applied": True,

            "message":
                "You have already applied to this job."
        }), 409

    # ========================================================
    # CHECK EXISTING PRESELECTION
    # ========================================================

    preselection = (
        JobFairJobPreselection.query
        .filter_by(

            job_fair_registration_id=
                registration.registration_id,

            job_fair_job_id=
                job_fair_job.job_fair_job_id
        )
        .first()
    )

    # ========================================================
    # REMOVE EXISTING PRESELECTION
    # ========================================================

    if preselection:

        try:

            db.session.delete(
                preselection
            )

            db.session.commit()

        except Exception:

            db.session.rollback()

            current_app.logger.exception(
                "Failed to remove kiosk job fair preselection."
            )

            return jsonify({
                "success": False,
                "message":
                    "Unable to remove preselection."
            }), 500

        current_app.logger.info(
            "Kiosk preselection removed: "
            "registration=%s, job_fair_job=%s",
            registration.registration_id,
            job_fair_job.job_fair_job_id
        )

        return jsonify({

            "success": True,

            "selected": False,

            "message":
                "Job removed from your preselected jobs.",

            "job_fair_job_id":
                job_fair_job.job_fair_job_id

        }), 200

    # ========================================================
    # CREATE PRESELECTION
    # ========================================================

    preselection = JobFairJobPreselection(

        job_fair_registration_id=
            registration.registration_id,

        job_fair_job_id=
            job_fair_job.job_fair_job_id,

        preselected_at=
            datetime.now(timezone.utc)
    )

    try:

        db.session.add(
            preselection
        )

        db.session.commit()

    except Exception:

        db.session.rollback()

        current_app.logger.exception(
            "Failed to create kiosk job fair preselection."
        )

        return jsonify({
            "success": False,
            "message":
                "Unable to preselect this job."
        }), 500

    current_app.logger.info(
        "Kiosk preselection created: "
        "registration=%s, job_fair_job=%s",
        registration.registration_id,
        job_fair_job.job_fair_job_id
    )

    return jsonify({

        "success": True,

        "selected": True,

        "message":
            "Job added to your preselected jobs.",

        "preselection_id":
            preselection.preselection_id,

        "job_fair_job_id":
            preselection.job_fair_job_id,

        "preselected_at":
            preselection.preselected_at.isoformat()

    }), 200


# ============================================================
# KIOSK LOGOUT
# ============================================================

@kiosk.route(
    "/logout",
    methods=["POST"]
)
@login_required
def logout():

    # --------------------------------------------------------
    # ONLY KIOSK ACCOUNTS MAY USE THIS ROUTE
    # --------------------------------------------------------

    if current_user.role != "kiosk":
        abort(403)

    # --------------------------------------------------------
    # GET PASSWORD FROM REQUEST
    # --------------------------------------------------------

    data = request.get_json(
        silent=True
    )

    if data is not None:

        password = (
            data.get("password", "")
            if isinstance(data, dict)
            else ""
        )

    else:

        password = request.form.get(
            "password",
            ""
        )

    # JSON clients can submit arrays, numbers or null; these must not crash logout.
    if not isinstance(password, str):
        return jsonify({
            "success": False,
            "message": "Invalid password format."
        }), 400

    password = password.strip()

    # --------------------------------------------------------
    # PASSWORD REQUIRED
    # --------------------------------------------------------

    if not password:

        return jsonify({
            "success": False,
            "message":
                "Please enter your password."
        }), 400

    # --------------------------------------------------------
    # VERIFY USING THE EXISTING USER PASSWORD
    # --------------------------------------------------------

    if not current_user.check_password(password):

        current_app.logger.warning(
            "Invalid kiosk logout password attempt "
            "for user_id=%s.",
            current_user.user_id
        )

        return jsonify({
            "success": False,
            "message":
                "Incorrect password."
        }), 401

    # --------------------------------------------------------
    # PASSWORD CORRECT
    # --------------------------------------------------------

    current_app.logger.info(
        "Kiosk user_id=%s logged out successfully.",
        current_user.user_id
    )

    logout_user()

    return jsonify({
        "success": True,
        "redirect_url": url_for("auth.login")
    }), 200