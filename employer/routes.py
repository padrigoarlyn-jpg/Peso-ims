import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import urljoin, urlparse

from flask import (
    abort,
    current_app,
    jsonify,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required, logout_user
from flask_mail import Message
from werkzeug.utils import secure_filename
from website.utils.job_matching import calculate_job_match
from website.extensions import db, mail
from website.models import (
    Application,
    Employer,
    EmployerRegistrationRequest,
    Job,
    JobFairCandidateEvaluation,
    JobSeeker,
    JobSeekerLanguage,
    JobSeekerLicense,
    JobSeekerTVET,
    JobSeekerWorkHistory,
    Referral,
)
from website.utils.decorators import role_required
from website.utils.email import send_email
from website.utils.notifications import notify_admins, notify_user, commit_notifications
from website.utils.audit import record_audit
from website.utils.application_ranking import rank_job_applications
from website.utils.uploads import save_validated_image
from website.utils.application_state import (
    mark_application_interview,
    mark_application_rejected,
    mark_application_accepted,
    validate_hire_transition,
    sync_jobseeker_employment_status,
)
from website.utils.referral_flow import (
    get_official_referral,
    lock_job_for_hiring,
    validate_hiring_capacity_locked,
)
from . import employer
from .forms import CompanyForm, JobForm

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def _safe_local_redirect(candidate, fallback):
    """Return only a same-host http(s) redirect target."""
    if not candidate:
        return fallback
    try:
        host = urlparse(request.host_url)
        target = urlparse(urljoin(request.host_url, candidate))
        if target.scheme in {"http", "https"} and target.netloc == host.netloc:
            return target.geturl()
    except (TypeError, ValueError):
        pass
    return fallback


def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def get_application(application_id):
    employer_info = Employer.query.filter_by(user_id=current_user.user_id).first()
    if employer_info is None:
        abort(403)

    application = Application.query.get_or_404(application_id)
    if application.job is None:
        abort(404)

    if application.job.employer_id != employer_info.employer_id:
        abort(403)

    referral = get_official_referral(
        application.application_id
    )

    job_fair_evaluation = JobFairCandidateEvaluation.query.filter_by(
        application_id=application.application_id
    ).first()

    if referral is None and job_fair_evaluation is None:
        abort(403)

    return application


def get_job_vacancy_info(job):
    accepted_count = Application.query.filter_by(
        job_id=job.job_id, status="Accepted"
    ).count()

    remaining_vacancies = max(job.vacancies - accepted_count, 0)
    return accepted_count, remaining_vacancies


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


# ============================================================
# DASHBOARD
# ============================================================

@employer.route("/dashboard")
@login_required
@role_required("employer")
def dashboard():
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_obj = Employer.query.filter_by(user_id=current_user.user_id).first()

    if employer_obj is None:
        flash(
            "Your employer profile could not be found. Please contact the administrator.",
            "danger",
        )
        return redirect(url_for("auth.login"))

    # Use the verified request's representative, never an unrelated account's name.
    # Match both the registered company and its email to avoid cross-company greetings.
    registration = EmployerRegistrationRequest.query.filter(
        db.func.lower(EmployerRegistrationRequest.email) == (employer_obj.company_email or "").strip().lower(),
        db.func.lower(EmployerRegistrationRequest.company_name) == (employer_obj.company_name or "").strip().lower(),
        EmployerRegistrationRequest.status == "Completed",
    ).order_by(EmployerRegistrationRequest.request_id.desc()).first()
    representative_name = (
        (registration.representative_name or "").strip() if registration else ""
    )
    welcome_name = representative_name or employer_obj.company_name or "Employer"

    jobs = Job.query.filter_by(employer_id=employer_obj.employer_id).all()

    active_jobs = Job.query.filter_by(
        employer_id=employer_obj.employer_id, status="Open"
    ).count()

    referred_exists = (
        db.session.query(Referral.referral_id)
        .filter(
            Referral.application_id == Application.application_id,
            Referral.status == "Referred",
        )
        .exists()
    )

    evaluation_exists = (
        db.session.query(JobFairCandidateEvaluation.evaluation_id)
        .filter(
            JobFairCandidateEvaluation.application_id == Application.application_id
        )
        .exists()
    )

    applications = (
        Application.query.join(Job)
        .filter(
            Job.employer_id == employer_obj.employer_id,
            db.or_(referred_exists, evaluation_exists),
        )
        .order_by(Application.application_date.desc())
        .all()
    )

    pending_applications = sum(
        application.status in ["Pending", "Reviewed", "Interview"]
        for application in applications
    )

    accepted_applications = sum(
        application.status == "Accepted" for application in applications
    )

    rejected_applications = sum(
        application.status == "Rejected" for application in applications
    )

    recent_applications = applications[:3]
    current_month = datetime.utcnow().replace(day=1)

    chart_labels = []
    chart_data = []

    for month_offset in range(5, -1, -1):
        month = current_month.month - month_offset
        year = current_month.year

        while month <= 0:
            month += 12
            year -= 1

        next_month = month + 1
        next_year = year

        if next_month == 13:
            next_month = 1
            next_year += 1

        chart_labels.append(datetime(year, month, 1).strftime("%b"))

        chart_data.append(
            sum(
                application.application_date
                and application.application_date.year == year
                and application.application_date.month == month
                for application in applications
            )
        )

    recent_jobs = (
        Job.query.filter_by(employer_id=employer_obj.employer_id)
        .order_by(Job.created_at.desc())
        .limit(3)
        .all()
    )

    return render_template(
        "employer/employer_dashboard.html",
        current_user=current_user,
        employer=employer_obj,
        welcome_name=welcome_name,
        jobs=jobs,
        active_jobs=active_jobs,
        recent_jobs=recent_jobs,
        recent_applications=recent_applications,
        pending_applications=pending_applications,
        accepted_applications=accepted_applications,
        rejected_applications=rejected_applications,
        chart_labels=chart_labels,
        chart_data=chart_data,
    )


# ============================================================
# PROFILE
# ============================================================

@employer.route("/profile", methods=["GET", "POST"])
@login_required
@role_required("employer")
def profile():
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_obj = Employer.query.filter_by(user_id=current_user.user_id).first()

    if employer_obj is None:
        flash(
            "Your employer profile could not be found. Please contact the administrator.",
            "danger",
        )
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        allowed_actions = {
            "website", "update_website", "summary", "update_summary",
            "contact", "update_contact", "details", "update_details",
            "social", "update_social", "upload_logo", "media",
            "update_media", "update_gallery",
        }
        if action not in allowed_actions:
            abort(400, description="Unsupported employer profile update action.")

        # --------------------------------------------------------
        # UPDATE WEBSITE
        # --------------------------------------------------------
        if action in ["website", "update_website"]:
            from urllib.parse import urlsplit
            website = (request.form.get("website") or "").strip()
            if len(website) > 255:
                flash("Website URL cannot exceed 255 characters.", "danger")
                return redirect(url_for("employer.profile"))
            if website:
                parsed = urlsplit(website)
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                        or any(ch.isspace() for ch in website)
                        or parsed.username or parsed.password):
                    flash("Enter a valid website URL beginning with https:// or http://.", "danger")
                    return redirect(url_for("employer.profile"))
            employer_obj.website = website or None

        # --------------------------------------------------------
        # UPDATE SUMMARY
        # --------------------------------------------------------
        elif action in ["summary", "update_summary"]:
            summary = (request.form.get("summary") or "").strip()
            if len(summary) > 5000:
                flash("Company summary cannot exceed 5,000 characters.", "danger")
                return redirect(url_for("employer.profile"))
            employer_obj.company_description = summary or None

        # --------------------------------------------------------
        # UPDATE CONTACT & ADDRESS
        # --------------------------------------------------------
        elif action in ["contact", "update_contact"]:
            address_fields = {"street": 255, "barangay": 100, "city": 100, "province": 100}
            validated_address = {}
            for field, limit in address_fields.items():
                value = (request.form.get(field) or "").strip()
                if len(value) > limit:
                    flash(f"{field.replace('_', ' ').title()} cannot exceed {limit} characters.", "danger")
                    return redirect(url_for("employer.profile"))
                validated_address[field] = value or None

            # Apply only after all address fields pass validation.
            for field, value in validated_address.items():
                setattr(employer_obj, field, value)

            phone = request.form.get("phone") or request.form.get("company_phone")

            if phone:
                phone = phone.strip()
                if re.fullmatch(r"[0-9+\-\s()]{7,20}", phone) and 7 <= sum(c.isdigit() for c in phone) <= 15:
                    employer_obj.company_phone = phone
                    if hasattr(employer_obj, "phone"):
                        employer_obj.phone = phone
                else:
                    flash(
                        "Invalid contact number format. Please enter a valid number (7 to 15 digits).",
                        "danger",
                    )
                    return redirect(url_for("employer.profile"))

            map_input = (
                request.form.get("map_link")
                or request.form.get("google_map_url")
                or request.form.get("google_maps_link")
            )

            if map_input:
                map_input = map_input.strip()

                # --------------------------------------------------------
                # ACCEPT NORMAL GOOGLE MAPS LINK OR EMBED CODE
                # --------------------------------------------------------
                if "<iframe" in map_input.lower():
                    # Extract the src="" from a Google Maps iframe
                    iframe_match = re.search(
                        r'<iframe[^>]+src=["\']([^"\']+)["\']',
                        map_input,
                        re.IGNORECASE,
                    )

                    if iframe_match:
                        map_url = iframe_match.group(1).strip()
                    else:
                        flash(
                            "Invalid Google Maps embed code. Please paste the complete iframe embed code.",
                            "danger",
                        )
                        return redirect(url_for("employer.profile"))
                else:
                    # Normal Google Maps URL
                    map_url = map_input

                # --------------------------------------------------------
                # SAVE THE EXTRACTED/NORMALIZED MAP URL
                # --------------------------------------------------------
                if hasattr(employer_obj, "google_map_url"):
                    employer_obj.google_map_url = map_url

                if hasattr(employer_obj, "google_maps_link"):
                    employer_obj.google_maps_link = map_url

                if hasattr(employer_obj, "map_link"):
                    employer_obj.map_link = map_url

        # --------------------------------------------------------
        # UPDATE COMPANY DETAILS
        # --------------------------------------------------------
        elif action in ["details", "update_details"]:
            employer_obj.founded_year = (
                request.form.get("founded")
                or request.form.get("founded_year")
                or employer_obj.founded_year
            )

            industry_val = request.form.get("industry") or request.form.get("company_industry")

            if industry_val:
                employer_obj.company_industry = industry_val
                if hasattr(employer_obj, "industry"):
                    employer_obj.industry = industry_val

            size_val = request.form.get("size") or request.form.get("company_size")
            if size_val:
                employer_obj.company_size = size_val

        # --------------------------------------------------------
        # UPDATE SOCIAL MEDIA LINKS
        # --------------------------------------------------------
        elif action in ["social", "update_social"]:
            employer_obj.facebook_url = request.form.get(
                "facebook", request.form.get("facebook_url", employer_obj.facebook_url)
            )

            employer_obj.linkedin_url = request.form.get(
                "linkedin", request.form.get("linkedin_url", employer_obj.linkedin_url)
            )

            employer_obj.instagram_url = request.form.get(
                "instagram", request.form.get("instagram_url", employer_obj.instagram_url)
            )

            employer_obj.twitter_url = request.form.get(
                "twitter", request.form.get("twitter_url", employer_obj.twitter_url)
            )

        # --------------------------------------------------------
        # UPLOAD LOGO
        # --------------------------------------------------------
        elif action == "upload_logo":
            if "company_logo" in request.files:
                file = request.files["company_logo"]

                if file and file.filename:
                    upload_folder = os.path.join(
                        current_app.static_folder,
                        "uploads",
                        "logos",
                    )

                    try:
                        filename = save_validated_image(
                            file,
                            upload_folder,
                            prefix=f"logo_{employer_obj.employer_id}",
                        )
                    except ValueError as error:
                        flash(str(error), "danger")
                        return redirect(url_for("employer.profile"))

                    employer_obj.company_logo = filename

        # --------------------------------------------------------
        # UPLOAD GALLERY PHOTOS
        # --------------------------------------------------------
        elif action in ["media", "update_media", "update_gallery"]:
            gallery_folder = os.path.join(
                current_app.static_folder, "uploads", "gallery"
            )
            os.makedirs(gallery_folder, exist_ok=True)

            for key in ["gallery_1", "gallery_2", "gallery_3"]:
                if key in request.files:
                    file = request.files[key]

                    if file and file.filename:
                        try:
                            filename = save_validated_image(
                                file,
                                gallery_folder,
                                prefix=f"{key}_{employer_obj.employer_id}",
                            )
                        except ValueError as error:
                            flash(str(error), "danger")
                            return redirect(url_for("employer.profile"))

                        setattr(employer_obj, key, filename)

        try:
            db.session.commit()
            flash("Profile updated successfully!", "success")
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception(
                "Employer profile update failed."
            )
            flash(
                "An error occurred while saving changes.",
                "danger",
            )

        return redirect(url_for("employer.profile"))

    active_jobs = Job.query.filter_by(
        employer_id=employer_obj.employer_id, status="Open"
    ).count()

    total_applicants = (
        Application.query.join(Job)
        .filter(Job.employer_id == employer_obj.employer_id)
        .count()
    )

    hired_applicants = (
        Application.query.join(Job)
        .filter(
            Job.employer_id == employer_obj.employer_id,
            Application.status == "Accepted",
        )
        .count()
    )

    return render_template(
        "employer/employer_profile.html",
        current_user=current_user,
        employer=employer_obj,
        active_jobs=active_jobs,
        total_applicants=total_applicants,
        hired_applicants=hired_applicants,
    )


# ============================================================
# APPLICANTS LIST
#
# IMPORTANT:
# An application is shown ONLY when it has:
#
# 1. A Referral record
# OR
# 2. A JobFairCandidateEvaluation record
#
# Applications with neither are excluded.
# ============================================================

@employer.route("/applicants_list")
@login_required
@role_required("employer")
def jobseekerlist():
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_info = Employer.query.filter_by(
        user_id=current_user.user_id
    ).first()

    if employer_info is None:
        flash("Employer profile not found.", "danger")
        return redirect(url_for("auth.login"))

    # ========================================================
    # REFERRED JOB SEEKERS DIRECTORY
    #
    # This page is intentionally PERSON-CENTRIC.
    #
    # One job seeker appears only once even when PESO referred
    # that person to multiple jobs under this employer.
    #
    # Job/application-specific ranking and matching remain in
    # the existing Shortlist page.
    # ========================================================
    referral_rows = (
        Referral.query
        .join(
            Application,
            Referral.application_id == Application.application_id
        )
        .join(
            Job,
            Application.job_id == Job.job_id
        )
        .filter(
            Job.employer_id == employer_info.employer_id,
            Referral.status == "Referred",
        )
        .order_by(
            Referral.referral_date.desc(),
            Referral.created_at.desc(),
        )
        .all()
    )

    seeker_map = {}

    for referral in referral_rows:
        application = referral.application

        if application is None:
            continue

        seeker = application.job_seeker
        job = application.job

        if seeker is None or job is None:
            continue

        seeker_id = seeker.job_seeker_id
        referral_date = (
            referral.referral_date
            or referral.created_at
            or application.application_date
        )

        if seeker_id not in seeker_map:
            seeker_map[seeker_id] = {
                "job_seeker": seeker,
                "representative_application_id": application.application_id,
                "latest_referral_date": referral_date,
                "jobs": [],
                "job_titles": [],
            }

        entry = seeker_map[seeker_id]

        # Rows are already ordered newest first, so the first
        # application becomes the profile context.
        if (
            entry["latest_referral_date"] is None
            or (
                referral_date is not None
                and referral_date > entry["latest_referral_date"]
            )
        ):
            entry["latest_referral_date"] = referral_date
            entry["representative_application_id"] = application.application_id

        if job.job_id not in [item["job_id"] for item in entry["jobs"]]:
            entry["jobs"].append({
                "job_id": job.job_id,
                "job_title": job.job_title,
                "employment_type": job.employment_type,
                "application_id": application.application_id,
                "application_status": application.status,
                "referral_date": referral_date,
            })
            entry["job_titles"].append(job.job_title)

    referred_seekers = list(seeker_map.values())

    referred_seekers.sort(
        key=lambda item: item["latest_referral_date"] or datetime.min,
        reverse=True,
    )

    # Job Fair applicants are kept separate from PESO referrals.
    # One person appears once, with all jobs they were evaluated for
    # under this employer collected in the same directory entry.
    evaluation_rows = (
        JobFairCandidateEvaluation.query
        .join(JobFairCandidateEvaluation.employer_registration)
        .filter_by(employer_id=employer_info.employer_id)
        .order_by(JobFairCandidateEvaluation.updated_at.desc())
        .all()
    )

    jobfair_map = {}

    for evaluation in evaluation_rows:
        seeker = evaluation.job_seeker
        application = evaluation.application
        fair = evaluation.job_fair
        fair_job = evaluation.job_fair_job
        job = fair_job.job if fair_job else (application.job if application else None)
        registration = evaluation.job_fair_registration

        if seeker is None or application is None or job is None:
            continue

        seeker_id = seeker.job_seeker_id
        activity_date = evaluation.updated_at or evaluation.created_at or application.application_date

        if seeker_id not in jobfair_map:
            jobfair_map[seeker_id] = {
                "job_seeker": seeker,
                "representative_application_id": application.application_id,
                "latest_activity_date": activity_date,
                "latest_evaluation_status": evaluation.status,
                "latest_attendance_status": registration.attendance_status if registration else None,
                "latest_job_fair": fair,
                "jobs": [],
                "job_titles": [],
            }

        entry = jobfair_map[seeker_id]

        if entry["latest_activity_date"] is None or (activity_date and activity_date > entry["latest_activity_date"]):
            entry["representative_application_id"] = application.application_id
            entry["latest_activity_date"] = activity_date
            entry["latest_evaluation_status"] = evaluation.status
            entry["latest_attendance_status"] = registration.attendance_status if registration else None
            entry["latest_job_fair"] = fair

        if job.job_id not in [item["job_id"] for item in entry["jobs"]]:
            entry["jobs"].append({
                "job_id": job.job_id,
                "job_title": job.job_title,
                "application_id": application.application_id,
                "application_status": application.status,
                "evaluation_status": evaluation.status,
                "job_fair_title": fair.title if fair else "Job Fair",
            })
            entry["job_titles"].append(job.job_title)

    jobfair_seekers = list(jobfair_map.values())
    jobfair_seekers.sort(
        key=lambda item: item["latest_activity_date"] or datetime.min,
        reverse=True,
    )

    source = request.args.get("source", "referred").strip().lower()
    if source not in {"referred", "jobfair"}:
        source = "referred"

    return render_template(
        "employer/employer_jobseekerlist.html",
        current_user=current_user,
        employer=employer_info,
        referred_seekers=referred_seekers,
        jobfair_seekers=jobfair_seekers,
        source=source,
    )


# ============================================================
# APPLICANT DETAILS
#
# IMPORTANT:
# An application can ONLY be viewed when it has:
#
# 1. A Referral record
# OR
# 2. A JobFairCandidateEvaluation record
#
# This prevents an application that is not yet associated
# with either workflow from being accessed directly by URL.
# ============================================================

@employer.route("/applicant/<int:application_id>")
@login_required
@role_required("employer")
def applicant_details(application_id):
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_info = Employer.query.filter_by(user_id=current_user.user_id).first()

    if employer_info is None:
        flash("Employer profile not found.", "danger")
        return redirect(url_for("auth.login"))

    # ========================================================
    # FETCH APPLICATION
    # ========================================================
    application = Application.query.get_or_404(application_id)

    # Keep legacy profiles in sync with confirmed hiring records.
    if sync_jobseeker_employment_status(application.job_seeker):
        db.session.commit()

    # ========================================================
    # SECURITY CHECK
    # ========================================================
    if application.job.employer_id != employer_info.employer_id:
        flash("Unauthorized.", "danger")
        return redirect(url_for("employer.jobseekerlist"))

    # ========================================================
    # CHECK REFERRAL DATA
    # ========================================================
    referral = Referral.query.filter_by(
        application_id=application.application_id, status="Referred"
    ).first()

    has_referral = referral is not None

    # ========================================================
    # CHECK JOB FAIR EVALUATION DATA
    # ========================================================
    job_fair_evaluation = JobFairCandidateEvaluation.query.filter_by(
        application_id=application.application_id
    ).first()

    has_evaluation = job_fair_evaluation is not None

    # ========================================================
    # ACCESS RESTRICTION
    #
    # The application MUST have at least one:
    # - Referral
    # - Job Fair Evaluation
    #
    # If neither exists, do not allow access.
    # ========================================================
    if not has_referral and not has_evaluation:
        flash(
            "This application is not available because "
            "it has not been referred or evaluated through "
            "a Job Fair.",
            "warning",
        )
        return redirect(url_for("employer.jobseekerlist"))

    # ========================================================
    # PARSE APPLICANT DATA
    # ========================================================
    job_seeker = application.job_seeker

    if job_seeker is None:
        flash("Jobseeker information could not be found.", "danger")
        return redirect(url_for("employer.jobseekerlist"))

    skills = [
        skill.strip()
        for skill in re.split(r"[,\n]", job_seeker.skills or "")
        if skill.strip()
    ]

    licenses = JobSeekerLicense.query.filter_by(
        job_seeker_id=job_seeker.job_seeker_id
    ).all()

    work_history = JobSeekerWorkHistory.query.filter_by(
        job_seeker_id=job_seeker.job_seeker_id
    ).all()

    tvet_trainings = JobSeekerTVET.query.filter_by(
        job_seeker_id=job_seeker.job_seeker_id
    ).all()

    languages = JobSeekerLanguage.query.filter_by(
        job_seeker_id=job_seeker.job_seeker_id
    ).all()

    # ========================================================
    # DETERMINE APPLICATION SOURCE
    #
    # Referral exists:
    #     Referred
    #
    # Job Fair Evaluation exists without Referral:
    #     Job Fair
    #
    # Neither:
    #     Already blocked above.
    # ========================================================
    is_referred = has_referral
    is_job_fair = has_evaluation and not has_referral

    # Job Fair applications are your Direct applications.
    is_direct = is_job_fair

    # ========================================================
    # ALL OFFICIAL PESO REFERRALS FOR THIS JOB SEEKER
    # UNDER THE CURRENT EMPLOYER
    #
    # This keeps the profile person-centric while preserving the
    # existing application-specific workflow below.
    # ========================================================
    referred_job_rows = (
        Referral.query
        .join(
            Application,
            Referral.application_id == Application.application_id
        )
        .join(
            Job,
            Application.job_id == Job.job_id
        )
        .filter(
            Referral.status == "Referred",
            Application.job_seeker_id == job_seeker.job_seeker_id,
            Job.employer_id == employer_info.employer_id,
        )
        .order_by(
            Referral.referral_date.desc(),
            Referral.created_at.desc(),
        )
        .all()
    )

    referred_jobs = []

    for referred_job_referral in referred_job_rows:
        referred_application = referred_job_referral.application

        if (
            referred_application is None
            or referred_application.job is None
        ):
            continue

        referred_jobs.append({
            "referral": referred_job_referral,
            "application": referred_application,
            "job": referred_application.job,
            "referral_date": (
                referred_job_referral.referral_date
                or referred_job_referral.created_at
                or referred_application.application_date
            ),
        })

    # ========================================================
    # VACANCY INFORMATION
    # ========================================================
    current_date = date.today()
    _, remaining_vacancies = get_job_vacancy_info(application.job)

    # ========================================================
    # HIRING LOGIC
    #
    # REFERRED:
    # Interview is required before hiring.
    #
    # JOB FAIR:
    # Evaluation has already occurred, so the employer may
    # proceed according to the application's current status,
    # provided there is a vacancy.
    #
    # DIRECT:
    # Not allowed through this route because direct-only
    # applications have neither Referral nor Evaluation.
    # ========================================================
    if is_referred:
        can_hire = (
            application.status == "Interview"
            and application.interview_date is not None
            and application.interview_date <= current_date
            and remaining_vacancies > 0
        )
    elif is_job_fair:
        can_hire = (
            application.status not in ["Accepted", "Rejected"]
            and remaining_vacancies > 0
        )
    else:
        can_hire = False

    # ========================================================
    # RENDER
    # ========================================================
    # ========================================================
    # STRUCTURED MATCH DETAILS
    # ========================================================
    match_result = calculate_job_match(
        application.job,
        job_seeker
    )

    # Keep navigation connected.
    source = request.args.get("source", "").strip().lower()

    if source == "shortlist":
        back_url = url_for(
            "employer.shortlist",
            job_id=application.job_id
        )
        back_label = "Back to Job Shortlist"
    else:
        back_url = url_for(
            "employer.jobseekerlist"
        )
        back_label = "Back to Referred Job Seekers"

    return render_template(
        "employer/employer_jobseeker.html",
        application=application,
        applicant=job_seeker,
        job=application.job,
        skills=skills,
        licenses=licenses,
        work_history=work_history,
        tvet_trainings=tvet_trainings,
        languages=languages,
        referral=referral,
        job_fair_evaluation=job_fair_evaluation,
        is_direct=is_direct,
        is_referred=is_referred,
        is_job_fair=is_job_fair,
        has_referral=has_referral,
        has_evaluation=has_evaluation,
        can_hire=can_hire,
        remaining_vacancies=remaining_vacancies,
        current_date=current_date,
        current_user=current_user,
        employer=employer_info,
        match_result=match_result,
        referred_jobs=referred_jobs,
        back_url=back_url,
        back_label=back_label,
        format_work_date=format_work_date
    )


# ============================================================
# BATCH INTERVIEW SCHEDULING
#
# SECURITY:
# Only applications that:
# 1. Belong to the currently logged-in employer
# 2. Have an official PESO Referral
# 3. Have Referral.status == "Referred"
# can be scheduled through the bulk interview action.
#
# Pending referral requests are NOT allowed.
# ============================================================

def _lock_interview_jobseekers(applications):
    """Serialize interview scheduling for each jobseeker across employers.

    All scheduling endpoints must use this lock before checking conflicts.
    Sorted locking order reduces the risk of deadlocks in batch operations.
    """
    seeker_ids = sorted({app.job_seeker_id for app in applications})
    if seeker_ids:
        JobSeeker.query.filter(
            JobSeeker.job_seeker_id.in_(seeker_ids)
        ).order_by(JobSeeker.job_seeker_id).with_for_update().all()


def _find_interview_conflicts(application_ids, interview_date):
    """Find active interviews for the same jobseekers on the same date."""
    selected_ids = set(application_ids)
    selected = Application.query.filter(
        Application.application_id.in_(selected_ids)
    ).all()
    jobseeker_ids = {app.job_seeker_id for app in selected}
    if not jobseeker_ids:
        return []

    return (
        Application.query
        .filter(
            Application.job_seeker_id.in_(jobseeker_ids),
            Application.interview_date == interview_date,
            Application.interview_date.isnot(None),
            Application.status == "Interview",
            ~Application.application_id.in_(selected_ids),
        )
        .with_for_update()
        .all()
    )


@employer.route("/interview-availability", methods=["GET"])
@login_required
@role_required("employer")
def interview_availability():
    """Return date-level interview conflicts for employer-visible applications.

    The UI uses this before submission, but the POST scheduling routes also
    enforce the same rule server-side. A jobseeker is considered unavailable
    for a date when another active application already has an interview on
    that date. This intentionally uses the whole date rather than only the
    exact time so employers cannot accidentally create unrealistic back-to-back
    interviews at different locations.
    """
    employer_info = Employer.query.filter_by(
        user_id=current_user.user_id
    ).first()

    if employer_info is None:
        return jsonify({"available": False, "message": "Employer profile not found."}), 403

    raw_date = (request.args.get("date") or "").strip()
    if not raw_date:
        return jsonify({"available": False, "message": "Interview date is required."}), 400

    try:
        requested_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"available": False, "message": "Invalid interview date."}), 400

    raw_application_ids = (request.args.get("application_ids") or "").strip()
    raw_application_id = (request.args.get("application_id") or "").strip()

    values = []
    if raw_application_ids:
        values.extend(raw_application_ids.split(","))
    elif raw_application_id:
        values.append(raw_application_id)

    application_ids = list(dict.fromkeys(
        int(value.strip())
        for value in values
        if value.strip().isdigit()
    ))

    if not application_ids:
        return jsonify({"available": False, "message": "At least one application is required."}), 400

    selected_applications = (
        Application.query
        .join(Job, Application.job_id == Job.job_id)
        .filter(
            Application.application_id.in_(application_ids),
            Job.employer_id == employer_info.employer_id,
        )
        .all()
    )

    if len(selected_applications) != len(application_ids):
        return jsonify({"available": False, "message": "One or more applications are not accessible."}), 403

    jobseeker_ids = {app.job_seeker_id for app in selected_applications}
    conflicts = []

    existing = (
        Application.query
        .join(Job, Application.job_id == Job.job_id)
        .filter(
            Application.job_seeker_id.in_(jobseeker_ids),
            Application.interview_date == requested_date,
            Application.interview_date.isnot(None),
            Application.status == "Interview",
            Job.employer_id != employer_info.employer_id,
        )
        .all()
    )

    # Also catch an interview already stored for the same employer when the
    # selected application is a different application for the same person.
    # This protects against multiple applications under one employer too.
    existing_same_employer = (
        Application.query
        .join(Job, Application.job_id == Job.job_id)
        .filter(
            Application.job_seeker_id.in_(jobseeker_ids),
            Application.interview_date == requested_date,
            Application.interview_date.isnot(None),
            Application.status == "Interview",
            Job.employer_id == employer_info.employer_id,
        )
        .all()
    )
    existing.extend(existing_same_employer)

    selected_ids = set(application_ids)
    seen = set()
    for existing_application in existing:
        if existing_application.application_id in selected_ids:
            continue
        key = (existing_application.application_id, existing_application.job_seeker_id)
        if key in seen:
            continue
        seen.add(key)
        jobseeker = existing_application.job_seeker
        conflicts.append({
            "application_id": existing_application.application_id,
            "job_seeker_id": existing_application.job_seeker_id,
            "name": jobseeker.full_name if jobseeker else "Jobseeker",
        })

    return jsonify({
        "available": not conflicts,
        "date": requested_date.strftime("%B %d, %Y"),
        "conflicts": conflicts,
    })


@employer.route("/schedule-interview-batch", methods=["POST"])
@login_required
@role_required("employer")
def schedule_interview_batch():
    # SECURITY: ROLE CHECK
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    # GET CURRENT EMPLOYER
    employer_info = Employer.query.filter_by(user_id=current_user.user_id).first()
    if employer_info is None:
        flash("Employer profile not found.", "danger")
        return redirect(url_for("auth.login"))

    # REDIRECT TARGET
    # Usually returns the employer to the shortlist page where the batch interview was submitted.
    target_url = request.referrer or url_for("employer.joblisting")

    # GET SELECTED APPLICATION IDS
    raw_ids = request.form.get("batch_application_ids", "").strip()
    if not raw_ids:
        flash("No applicants were selected.", "warning")
        return redirect(target_url)

    # Reject the entire request when any selected ID is malformed.
    # Silently skipping bad IDs can schedule a different subset than the user submitted.
    raw_parts = [value.strip() for value in raw_ids.split(",")]
    if any(not value.isascii() or not value.isdecimal() or int(value) <= 0
           for value in raw_parts) or len(raw_parts) > 500:
        flash("Invalid applicant selection. Please select applicants again.", "warning")
        return redirect(target_url)
    app_ids = list(dict.fromkeys(int(value) for value in raw_parts))

    if not app_ids:
        flash("No valid applicants were selected.", "warning")
        return redirect(target_url)

    # GET INTERVIEW INFORMATION
    raw_interview_date = (request.form.get("interview_date") or "").strip()
    raw_interview_time = (request.form.get("interview_time") or "").strip()
    interview_location = (request.form.get("interview_location") or "").strip()
    interview_floor_room = (request.form.get("interview_floor_room") or "").strip()
    interview_notes = (request.form.get("interview_notes") or "").strip()

    if not raw_interview_date or not raw_interview_time or not interview_location:
        flash("Please provide the interview date, time, and location.", "warning")
        return redirect(target_url)

    if len(interview_location) > 220 or len(interview_floor_room) > 30:
        flash("Interview location is too long.", "warning")
        return redirect(target_url)
    if len(interview_notes) > 5000:
        flash("Interview notes must not exceed 5,000 characters.", "warning")
        return redirect(target_url)

    try:
        interview_date = datetime.strptime(raw_interview_date, "%Y-%m-%d").date()
        interview_time = datetime.strptime(raw_interview_time, "%H:%M").time()
    except ValueError:
        flash("Invalid interview date or time.", "danger")
        return redirect(target_url)

    if interview_date <= date.today():
        flash("Interview date must be after today.", "warning")
        return redirect(target_url)

    full_location = (
        f"{interview_location} ({interview_floor_room})"
        if interview_floor_room
        else interview_location
    )

    # Lock the selected jobseekers first so another employer cannot schedule
    # a competing interview for the same person while this request runs.
    selected = Application.query.filter(
        Application.application_id.in_(app_ids)
    ).all()
    _lock_interview_jobseekers(selected)

    # SECURITY QUERY: read fresh, locked application states after seeker locks.
    applications = (
        Application.query.join(Job, Application.job_id == Job.job_id)
        .join(Referral, Referral.application_id == Application.application_id)
        .filter(
            Application.application_id.in_(app_ids),
            Job.employer_id == employer_info.employer_id,
            Referral.status == "Referred",
        )
        .order_by(Application.application_id)
        .with_for_update(of=Application)
        .all()
    )

    allowed_application_ids = {app.application_id for app in applications}
    requested_application_ids = set(app_ids)

    if not applications:
        db.session.rollback()
        flash("No eligible referred applicants were found.", "warning")
        return redirect(target_url)

    if allowed_application_ids != requested_application_ids:
        db.session.rollback()
        flash(
            "One or more selected applicants are not eligible for bulk interview scheduling. "
            "Only applicants officially referred by PESO can be selected.",
            "danger",
        )
        return redirect(target_url)

    conflicts = _find_interview_conflicts(app_ids, interview_date)
    if conflicts:
        db.session.rollback()
        conflict_names = ", ".join(
            sorted({
                conflict.job_seeker.full_name
                for conflict in conflicts
                if conflict.job_seeker
            })
        )
        flash(
            f"Cannot schedule the selected jobseekers for {interview_date.strftime('%B %d, %Y')}. "
            f"The following jobseeker(s) are already unavailable that day: {conflict_names}.",
            "warning",
        )
        return redirect(target_url)

    # SCHEDULE INTERVIEWS
    for application in applications:
        can_schedule, reason = mark_application_interview(
            application
        )

        if not can_schedule:
            db.session.rollback()

            flash(
                (
                    f"{application.job_seeker.full_name}: "
                    f"{reason}"
                ),
                "warning",
            )
            return redirect(target_url)

        application.interview_date = interview_date
        application.interview_time = interview_time
        application.interview_location = full_location
        application.interview_notes = interview_notes
        application.interview_response_status = "Pending Confirmation"
        application.interview_response_note = None

    record_audit(
        f"INTERVIEW_BATCH_SCHEDULED count={len(applications)} date={interview_date.isoformat()}"
    )
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("Bulk interview scheduling failed: %s", e)
        flash("Unable to schedule the interviews. Please try again.", "danger")
        return redirect(target_url)

    email_failures = 0

    for application in applications:
        notify_user(
            application.job_seeker.user_id,
            "Interview Scheduled",
            (
                f"An interview was scheduled for your "
                f"application to "
                f"{application.job.job_title}."
            ),
            "Application",
        )

        jobseeker_email = (
            application.job_seeker.user.email
            if application.job_seeker and application.job_seeker.user
            else None
        )

        if jobseeker_email:
            try:
                formatted_time = interview_time.strftime("%I:%M %p")
                company_name = application.job.employer.company_name
                send_email(
                    recipient=jobseeker_email,
                    subject=f"Interview Scheduled - {application.job.job_title}",
                    body=(
                        f"Hello {application.job_seeker.first_name},\n\n"
                        f"An interview has been scheduled for your application "
                        f"to {application.job.job_title} at {company_name}.\n\n"
                        f"Date: {interview_date.strftime('%B %d, %Y')}\n"
                        f"Time: {formatted_time}\n"
                        f"Location: {full_location}\n\n"
                        f"Please log in to PESO-IMS to review the interview details."
                    ),
                )
            except Exception as email_error:
                email_failures += 1
                current_app.logger.exception(
                    "Bulk interview email failed for application %s: %s",
                    application.application_id,
                    email_error,
                )

    commit_notifications("bulk interview notifications")

    count = len(applications)
    label = "jobseeker" if count == 1 else "jobseekers"
    if email_failures:
        flash(
            f"Interview scheduled for {count} {label}, but {email_failures} email(s) could not be sent.",
            "warning",
        )
    else:
        flash(f"Interview successfully scheduled for {count} {label} and email notifications were sent.", "success")

    return redirect(target_url)


# ============================================================
# UPDATE COMPANY
# ============================================================

@employer.route("/update_company", methods=["POST"])
@login_required
@role_required("employer")
def update_company():
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_info = Employer.query.filter_by(user_id=current_user.user_id).first()

    if employer_info is None:
        flash("Employer not found.", "danger")
        return redirect(url_for("employer.employerverification"))

    def _company_field(name, limit, required=False):
        value = (request.form.get(name) or "").strip()
        if required and not value:
            abort(400, description=f"{name.replace('_', ' ').title()} is required.")
        if len(value) > limit:
            abort(400, description=f"{name.replace('_', ' ').title()} cannot exceed {limit} characters.")
        return value or None

    company_name = _company_field("company_name", 150, required=True)
    company_industry = _company_field("company_industry", 150)
    company_email = _company_field("company_email", 100)
    company_phone = _company_field("company_phone", 20)
    street = _company_field("street", 255)
    barangay = _company_field("barangay", 100)
    city = _company_field("city", 100)
    province = _company_field("province", 100)
    google_map_url = _company_field("google_map_url", 500)
    company_description = (request.form.get("company_description") or "").strip()
    if len(company_description) > 5000:
        abort(400, description="Company description cannot exceed 5000 characters.")
    # Do not silently replace an established company contact email.
    # A separate ownership-verification workflow must authorize such changes.
    existing_company_email = (employer_info.company_email or "").strip()
    proposed_company_email = (company_email or "").strip()
    if proposed_company_email.casefold() != existing_company_email.casefold():
        flash("Company email changes require verification. Your existing email was kept; other details were not changed.", "warning")
        return redirect(url_for("employer.employerverification"))
    if company_email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", company_email):
        abort(400, description="Please enter a valid company email address.")
    if company_phone and not re.fullmatch(r"[0-9+\-\s()]{7,20}", company_phone):
        abort(400, description="Please enter a valid company contact number.")

    employer_info.company_name = company_name
    employer_info.company_industry = company_industry
    employer_info.company_email = company_email
    employer_info.company_phone = company_phone
    employer_info.street = street
    employer_info.barangay = barangay
    employer_info.city = city
    employer_info.province = province
    employer_info.google_map_url = google_map_url
    employer_info.company_description = company_description or None

    location = ", ".join(
        filter(
            None,
            [
                employer_info.street,
                employer_info.barangay,
                employer_info.city,
                employer_info.province,
            ],
        )
    )

    for job in employer_info.jobs:
        job.location = location

    db.session.commit()

    flash("Company information updated!", "success")
    return redirect(url_for("employer.employerverification"))


# ============================================================
# APPLICANTS REDIRECT
# ============================================================

@employer.route("/applicants")
@login_required
@role_required("employer")
def employerjobseeker():
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    return redirect(url_for("employer.jobseekerlist"))


# ============================================================
# LOGOUT
# ============================================================

# ============================================================
# SHORTLIST
# ============================================================

@employer.route("/shortlist/<int:job_id>")
@login_required
@role_required("employer")
def shortlist(job_id):
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_info = Employer.query.filter_by(
        user_id=current_user.user_id
    ).first()

    if employer_info is None:
        flash("Employer profile not found.", "danger")
        return redirect(url_for("auth.login"))

    job = Job.query.get_or_404(job_id)

    if job.employer_id != employer_info.employer_id:
        flash("Unauthorized.", "danger")
        return redirect(url_for("employer.joblisting"))

    search_query = request.args.get("search", "").strip()

    accepted_count, remaining_vacancies = get_job_vacancy_info(job)

    referral_exists = (
        db.session.query(Referral.referral_id)
        .filter(
            Referral.application_id == Application.application_id,
            Referral.status == "Referred",
        )
        .exists()
    )

    evaluation_exists = (
        db.session.query(JobFairCandidateEvaluation.evaluation_id)
        .filter(
            JobFairCandidateEvaluation.application_id
            == Application.application_id
        )
        .exists()
    )

    app_query = (
        Application.query
        .join(
            JobSeeker,
            Application.job_seeker_id == JobSeeker.job_seeker_id,
        )
        .filter(
            Application.job_id == job.job_id,
            db.or_(
                referral_exists,
                evaluation_exists,
            ),
        )
    )

    if search_query:
        search = f"%{search_query}%"

        app_query = app_query.filter(
            db.or_(
                JobSeeker.first_name.ilike(search),
                JobSeeker.last_name.ilike(search),
                JobSeeker.skills.ilike(search),
                JobSeeker.experience.ilike(search),
                JobSeeker.pref_occupation_1.ilike(search),
                JobSeeker.pref_occupation_2.ilike(search),
                JobSeeker.pref_occupation_3.ilike(search),
            )
        )

    shortlisted_applications = (
        app_query
        .order_by(Application.application_date.desc())
        .all()
    )

    ranking_result = rank_job_applications(
        job,
        shortlisted_applications,
    )

    return render_template(
        "employer/employer_shortlist.html",
        current_user=current_user,
        employer=employer_info,
        job=job,
        shortlisted_applications=ranking_result["all"],
        ranked_applications=ranking_result["ranked"],
        unranked_applications=ranking_result["unranked"],
        accepted_count=accepted_count,
        remaining_vacancies=remaining_vacancies,
        current_date=date.today(),
        search=search_query,
    )


# ============================================================
# SCHEDULE INTERVIEW
# ============================================================

@employer.route(
    "/application/<int:application_id>/schedule", methods=["POST"]
)
@login_required
@role_required("employer")
def schedule_interview(application_id):
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    application = get_application(application_id)

    if not application:
        flash("Application not found.", "danger")
        return redirect(url_for("employer.joblisting"))

    default_url = url_for("employer.shortlist", job_id=application.job_id)
    target_url = _safe_local_redirect(
        request.form.get("next") or request.referrer,
        default_url,
    )

    _lock_interview_jobseekers([application])
    application = Application.query.filter_by(
        application_id=application.application_id
    ).with_for_update().first_or_404()

    can_schedule, reason = mark_application_interview(
        application
    )

    if not can_schedule:
        db.session.rollback()
        flash(reason, "danger")
        return redirect(target_url)

    raw_date = request.form.get("interview_date", "").strip()
    raw_time = request.form.get("interview_time", "").strip()
    interview_location = request.form.get("interview_location", "").strip()
    interview_floor_room = request.form.get("interview_floor_room", "").strip()
    interview_notes = request.form.get("interview_notes", "").strip()

    if not raw_date or not raw_time or not interview_location:
        flash(
            "Please fill in all required fields (Date, Time, Location).",
            "danger",
        )
        db.session.rollback()
        return redirect(target_url)

    if len(interview_location) > 220 or len(interview_floor_room) > 30:
        flash("Interview location is too long.", "danger")
        db.session.rollback()
        return redirect(target_url)
    if len(interview_notes) > 5000:
        flash("Interview notes must not exceed 5,000 characters.", "danger")
        db.session.rollback()
        return redirect(target_url)

    try:
        parsed_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
        if parsed_date <= date.today():
            flash("Interview date must be after today.", "danger")
            db.session.rollback()
            return redirect(target_url)

        conflicts = _find_interview_conflicts(
            [application.application_id],
            parsed_date,
        )
        if conflicts:
            conflict = conflicts[0]
            flash(
                f"{application.job_seeker.full_name} is not available on "
                f"{parsed_date.strftime('%B %d, %Y')}. "
                "They already have another interview scheduled that day.",
                "warning",
            )
            db.session.rollback()
            return redirect(target_url)

        application.interview_date = parsed_date
    except ValueError:
        flash("Invalid date format provided.", "danger")
        db.session.rollback()
        return redirect(target_url)

    try:
        application.interview_time = datetime.strptime(
            raw_time, "%H:%M"
        ).time()
    except ValueError:
        flash("Invalid time format provided.", "danger")
        db.session.rollback()
        return redirect(target_url)

    if interview_floor_room:
        interview_location = f"{interview_location}, {interview_floor_room}"

    application.interview_location = interview_location
    application.interview_notes = interview_notes
    application.interview_response_status = "Pending Confirmation"
    application.interview_response_note = None

    notify_user(
        application.job_seeker.user_id,
        "Interview Scheduled",
        f"An interview was scheduled for your application to {application.job.job_title}.",
        "Application",
    )

    notify_user(
        current_user.user_id,
        "Interview Scheduled",
        f"An interview was scheduled with {application.job_seeker.full_name} for {application.job.job_title}.",
        "Application",
    )
    record_audit(
        f"INTERVIEW_SCHEDULED application_id={application.application_id} job_id={application.job_id} date={parsed_date.isoformat()}"
    )

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Interview scheduling failed for application %s", application_id)
        flash("Unable to schedule the interview. Please try again.", "danger")
        return redirect(target_url)

    try:
        jobseeker_email = (
            application.job_seeker.user.email
            if (
                application.job_seeker
                and application.job_seeker.user
            )
            else None
        )
        if not jobseeker_email:
            raise ValueError(
                "The jobseeker does not have an email address."
            )
        formatted_time = application.interview_time.strftime("%I:%M %p")
        company_name = application.job.employer.company_name

        email_body = f"""Hello {application.job_seeker.first_name},

Great news! An interview has been scheduled for your application to {application.job.job_title} at {company_name}.

Interview Details:
- Date: {application.interview_date.strftime("%B %d, %Y")}
- Time: {formatted_time}
- Location: {application.interview_location}

Please log in to your PESO account to view further details.

Thank you.

{company_name}
via PESO Management Information System
"""

        send_email(
            recipient=jobseeker_email,
            subject=f"Interview Scheduled - {application.job.job_title}",
            body=email_body,
        )

        flash("Interview scheduled successfully and email sent.", "success")
    except Exception:
        current_app.logger.exception("Interview email failed for application %s", application_id)
        flash("Interview scheduled, but the email could not be delivered.", "warning")

    return redirect(target_url)


# ============================================================
# REJECT APPLICANT
# ============================================================

@employer.route(
    "/application/<int:application_id>/reject", methods=["POST"]
)
@login_required
@role_required("employer")
def reject_applicant(application_id):
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    application = get_application(application_id)
    # get_application enforces employer ownership and official referral/job-fair access.
    # Lock and refresh before applying a terminal decision so concurrent actions
    # cannot use a stale application status.
    application = (
        Application.query.filter_by(application_id=application.application_id)
        .with_for_update()
        .first()
    )
    if application is None:
        db.session.rollback()
        abort(404)

    default_url = url_for("employer.shortlist", job_id=application.job_id)
    target_url = _safe_local_redirect(
        request.form.get("next") or request.referrer,
        default_url,
    )

    rejected, reason = mark_application_rejected(
        application,
        rejected_at=datetime.utcnow(),
    )

    if not rejected:
        db.session.rollback()
        flash(reason, "danger")
        return redirect(target_url)

    notify_user(
        application.job_seeker.user_id,
        "Application Update",
        f"Your application to {application.job.job_title} was rejected.",
        "Application",
    )

    record_audit(
        f"APPLICATION_REJECTED application_id={application.application_id} job_id={application.job_id}"
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Reject applicant failed for application %s", application_id)
        flash("Unable to reject this applicant. Please try again.", "danger")
        return redirect(target_url)

    try:
        jobseeker_email = (
            application.job_seeker.user.email
            if (
                application.job_seeker
                and application.job_seeker.user
            )
            else None
        )

        if not jobseeker_email:
            raise ValueError(
                "The jobseeker does not have an email address."
            )
        company_name = application.job.employer.company_name

        email_body = f"""Hello {application.job_seeker.first_name},

Thank you for your interest in the {application.job.job_title} position at {company_name}.

After careful consideration, we regret to inform you that we will not be moving forward with your application at this time.

We appreciate your time and wish you success in your job search.

Thank you.

{company_name}
via PESO Management Information System
"""

        send_email(
            recipient=jobseeker_email,
            subject=f"Application Update - {application.job.job_title}",
            body=email_body,
        )

        flash("Applicant rejected successfully and email sent.", "success")
    except Exception:
        current_app.logger.exception("Applicant rejection email failed for application %s", application_id)
        flash("Applicant rejected, but the email could not be delivered.", "warning")

    return redirect(target_url)


# ============================================================
# HIRE APPLICANT
# ============================================================

@employer.route("/application/<int:application_id>/hire", methods=["POST"])
@login_required
@role_required("employer")
def hire_applicant(application_id):
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    application = get_application(application_id)
    job = application.job

    default_url = url_for("employer.shortlist", job_id=application.job_id)
    target_url = _safe_local_redirect(
        request.form.get("next") or request.referrer,
        default_url,
    )

    # ========================================================
    # DETERMINE APPLICATION SOURCE
    # ========================================================
    referral = get_official_referral(
        application.application_id
    )

    is_referred = referral is not None

    job_fair_evaluation = (
        JobFairCandidateEvaluation.query
        .filter_by(
            application_id=application.application_id
        )
        .first()
    )

    is_direct = (
        job_fair_evaluation is not None
        and not is_referred
    )

    if not is_referred and not is_direct:
        db.session.rollback()
        flash(
            "This applicant is not eligible for employer processing because there is no official PESO referral or Job Fair evaluation.",
            "danger",
        )
        return redirect(target_url)

    # ========================================================
    # APPLICATION STATE / SOURCE VALIDATION
    # ========================================================
    can_hire, reason = validate_hire_transition(
        application,
        is_referred=is_referred,
        is_job_fair_direct=is_direct,
    )

    if not can_hire:
        flash(reason, "danger")
        return redirect(target_url)

    # ========================================================
    # VACANCY CHECK WITH ROW LOCK
    # ========================================================
    locked_job = lock_job_for_hiring(
        job.job_id
    )
    if locked_job is None:
        db.session.rollback()
        flash("This job listing is no longer available.", "danger")
        return redirect(target_url)

    # The initial application check happened before acquiring the job lock.
    # Reload and lock the application so concurrent hiring requests cannot
    # rely on a stale status (including duplicate requests for the same hire).
    application = (
        db.session.query(Application)
        .filter(
            Application.application_id == application_id,
            Application.job_id == locked_job.job_id,
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    if application is None:
        db.session.rollback()
        flash("This application is no longer available.", "danger")
        return redirect(target_url)

    can_hire, reason = validate_hire_transition(
        application,
        is_referred=is_referred,
        is_job_fair_direct=is_direct,
    )
    if not can_hire:
        db.session.rollback()
        flash(reason, "danger")
        return redirect(target_url)

    (
        has_capacity,
        capacity_error,
        accepted_count,
        remaining_vacancies,
    ) = validate_hiring_capacity_locked(
        locked_job
    )

    if not has_capacity:
        db.session.rollback()
        flash(capacity_error, "danger")
        return redirect(target_url)

    job = locked_job

    # ========================================================
    # ACCEPT APPLICANT
    # ========================================================
    accepted, reason = mark_application_accepted(
        application,
        hired_at=datetime.utcnow(),
    )

    if accepted:
        record_audit(
            f"APPLICATION_HIRED application_id={application.application_id} job_id={application.job_id}"
        )

    if not accepted:
        db.session.rollback()
        flash(reason, "danger")
        return redirect(target_url)

    # Link a real work-history entry to this specific accepted application.
    # The application remains the source of truth for ending this employment.
    if application.work_history_id is None:
        history = JobSeekerWorkHistory(
            job_seeker_id=application.job_seeker_id,
            company=(job.employer.company_name or "")[:150],
            company_address=(job.location or "")[:150],
            position=(job.job_title or "")[:150],
            work_from=(application.hired_at or datetime.utcnow()).strftime("%Y-%m"),
            work_to=None,
            appointment_status="Current",
        )
        db.session.add(history)
        db.session.flush()
        application.work_history_id = history.work_id
    application.job_seeker.employment_status = "employed"

    # ========================================================
    # UPDATE JOB VACANCY
    # ========================================================
    new_accepted_count = accepted_count + 1
    if new_accepted_count >= job.vacancies:
        job.status = "Closed"

    # ========================================================
    # IN-APP NOTIFICATION
    # ========================================================
    notify_user(
        application.job_seeker.user_id,
        "Application Accepted",
        f"Your application to {job.job_title} was accepted.",
        "Application",
    )

    notify_user(
        current_user.user_id,
        "Applicant Hired",
        f"{application.job_seeker.full_name} was hired for {job.job_title}.",
        "Application",
    )

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Hire applicant failed for application %s", application_id)
        flash("Unable to hire this applicant. Please try again.", "danger")
        return redirect(target_url)

    # ========================================================
    # EMAIL NOTIFICATION
    # ========================================================
    try:
        jobseeker_email = (
            application.job_seeker.user.email
            if (
                application.job_seeker
                and application.job_seeker.user
            )
            else None
        )

        if not jobseeker_email:
            raise ValueError(
                "The jobseeker does not have an email address."
            )
        company_name = job.employer.company_name

        email_body = f"""Hello {application.job_seeker.first_name},

Congratulations! We are pleased to inform you that your application for {job.job_title} at {company_name} has been Accepted.

Our hiring team will be in touch with you shortly regarding the next onboarding steps.

Thank you.

{company_name}
via PESO Management Information System
"""

        send_email(
            recipient=jobseeker_email,
            subject=f"Application Accepted - {job.job_title}",
            body=email_body,
        )
    except Exception:
        current_app.logger.exception("Hiring email failed for application %s", application_id)
        flash("Applicant hired, but the email could not be delivered.", "warning")

    # ========================================================
    # SUCCESS MESSAGE
    # ========================================================
    if job.status == "Closed":
        flash(
            "Applicant hired successfully. The job is now closed because all vacancies have been filled.",
            "success",
        )
    else:
        remaining_after_hire = job.vacancies - new_accepted_count
        flash(
            f"Applicant hired successfully. {remaining_after_hire} vacancy/vacancies remaining.",
            "success",
        )

    return redirect(target_url)


# ============================================================
# END AN EMPLOYMENT (does not reverse the historical hire)
# ============================================================
@employer.route("/application/<int:application_id>/end-employment", methods=["POST"])
@login_required
@role_required("employer")
def end_employment(application_id):
    employer_info = Employer.query.filter_by(user_id=current_user.user_id).first()
    if employer_info is None:
        abort(403)
    application = (
        db.session.query(Application)
        .join(Job, Application.job_id == Job.job_id)
        .filter(Application.application_id == application_id,
                Job.employer_id == employer_info.employer_id)
        .with_for_update()
        .first()
    )
    if application is None:
        abort(404)
    # Apply the same referral / job-fair authorization used for applicant details.
    get_application(application_id)
    target = url_for("employer.applicant_details", application_id=application_id)
    if application.status != "Accepted" or application.employment_ended_at is not None:
        flash("This application has no active employment to end.", "warning")
        return redirect(target)
    reason = (request.form.get("end_reason") or "").strip()
    if not reason or len(reason) > 255:
        flash("Please provide a reason (up to 255 characters).", "danger")
        return redirect(target)
    ended_at = datetime.utcnow()
    application.employment_ended_at = ended_at
    application.employment_end_reason = reason
    if not application.work_history_id and application.hired_at:
        legacy_history = JobSeekerWorkHistory(
            job_seeker_id=application.job_seeker_id,
            company=(application.job.employer.company_name or "")[:150],
            company_address=(application.job.location or "")[:150],
            position=(application.job.job_title or "")[:150],
            work_from=application.hired_at.strftime("%Y-%m"),
            work_to=ended_at.strftime("%Y-%m"),
            appointment_status="Former",
            number_of_months=max(0, (ended_at.year - application.hired_at.year) * 12
                                 + ended_at.month - application.hired_at.month),
        )
        db.session.add(legacy_history)
        db.session.flush()
        application.work_history_id = legacy_history.work_id
    if application.work_history_id:
        history = JobSeekerWorkHistory.query.filter_by(
            work_id=application.work_history_id,
            job_seeker_id=application.job_seeker_id,
        ).first()
        if history:
            history.work_to = ended_at.strftime("%Y-%m")
            history.appointment_status = "Former"
            if application.hired_at:
                history.number_of_months = max(
                    0, (ended_at.year - application.hired_at.year) * 12
                    + ended_at.month - application.hired_at.month
                )
    # An accepted application is a permanent hiring record, not a live-employment flag.
    active_other = Application.query.filter(
        Application.job_seeker_id == application.job_seeker_id,
        Application.application_id != application.application_id,
        Application.status == "Accepted",
        Application.employment_ended_at.is_(None),
    ).first()
    if active_other is None:
        application.job_seeker.employment_status = "unemployed"
        application.job_seeker.employed_type = None
        application.job_seeker.unemployed_reason = reason
    record_audit(
        f"EMPLOYMENT_ENDED application_id={application_id} job_id={application.job_id}"
    )
    notify_user(
        application.job_seeker.user_id,
        "Employment Updated",
        f"Your employment record for {application.job.job_title} at "
        f"{application.job.employer.company_name} has been marked ended. Reason: {reason}",
        "Application",
    )
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Ending employment failed for application %s", application_id)
        flash("Unable to update employment. Please try again.", "danger")
        return redirect(target)
    flash("Employment ended and work history updated successfully.", "success")
    return redirect(target)


# ============================================================
# VIEW APPLICATION RESUME
# ============================================================

@employer.route("/application/<int:application_id>/resume")
@login_required
@role_required("employer")
def view_application_resume(application_id):
    # Reuse the same ownership and mediated-access checks as applicant details.
    # Employer ownership alone must not expose a resume before an official
    # referral or an authorized job-fair evaluation exists.
    application = get_application(application_id)

    seeker = application.job_seeker

    if not seeker or not seeker.resume:
        abort(404)

    from website.utils.private_resumes import private_resume_directory
    upload_folder = private_resume_directory()

    # Resume names are stored server-side, but must still be treated as
    # untrusted path components (including legacy database values).
    stored_name = str(seeker.resume)
    if (not stored_name or stored_name in {".", ".."}
            or os.path.basename(stored_name) != stored_name
            or "\\" in stored_name):
        abort(404)

    resume_path = os.path.join(upload_folder, stored_name)
    if not os.path.isfile(resume_path):
        abort(404)

    return send_from_directory(
        directory=upload_folder,
        path=stored_name,
        as_attachment=False,
        download_name=getattr(seeker, "resume_filename", seeker.resume),
    )