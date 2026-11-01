from flask import (
    abort,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    current_app
)
from flask_login import (
    login_required,
    current_user
)
from website.extensions import db
from website.utils.notifications import notify_admins
from website.utils.job_fair_lifecycle import (
    close_exclusive_jobs_for_registration,
)
from website.utils.job_requirements import (
    VALID_EDUCATION_LEVELS,
    VALID_EXPERIENCE_MONTHS,
    parse_structured_qualifications,
)
from website.models import (
    Employer,
    Job,
    JobFair,
    JobFairEmployerRegistration,
    JobFairJob,
    Application,
    JobQualification
)
from decimal import Decimal, InvalidOperation
from datetime import datetime

from . import employer


# ============================================================
# JOB FAIR-EXCLUSIVE JOB LIFECYCLE GUARDS
# ============================================================

def get_current_employer_for_job_fair():
    if current_user.role != "employer":
        abort(404)

    employer_info = Employer.query.filter_by(
        user_id=current_user.user_id
    ).first()

    if employer_info is None:
        abort(404)

    return employer_info


def get_owned_exclusive_job_context(
    job_fair_id,
    job_id,
    *,
    require_editable=False,
):
    """
    Resolve and validate:
    Employer -> Job Fair registration -> JobFairJob -> Job.

    This prevents an employer from modifying another
    employer's Job Fair-exclusive job by changing IDs.
    """
    employer_info = get_current_employer_for_job_fair()

    job_fair = JobFair.query.get_or_404(
        job_fair_id
    )

    registration = (
        JobFairEmployerRegistration.query
        .filter_by(
            job_fair_id=job_fair_id,
            employer_id=employer_info.employer_id,
        )
        .first()
    )

    if registration is None:
        abort(404)

    entry = (
        JobFairJob.query
        .filter_by(
            registration_id=registration.registration_id,
            job_id=job_id,
        )
        .first()
    )

    if entry is None or entry.job is None:
        abort(404)

    job = entry.job

    if (
        job.employer_id != employer_info.employer_id
        or job.job_type != "Job Fair"
    ):
        abort(403)

    if require_editable:
        if registration.status != "Pending":
            return (
                employer_info,
                job_fair,
                registration,
                entry,
                job,
                "This Job Fair registration has already been reviewed.",
            )

        if job_fair.status != "Upcoming":
            return (
                employer_info,
                job_fair,
                registration,
                entry,
                job,
                "Job Fair-exclusive positions can only be modified while the event is upcoming.",
            )

        now = datetime.now()

        if (
            job_fair.registration_deadline
            and now > job_fair.registration_deadline
        ):
            return (
                employer_info,
                job_fair,
                registration,
                entry,
                job,
                "The Job Fair registration deadline has already passed.",
            )

        if job.approval_status == "Approved":
            return (
                employer_info,
                job_fair,
                registration,
                entry,
                job,
                "This position has already been approved by PESO and can no longer be modified.",
            )

    return (
        employer_info,
        job_fair,
        registration,
        entry,
        job,
        None,
    )


# ============================================================
# JOB FAIR JOB PAYLOAD VALIDATION
# ============================================================

def parse_job_fair_job_payload():
    """
    Validate and normalize the Job Fair-exclusive job form.

    Returns:
        (payload, None) on success
        (None, error_message) on failure
    """

    job_title = (
        request.form.get("job_title", "")
        or ""
    ).strip()

    category = (
        request.form.get("category", "")
        or ""
    ).strip()

    if category == "Others":
        category = (
            request.form.get(
                "other_category",
                ""
            )
            or ""
        ).strip()

    employment_type = (
        request.form.get(
            "employment_type",
            ""
        )
        or ""
    ).strip()

    description = (
        request.form.get(
            "description",
            ""
        )
        or ""
    ).strip()

    minimum_education = (
        request.form.get(
            "minimum_education",
            ""
        )
        or ""
    ).strip()

    try:
        minimum_experience_months = int(
            request.form.get(
                "minimum_experience_months",
                ""
            )
        )
    except (TypeError, ValueError):
        return None, (
            "Please select a valid minimum "
            "experience requirement."
        )

    if not job_title:
        return None, "Job title is required."
    if len(job_title) > 150:
        return None, "Job title must not exceed 150 characters."

    if len(category) > 100:
        return None, "Job category must not exceed 100 characters."

    if len(description) > 10000:
        return None, "Job description must not exceed 10,000 characters."


    if not category:
        return None, "Job category is required."

    if employment_type not in {
        "Full Time",
        "Part Time",
    }:
        return None, (
            "Please select a valid employment type."
        )

    if not description:
        return None, "Job description is required."

    if minimum_education not in VALID_EDUCATION_LEVELS:
        return None, (
            "Please select a valid minimum "
            "education requirement."
        )

    if (
        minimum_experience_months
        not in VALID_EXPERIENCE_MONTHS
    ):
        return None, (
            "Please select a valid minimum "
            "experience requirement."
        )

    try:
        salary_min = Decimal(
            (
                request.form.get(
                    "salary_min",
                    ""
                )
                or ""
            )
            .replace(",", "")
            .strip()
        )

        salary_max = Decimal(
            (
                request.form.get(
                    "salary_max",
                    ""
                )
                or ""
            )
            .replace(",", "")
            .strip()
        )

        vacancies = int(
            (
                request.form.get(
                    "vacancies",
                    ""
                )
                or ""
            )
            .replace(",", "")
            .strip()
        )

    except (
        ValueError,
        TypeError,
        InvalidOperation,
    ):
        return None, (
            "Please enter valid salary and "
            "vacancy values."
        )

    if not salary_min.is_finite() or not salary_max.is_finite():
        return None, "Please enter valid finite salary amounts."

    if salary_min < 0 or salary_max < 0:
        return None, "Salary cannot be negative."

    if salary_min > salary_max:
        return None, (
            "Minimum salary cannot exceed "
            "maximum salary."
        )

    if not 1 <= vacancies <= 100000:
        return None, "Vacancies must be a whole number from 1 to 100,000."

    structured_qualifications = (
        parse_structured_qualifications(
            request.form
        )
    )

    raw_qualifications = (
        request.form.getlist(
            "qualification[]"
        )
    )

    if any(
        not (value or "").strip()
        for value in raw_qualifications
    ):
        return None, (
            "Please complete or remove every "
            "additional qualification field."
        )

    qualifications = [
        value.strip()
        for value in raw_qualifications
        if value.strip()
    ]

    if not qualifications:
        return None, (
            "Please provide at least one "
            "additional qualification."
        )
    if len(qualifications) > 30 or any(len(value) > 500 for value in qualifications):
        return None, "Qualifications contain too many or overly long entries."


    raw_responsibilities = (
        request.form.getlist(
            "responsibility[]"
        )
    )

    if any(
        not (value or "").strip()
        for value in raw_responsibilities
    ):
        return None, (
            "Please complete or remove every "
            "responsibility field."
        )

    responsibilities = [
        value.strip()
        for value in raw_responsibilities
        if value.strip()
    ]

    if not responsibilities:
        return None, (
            "Please provide at least one "
            "job responsibility."
        )
    if len(responsibilities) > 30 or any(len(value) > 500 for value in responsibilities):
        return None, "Responsibilities contain too many or overly long entries."


    return {
        "job_title": job_title,
        "category": category,
        "employment_type": employment_type,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "vacancies": vacancies,
        "description": description,
        "minimum_education": minimum_education,
        "minimum_experience_months":
            minimum_experience_months,
        "qualifications": qualifications,
        "responsibilities": responsibilities,
        "structured_qualifications":
            structured_qualifications,
    }, None

# ============================================================
# JOB FAIR ELIGIBLE REGULAR JOBS
# ============================================================

def get_eligible_job_fair_jobs(employer_id, job_fair):
    """
    Return regular employer jobs that may be offered
    during the selected Job Fair.

    Requirements:
    - belongs to the logged-in employer
    - approved by PESO
    - currently open
    - has a deadline
    - deadline remains valid through the Job Fair
    - is not already a Job Fair-exclusive job
    """

    if not job_fair.end_datetime:
        fair_deadline = job_fair.start_datetime.date()
    else:
        fair_deadline = job_fair.end_datetime.date()

    return (
        Job.query
        .filter(
            Job.employer_id == employer_id,
            Job.status == "Open",
            Job.approval_status == "Approved",
            Job.deadline.isnot(None),
            Job.deadline >= fair_deadline,
            Job.job_type != "Job Fair"
        )
        .order_by(Job.job_title.asc())
        .all()
    )



def _validated_optional_positive_int(field_name):
    """Read an optional positive integer without silently accepting malformed input.

    An omitted field keeps the existing default of 1; an explicitly
    submitted invalid value is rejected by the calling range checks.
    """
    raw_value = request.form.get(field_name)
    if raw_value is None:
        return 1
    raw_value = raw_value.strip()
    if not raw_value or not raw_value.isascii() or not raw_value.isdecimal():
        return None
    try:
        return int(raw_value)
    except ValueError:
        return None


@employer.route("/job-fairs")
@login_required
def job_fairs():
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_info = Employer.query.filter_by(
        user_id=current_user.user_id
    ).first()

    if employer_info is None:
        flash("Employer not found.", "danger")
        return redirect(url_for("employer.dashboard"))

    now = datetime.now()

    # The listing page should show the event based on the Job Fair's
    # lifecycle status, even when employer registration has already closed.
    # Registration eligibility is still enforced in job_fair_details().
    job_fairs = (
        JobFair.query
        .filter(
            JobFair.status.in_([
                "Upcoming",
                "Ongoing",
                "Finished",
            ])
        )
        .all()
    )

    status_order = {
        "Ongoing": 0,
        "Upcoming": 1,
        "Finished": 2,
    }

    # Keep Ongoing and Upcoming in their existing chronological order.
    # Finished fairs appear after them, with the most recent event first.
    job_fairs.sort(
        key=lambda fair: (
            status_order.get(fair.status, 3),
            fair.start_datetime or datetime.max,
        )
    )
    finished_fairs = [fair for fair in job_fairs if fair.status == "Finished"]
    finished_fairs.reverse()
    job_fairs = [fair for fair in job_fairs if fair.status != "Finished"] + finished_fairs

    registrations = {
        registration.job_fair_id: registration
        for registration in employer_info.job_fair_registrations
    }

    return render_template(
        "employer/employer_jobfairs.html",
        current_user=current_user,
        employer=employer_info,
        job_fairs=job_fairs,
        registrations=registrations,
        now=now
    )


@employer.route("/job-fair/<int:job_fair_id>")
@login_required
def job_fair_details(job_fair_id):
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_info = Employer.query.filter_by(
        user_id=current_user.user_id
    ).first()

    if employer_info is None:
        flash("Employer not found.", "danger")
        return redirect(url_for("employer.dashboard"))

    job_fair = JobFair.query.get_or_404(job_fair_id)

    registration = (
        JobFairEmployerRegistration.query
        .filter_by(
            job_fair_id=job_fair.job_fair_id,
            employer_id=employer_info.employer_id
        )
        .first()
    )

    now = datetime.now()

    registration_open = (
        job_fair.status == "Upcoming"
        and (
            job_fair.registration_deadline is None
            or now <= job_fair.registration_deadline
        )
    )

    event_finished = (job_fair.status == "Finished")

    registration_deadline_passed = (
        job_fair.registration_deadline is not None
        and now > job_fair.registration_deadline
    )

    available_jobs = get_eligible_job_fair_jobs(
        employer_info.employer_id,
        job_fair
    )

    selected_jobs = []
    exclusive_jobs = []

    if registration:
        selected_jobs = [
            entry.job
            for entry in registration.selected_jobs
            if entry.job is not None
        ]

        exclusive_jobs = [
            job
            for job in selected_jobs
            if job.job_type == "Job Fair"
        ]

    return render_template(
        "employer/employer_jobfair_details.html",
        current_user=current_user,
        employer=employer_info,
        job_fair=job_fair,
        registration=registration,
        available_jobs=available_jobs,
        selected_jobs=selected_jobs,
        exclusive_jobs=exclusive_jobs,
        registration_open=registration_open,
        event_finished=event_finished,
        registration_deadline_passed=registration_deadline_passed,
        now=now
    )


@employer.route("/job-fair/<int:job_fair_id>/register", methods=["POST"])
@login_required
def register_job_fair(job_fair_id):
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_info = Employer.query.filter_by(user_id=current_user.user_id).first()
    if employer_info is None:
        flash("Employer not found.", "danger")
        return redirect(url_for("employer.dashboard"))

    job_fair = JobFair.query.get_or_404(job_fair_id)
    now = datetime.now()

    # ========================================================
    # CHECK JOB FAIR REGISTRATION STATUS
    # ========================================================
    if job_fair.status != "Upcoming":
        flash("This Job Fair is no longer accepting employer registrations.", "danger")
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    if job_fair.registration_deadline and now > job_fair.registration_deadline:
        flash("Registration for this Job Fair is already closed.", "danger")
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    # ========================================================
    # PREVENT DUPLICATE REGISTRATION
    # ========================================================
    existing_registration = JobFairEmployerRegistration.query.filter_by(
        job_fair_id=job_fair_id,
        employer_id=employer_info.employer_id
    ).first()

    if existing_registration:
        flash("Your company is already registered for this Job Fair.", "info")
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    # ========================================================
    # REPRESENTATIVES
    # ========================================================
    representatives = _validated_optional_positive_int("representatives")

    if representatives is None or representatives < 1 or representatives > 15:
        flash("Representatives must be a whole number from 1 to 15.", "danger")
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    simultaneous_interview_capacity = _validated_optional_positive_int("simultaneous_interview_capacity")
    if (
        simultaneous_interview_capacity is None
        or simultaneous_interview_capacity < 1
        or simultaneous_interview_capacity > representatives
        or simultaneous_interview_capacity > 15
    ):
        flash(
            "Simultaneous interview capacity must be between 1 and the number of representatives.",
            "danger",
        )
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    special_requests = request.form.get("special_requests", "").strip()

    if len(special_requests) > 2000:
        flash("Special requests must not exceed 2,000 characters.", "danger")
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    # ========================================================
    # GET SELECTED EXISTING JOBS
    # ========================================================
    submitted_job_ids = request.form.getlist("job_ids")
    selected_job_ids = []
    seen_job_ids = set()

    for raw_job_id in submitted_job_ids:
        # Do not silently discard tampered or malformed selections.
        # Reject the entire request before making any database changes.
        raw_job_id = str(raw_job_id).strip()
        if not raw_job_id.isascii() or not raw_job_id.isdecimal():
            flash("Invalid job selection. Please select jobs again.", "danger")
            return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))
        job_id = int(raw_job_id)
        if job_id <= 0:
            flash("Invalid job selection. Please select jobs again.", "danger")
            return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

        if job_id in seen_job_ids:
            continue

        seen_job_ids.add(job_id)
        selected_job_ids.append(job_id)

    # ========================================================
    # MUST SELECT AT LEAST ONE EXISTING JOB
    # ========================================================
    # Exclusive-job registration uses a different route.
    if not selected_job_ids:
        flash(
            "Please select at least one eligible job, or create a Job Fair-exclusive position.",
            "danger"
        )
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    # ========================================================
    # REVALIDATE JOBS SERVER-SIDE
    # ========================================================
    if job_fair.end_datetime:
        required_deadline = job_fair.end_datetime.date()
    else:
        required_deadline = job_fair.start_datetime.date()

    valid_jobs = []

    for job_id in selected_job_ids:
        job = Job.query.filter(
            Job.job_id == job_id,
            Job.employer_id == employer_info.employer_id,
            Job.status == "Open",
            Job.approval_status == "Approved",
            Job.deadline.isnot(None),
            Job.deadline >= required_deadline,
            Job.job_type != "Job Fair"
        ).first()

        if job:
            valid_jobs.append(job)

    # ========================================================
    # INVALID / CLOSED / UNAPPROVED JOB PROTECTION
    # ========================================================
    if not valid_jobs:
        flash("None of the selected jobs are currently eligible for this Job Fair.", "danger")
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    # If browser submitted 3 jobs but only 2 are valid,
    # reject instead of silently accepting only 2.
    if len(valid_jobs) != len(selected_job_ids):
        flash(
            "One or more selected jobs are no longer available or eligible. Please review your selection.",
            "warning"
        )
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    # ========================================================
    # CREATE REGISTRATION + JOB LINKS
    # ========================================================
    try:
        registration = JobFairEmployerRegistration(
            job_fair_id=job_fair_id,
            employer_id=employer_info.employer_id,
            representatives=representatives,
            simultaneous_interview_capacity=simultaneous_interview_capacity,
            special_requests=special_requests,
            status="Pending"
        )

        db.session.add(registration)
        db.session.flush()  # Get registration_id without committing yet.

        for job in valid_jobs:
            job_fair_entry = JobFairJob(
                registration_id=registration.registration_id,
                job_id=job.job_id
            )
            db.session.add(job_fair_entry)

        notify_admins(
            "New Job Fair Registration",
            f"{employer_info.company_name} registered for {job_fair.title} with {len(valid_jobs)} job position(s).",
            "Job Fair"
        )

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(
            "Failed Job Fair registration for employer %s: %s",
            employer_info.employer_id,
            e
        )
        flash("Unable to submit your Job Fair registration. Please try again.", "danger")
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    flash("Your Job Fair registration has been submitted for review.", "success")
    return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))


@employer.route("/job-fair/<int:job_fair_id>/register/jobs", methods=["POST"])
@login_required
def select_job_fair_jobs(job_fair_id):
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_info = Employer.query.filter_by(
        user_id=current_user.user_id
    ).first()

    if employer_info is None:
        flash("Employer not found.", "danger")
        return redirect(url_for("employer.dashboard"))

    job_fair = JobFair.query.get_or_404(job_fair_id)

    registration = (
        JobFairEmployerRegistration.query
        .filter_by(
            job_fair_id=job_fair_id,
            employer_id=employer_info.employer_id
        )
        .first()
    )

    if registration is None:
        flash("You must register for the Job Fair first.", "danger")
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    if registration.status != "Pending":
        flash("Job selections can only be changed while your registration is pending.", "danger")
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    now = datetime.now()

    if job_fair.status != "Upcoming":
        flash("Job selections can no longer be changed for this Job Fair.", "danger")
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    if job_fair.registration_deadline and now > job_fair.registration_deadline:
        flash("The registration deadline for this Job Fair has passed.", "danger")
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    submitted_job_ids = request.form.getlist("job_ids")
    selected_job_ids = []
    seen_job_ids = set()

    for raw_job_id in submitted_job_ids:
        # Do not silently discard tampered or malformed selections.
        # Reject the entire request before making any database changes.
        raw_job_id = str(raw_job_id).strip()
        if not raw_job_id.isascii() or not raw_job_id.isdecimal():
            flash("Invalid job selection. Please select jobs again.", "danger")
            return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))
        job_id = int(raw_job_id)
        if job_id <= 0:
            flash("Invalid job selection. Please select jobs again.", "danger")
            return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

        if job_id in seen_job_ids:
            continue

        seen_job_ids.add(job_id)
        selected_job_ids.append(job_id)

    selected_job_id_set = set(selected_job_ids)

    existing_entries = (
        JobFairJob.query
        .join(Job, JobFairJob.job_id == Job.job_id)
        .filter(
            JobFairJob.registration_id == registration.registration_id,
            Job.job_type != "Job Fair"
        )
        .all()
    )

    existing_entries_by_job_id = {
        entry.job_id: entry
        for entry in existing_entries
    }

    exclusive_job_exists = (
        JobFairJob.query
        .join(Job, JobFairJob.job_id == Job.job_id)
        .filter(
            JobFairJob.registration_id == registration.registration_id,
            Job.job_type == "Job Fair"
        )
        .first()
    )

    if not selected_job_ids and not exclusive_job_exists:
        flash("Please select at least one existing job or create a Job Fair-exclusive job.", "danger")
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    for job_id, entry in existing_entries_by_job_id.items():
        if job_id not in selected_job_id_set:
            db.session.delete(entry)

    db.session.flush()

    valid_job_count = 0

    required_deadline = (
        job_fair.end_datetime.date()
        if job_fair.end_datetime
        else job_fair.start_datetime.date()
    )

    for job_id in selected_job_ids:
        # Always revalidate.
        job = (
            Job.query.filter(
                Job.job_id == job_id,
                Job.employer_id == employer_info.employer_id,
                Job.status == "Open",
                Job.approval_status == "Approved",
                Job.deadline.isnot(None),
                Job.deadline >= required_deadline,
                Job.job_type != "Job Fair",
            ).first()
        )

        if not job:
            continue

        existing_entry = JobFairJob.query.filter_by(
            registration_id=registration.registration_id,
            job_id=job.job_id,
        ).first()

        if not existing_entry:
            entry = JobFairJob(
                registration_id=registration.registration_id,
                job_id=job.job_id,
            )
            db.session.add(entry)

        valid_job_count += 1

    db.session.flush()

    if valid_job_count == 0 and not exclusive_job_exists:
        db.session.rollback()
        flash(
            "At least one valid regular job or Job Fair-exclusive position is required.",
            "danger",
        )
        return redirect(
            url_for("employer.job_fair_details", job_fair_id=job_fair_id)
        )
        
        # ========================================================
    # SAVE UPDATED JOB SELECTIONS
    # ========================================================
    try:
        db.session.commit()

    except Exception as e:
        db.session.rollback()

        current_app.logger.exception(
            "Failed to update Job Fair job selections "
            "for registration %s: %s",
            registration.registration_id,
            e,
        )

        flash(
            "Unable to update your Job Fair job selections. "
            "Please try again.",
            "danger",
        )

        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id,
            )
        )

    flash(
        "Your Job Fair job selections have been updated.",
        "success",
    )

    return redirect(
        url_for(
            "employer.job_fair_details",
            job_fair_id=job_fair_id,
        )
    )


@employer.route("/job-fair/<int:job_fair_id>/register/create-exclusive", methods=["POST"])
@login_required
def create_exclusive_job_and_register(job_fair_id):
    if current_user.role != "employer":
        return render_template(
            "errors/404.html"
        ), 404

    employer_info = (
        Employer.query
        .filter_by(
            user_id=current_user.user_id
        )
        .first()
    )

    if employer_info is None:
        flash(
            "Employer not found.",
            "danger"
        )
        return redirect(
            url_for("employer.dashboard")
        )

    job_fair = JobFair.query.get_or_404(
        job_fair_id
    )

    now = datetime.now()

    # ========================================================
    # JOB FAIR MUST STILL ACCEPT REGISTRATION
    # ========================================================

    if job_fair.status != "Upcoming":
        flash(
            "This Job Fair is no longer "
            "accepting registrations.",
            "danger"
        )
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id
            )
        )

    if (
        job_fair.registration_deadline
        and now > job_fair.registration_deadline
    ):
        flash(
            "The registration deadline for "
            "this Job Fair has passed.",
            "danger"
        )
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id
            )
        )

    # ========================================================
    # MUST NOT ALREADY BE REGISTERED
    # ========================================================

    existing_registration = (
        JobFairEmployerRegistration.query
        .filter_by(
            job_fair_id=job_fair_id,
            employer_id=(
                employer_info.employer_id
            )
        )
        .first()
    )

    if existing_registration:
        flash(
            "Your company is already registered "
            "for this Job Fair.",
            "info"
        )
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id
            )
        )

    # ========================================================
    # REPRESENTATIVE INFORMATION
    # ========================================================

    representatives = _validated_optional_positive_int("representatives")

    if (
        representatives is None
        or representatives < 1
        or representatives > 15
    ):
        flash(
            "Representatives must be a whole number from 1 to 15.",
            "danger"
        )
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id
            )
        )

    simultaneous_interview_capacity = _validated_optional_positive_int("simultaneous_interview_capacity")
    if (
        simultaneous_interview_capacity is None
        or simultaneous_interview_capacity < 1
        or simultaneous_interview_capacity > representatives
        or simultaneous_interview_capacity > 15
    ):
        flash(
            "Simultaneous interview capacity must be between 1 and the number of representatives.",
            "danger",
        )
        return redirect(
            url_for("employer.job_fair_details", job_fair_id=job_fair_id)
        )

    special_requests = (
        request.form.get(
            "special_requests",
            ""
        )
        or ""
    ).strip()

    if len(special_requests) > 2000:
        flash("Special requests must not exceed 2,000 characters.", "danger")
        return redirect(url_for("employer.job_fair_details", job_fair_id=job_fair_id))

    # ========================================================
    # STRUCTURED JOB PAYLOAD
    # ========================================================

    payload, error = (
        parse_job_fair_job_payload()
    )

    if error:
        flash(error, "danger")
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id
            )
        )

    location = ", ".join(
        filter(
            None,
            [
                employer_info.street,
                employer_info.barangay,
                employer_info.city,
                employer_info.province,
            ]
        )
    )

    exclusive_deadline = (
        job_fair.end_datetime.date()
        if job_fair.end_datetime
        else job_fair.start_datetime.date()
    )

    # ========================================================
    # CREATE REGISTRATION + JOB + QUALIFICATIONS
    # ========================================================

    try:
        registration = (
            JobFairEmployerRegistration(
                job_fair_id=job_fair_id,
                employer_id=(
                    employer_info.employer_id
                ),
                representatives=representatives,
                simultaneous_interview_capacity=simultaneous_interview_capacity,
                special_requests=special_requests,
                status="Pending",
            )
        )

        db.session.add(registration)
        db.session.flush()

        job = Job(
            employer_id=(
                employer_info.employer_id
            ),
            job_title=payload[
                "job_title"
            ],
            category=payload[
                "category"
            ],
            employment_type=payload[
                "employment_type"
            ],
            salary_min=payload[
                "salary_min"
            ],
            salary_max=payload[
                "salary_max"
            ],
            location=location,
            vacancies=payload[
                "vacancies"
            ],
            description=payload[
                "description"
            ],
            minimum_education=payload[
                "minimum_education"
            ],
            minimum_experience_months=(
                payload[
                    "minimum_experience_months"
                ]
            ),
            qualifications="\n".join(
                payload["qualifications"]
            ),
            responsibilities="\n".join(
                payload["responsibilities"]
            ),
            job_type="Job Fair",
            status="Pending",
            approval_status="Pending",
            deadline=exclusive_deadline,
        )

        db.session.add(job)
        db.session.flush()

        for item in payload[
            "structured_qualifications"
        ]:
            db.session.add(
                JobQualification(
                    job_id=job.job_id,
                    qualification_code=(
                        item["code"]
                    ),
                    importance=(
                        item["importance"]
                    ),
                )
            )

        job_fair_entry = JobFairJob(
            registration_id=(
                registration.registration_id
            ),
            job_id=job.job_id,
        )

        db.session.add(job_fair_entry)

        notify_admins(
            "New Job Fair Registration",
            (
                f"{employer_info.company_name} "
                f"registered for {job_fair.title} "
                "with a new Job Fair-exclusive "
                f"position: {job.job_title}."
            ),
            "Job Fair",
        )

        notify_admins(
            "New Job Fair Job Submitted",
            (
                f"{employer_info.company_name} "
                f"submitted {job.job_title} for "
                f"{job_fair.title}."
            ),
            "Job Fair",
        )

        db.session.commit()

    except Exception as error:
        db.session.rollback()

        current_app.logger.exception(
            "Unable to create exclusive Job Fair "
            "registration for employer %s: %s",
            employer_info.employer_id,
            error,
        )

        flash(
            "Unable to create the Job Fair "
            "registration. Please try again.",
            "danger"
        )

        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id
            )
        )

    flash(
        "Your Job Fair registration and "
        "exclusive position were submitted "
        "for review.",
        "success"
    )

    return redirect(
        url_for(
            "employer.job_fair_details",
            job_fair_id=job_fair_id
        )
    )


@employer.route("/job-fair/<int:job_fair_id>/register/create-job", methods=["POST"])
@login_required
def create_job_fair_job(job_fair_id):
    if current_user.role != "employer":
        return render_template(
            "errors/404.html"
        ), 404

    employer_info = (
        Employer.query
        .filter_by(
            user_id=current_user.user_id
        )
        .first()
    )

    if employer_info is None:
        flash(
            "Employer not found.",
            "danger"
        )
        return redirect(
            url_for("employer.dashboard")
        )

    job_fair = JobFair.query.get_or_404(
        job_fair_id
    )

    registration = (
        JobFairEmployerRegistration.query
        .filter_by(
            job_fair_id=job_fair_id,
            employer_id=(
                employer_info.employer_id
            )
        )
        .first()
    )

    if registration is None:
        flash(
            "Please register for the Job Fair first.",
            "danger"
        )
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id
            )
        )

    if registration.status != "Pending":
        flash(
            "You cannot add jobs after your "
            "registration has been reviewed.",
            "danger"
        )
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id
            )
        )

    if job_fair.status != "Upcoming":
        flash(
            "You can only create jobs while "
            "the Job Fair is upcoming.",
            "danger"
        )
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id
            )
        )

    now = datetime.now()

    if (
        job_fair.registration_deadline
        and now > job_fair.registration_deadline
    ):
        flash(
            "The registration deadline for "
            "this Job Fair has passed.",
            "danger"
        )
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id
            )
        )

    payload, error = (
        parse_job_fair_job_payload()
    )

    if error:
        flash(error, "danger")
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id
            )
        )

    location = ", ".join(
        filter(
            None,
            [
                employer_info.street,
                employer_info.barangay,
                employer_info.city,
                employer_info.province,
            ]
        )
    )

    exclusive_deadline = (
        job_fair.end_datetime.date()
        if job_fair.end_datetime
        else job_fair.start_datetime.date()
    )

    try:
        job = Job(
            employer_id=(
                employer_info.employer_id
            ),
            job_title=payload[
                "job_title"
            ],
            category=payload[
                "category"
            ],
            employment_type=payload[
                "employment_type"
            ],
            salary_min=payload[
                "salary_min"
            ],
            salary_max=payload[
                "salary_max"
            ],
            location=location,
            vacancies=payload[
                "vacancies"
            ],
            description=payload[
                "description"
            ],
            minimum_education=payload[
                "minimum_education"
            ],
            minimum_experience_months=(
                payload[
                    "minimum_experience_months"
                ]
            ),
            qualifications="\n".join(
                payload["qualifications"]
            ),
            responsibilities="\n".join(
                payload["responsibilities"]
            ),
            job_type="Job Fair",
            status="Pending",
            approval_status="Pending",
            deadline=exclusive_deadline,
        )

        db.session.add(job)
        db.session.flush()

        for item in payload[
            "structured_qualifications"
        ]:
            db.session.add(
                JobQualification(
                    job_id=job.job_id,
                    qualification_code=(
                        item["code"]
                    ),
                    importance=(
                        item["importance"]
                    ),
                )
            )

        db.session.add(
            JobFairJob(
                registration_id=(
                    registration.registration_id
                ),
                job_id=job.job_id,
            )
        )

        notify_admins(
            "New Job Fair Job Submitted",
            (
                f"{employer_info.company_name} "
                f"submitted {job.job_title} for "
                f"{job_fair.title}."
            ),
            "Job Fair",
        )

        db.session.commit()

    except Exception as error:
        db.session.rollback()

        current_app.logger.exception(
            "Unable to create Job Fair job for "
            "employer %s: %s",
            employer_info.employer_id,
            error,
        )

        flash(
            "Unable to add the Job Fair job. "
            "Please try again.",
            "danger"
        )

        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id
            )
        )

    flash(
        "The new job has been added to your "
        "Job Fair registration and submitted "
        "for review.",
        "success"
    )

    return redirect(
        url_for(
            "employer.job_fair_details",
            job_fair_id=job_fair_id
        )
    )



# ============================================================
# EDIT JOB FAIR-EXCLUSIVE JOB
# ============================================================

@employer.route(
    "/job-fair/<int:job_fair_id>/exclusive-job/<int:job_id>/update",
    methods=["POST"],
)
@login_required
def update_job_fair_exclusive_job(
    job_fair_id,
    job_id,
):
    (
        employer_info,
        job_fair,
        registration,
        entry,
        job,
        guard_error,
    ) = get_owned_exclusive_job_context(
        job_fair_id,
        job_id,
        require_editable=True,
    )

    if guard_error:
        flash(guard_error, "danger")
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id,
            )
        )

    payload, error = parse_job_fair_job_payload()

    if error:
        flash(error, "danger")
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id,
            )
        )

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

    deadline = (
        job_fair.end_datetime.date()
        if job_fair.end_datetime
        else job_fair.start_datetime.date()
    )

    try:
        job.job_title = payload["job_title"]
        job.category = payload["category"]
        job.employment_type = payload[
            "employment_type"
        ]
        job.salary_min = payload["salary_min"]
        job.salary_max = payload["salary_max"]
        job.location = location
        job.vacancies = payload["vacancies"]
        job.description = payload["description"]

        job.minimum_education = payload[
            "minimum_education"
        ]
        job.minimum_experience_months = payload[
            "minimum_experience_months"
        ]

        job.qualifications = "\n".join(
            payload["qualifications"]
        )
        job.responsibilities = "\n".join(
            payload["responsibilities"]
        )

        job.deadline = deadline

        # Editing a rejected/flagged/unreviewed job means the
        # employer is resubmitting it for PESO review.
        job.approval_status = "Pending"
        job.status = "Pending"
        job.remarks = None
        job.reviewed_by = None
        job.reviewed_at = None

        JobQualification.query.filter_by(
            job_id=job.job_id
        ).delete(
            synchronize_session=False
        )

        for item in payload[
            "structured_qualifications"
        ]:
            db.session.add(
                JobQualification(
                    job_id=job.job_id,
                    qualification_code=item["code"],
                    importance=item["importance"],
                )
            )

        notify_admins(
            "Job Fair Job Resubmitted",
            (
                f"{employer_info.company_name} updated and "
                f"resubmitted '{job.job_title}' for "
                f"{job_fair.title}."
            ),
            "Job Fair",
        )

        db.session.commit()

    except Exception as error:
        db.session.rollback()

        current_app.logger.exception(
            "Unable to update Job Fair-exclusive job %s: %s",
            job_id,
            error,
        )

        flash(
            "Unable to update the Job Fair position.",
            "danger",
        )

        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id,
            )
        )

    flash(
        "The Job Fair position was updated and resubmitted for PESO review.",
        "success",
    )

    return redirect(
        url_for(
            "employer.job_fair_details",
            job_fair_id=job_fair_id,
        )
    )


# ============================================================
# DELETE PENDING JOB FAIR-EXCLUSIVE JOB
# ============================================================

@employer.route(
    "/job-fair/<int:job_fair_id>/exclusive-job/<int:job_id>/delete",
    methods=["POST"],
)
@login_required
def delete_job_fair_exclusive_job(
    job_fair_id,
    job_id,
):
    (
        employer_info,
        job_fair,
        registration,
        entry,
        job,
        guard_error,
    ) = get_owned_exclusive_job_context(
        job_fair_id,
        job_id,
        require_editable=True,
    )

    if guard_error:
        flash(guard_error, "danger")
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id,
            )
        )

    # Never cascade-delete real applicant/evaluation history.
    has_applications = (
        Application.query
        .filter_by(job_id=job.job_id)
        .first()
        is not None
    )

    has_job_fair_activity = bool(
        entry.candidate_evaluations
        or entry.preselections
        or entry.queue_groupings
    )

    if has_applications or has_job_fair_activity:
        flash(
            "This position already has applicant or Job Fair activity and cannot be deleted.",
            "danger",
        )
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id,
            )
        )

    remaining_entries = [
        selected_entry
        for selected_entry in registration.selected_jobs
        if selected_entry.job_id != job.job_id
    ]

    if not remaining_entries:
        flash(
            "You cannot delete the last position from the registration. Cancel the Job Fair registration instead.",
            "warning",
        )
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id,
            )
        )

    job_title = job.job_title

    try:
        db.session.delete(job)

        notify_admins(
            "Job Fair Job Removed",
            (
                f"{employer_info.company_name} removed "
                f"'{job_title}' from {job_fair.title} "
                "before registration approval."
            ),
            "Job Fair",
        )

        db.session.commit()

    except Exception as error:
        db.session.rollback()

        current_app.logger.exception(
            "Unable to delete Job Fair-exclusive job %s: %s",
            job_id,
            error,
        )

        flash(
            "Unable to remove the Job Fair position.",
            "danger",
        )

        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id,
            )
        )

    flash(
        "The Job Fair-exclusive position was removed.",
        "success",
    )

    return redirect(
        url_for(
            "employer.job_fair_details",
            job_fair_id=job_fair_id,
        )
    )


# ============================================================
# CANCEL PENDING EMPLOYER JOB FAIR REGISTRATION
# ============================================================

@employer.route(
    "/job-fair/<int:job_fair_id>/registration/cancel",
    methods=["POST"],
)
@login_required
def cancel_job_fair_registration(job_fair_id):
    employer_info = (
        get_current_employer_for_job_fair()
    )

    job_fair = JobFair.query.get_or_404(
        job_fair_id
    )

    registration = (
        JobFairEmployerRegistration.query
        .filter_by(
            job_fair_id=job_fair_id,
            employer_id=employer_info.employer_id,
        )
        .first_or_404()
    )

    if registration.status != "Pending":
        flash(
            "Only pending Job Fair registrations can be cancelled.",
            "danger",
        )
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id,
            )
        )

    if job_fair.status != "Upcoming":
        flash(
            "This registration can no longer be cancelled because the Job Fair has already started or finished.",
            "danger",
        )
        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id,
            )
        )

    try:
        registration.status = "Cancelled"
        registration.booth_number = None

        close_exclusive_jobs_for_registration(
            registration,
            reason=(
                "Employer cancelled the Job Fair registration."
            ),
        )

        notify_admins(
            "Job Fair Registration Cancelled",
            (
                f"{employer_info.company_name} cancelled "
                f"its registration for {job_fair.title}."
            ),
            "Job Fair",
        )

        db.session.commit()

    except Exception as error:
        db.session.rollback()

        current_app.logger.exception(
            "Unable to cancel Job Fair registration %s: %s",
            registration.registration_id,
            error,
        )

        flash(
            "Unable to cancel the Job Fair registration.",
            "danger",
        )

        return redirect(
            url_for(
                "employer.job_fair_details",
                job_fair_id=job_fair_id,
            )
        )

    flash(
        "Your Job Fair registration was cancelled.",
        "success",
    )

    return redirect(
        url_for(
            "employer.job_fair_details",
            job_fair_id=job_fair_id,
        )
    )


def get_application(application_id):
    employer_info = Employer.query.filter_by(
        user_id=current_user.user_id
    ).first()

    if employer_info is None:
        abort(404)

    application = Application.query.get_or_404(application_id)

    if application.job.employer_id != employer_info.employer_id:
        abort(403)

    return application


def get_job_vacancy_info(job):
    accepted_count = (
        Application.query
        .filter_by(
            job_id=job.job_id,
            status="Accepted"
        )
        .count()
    )

    remaining_vacancies = max(job.vacancies - accepted_count, 0)

    return (accepted_count, remaining_vacancies)