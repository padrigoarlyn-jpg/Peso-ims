from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from flask import (
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required
from website.extensions import db
from website.models import (
    Application,
    Employer,
    Job,
    JobQualification,
    JobFairCandidateEvaluation,
    Referral,
)
from website.utils.notifications import notify_admins
from website.utils.job_requirements import (
    VALID_EDUCATION_LEVELS,
    VALID_EXPERIENCE_MONTHS,
    parse_structured_qualifications,
)

from . import employer
from .forms import JobForm


# --- HELPER FUNCTIONS ---
def get_current_employer():
    """Return the Employer record belonging to the logged-in user."""
    return Employer.query.filter_by(user_id=current_user.user_id).first()


def get_company_location(employer_info):
    """Build the employer's full address."""
    parts = [
        employer_info.street,
        employer_info.barangay,
        employer_info.city,
        employer_info.province,
    ]
    return ", ".join(filter(None, parts)) or "No location provided"


def get_job_vacancy_info(job):
    """Return accepted applications and remaining vacancies."""
    accepted_count = Application.query.filter_by(
        job_id=job.job_id, status="Accepted"
    ).count()

    remaining_vacancies = max(job.vacancies - accepted_count, 0)
    return accepted_count, remaining_vacancies


def get_employer_visible_applicant_count(job_id):
    """Count applicants visible to the employer through PESO referral or Job Fair evaluation."""
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

    return (
        Application.query
        .filter(
            Application.job_id == job_id,
            db.or_(referred_exists, evaluation_exists),
        )
        .count()
    )


def parse_salary(value):
    """Safely convert a formatted salary string to Decimal."""

    if value is None:
        return None

    try:
        cleaned_value = str(value).replace(",", "").strip()

        if not cleaned_value:
            return None

        parsed = Decimal(cleaned_value)
        return parsed if parsed.is_finite() else None

    except (InvalidOperation, TypeError, ValueError):
        return None


def parse_vacancies(value):
    """Safely convert vacancies to an integer."""
    try:
        raw = str(value).strip()
        if not raw.isascii() or not raw.isdecimal():
            return None
        return int(raw)
    except (TypeError, ValueError):
        return None
    


# --- JOB LISTING ---

@employer.route("/job_listing")
@login_required
def joblisting():
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_info = get_current_employer()
    if employer_info is None:
        flash("Employer profile not found.", "danger")
        return redirect(url_for("auth.login"))

    form = JobForm()
    company_location = get_company_location(employer_info)
    page = request.args.get("page", 1, type=int)

    jobs = (
        Job.query.filter_by(employer_id=employer_info.employer_id)
        .order_by(Job.created_at.desc())
        .paginate(page=page, per_page=15)
    )

    for job in jobs.items:
        accepted_count, remaining_vacancies = get_job_vacancy_info(job)

        job.accepted_count = accepted_count
        job.remaining_vacancies = remaining_vacancies
        job.visible_applicant_count = get_employer_visible_applicant_count(job.job_id)
        job.is_in_job_fair = bool(job.job_fair_entries)

        job.job_fair_id = None
        if job.job_fair_entries:
            first_entry = job.job_fair_entries[0]
            if first_entry.registration:
                job.job_fair_id = first_entry.registration.job_fair_id

        job.structured_qualification_payload = [
            {
                "code":
                    qualification.qualification_code,

                "importance":
                    qualification.importance,
            }

            for qualification
            in job.structured_qualifications
        ]

    return render_template(
        "employer/employer_joblist.html",
        current_user=current_user,
        employer=employer_info,
        form=form,
        jobs=jobs,
        company_location=company_location,
    )


# --- CREATE JOB ---

@employer.route("/create_job", methods=["POST"])
@login_required
def create_job():
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_info = get_current_employer()

    if employer_info is None:
        flash("Employer profile not found.", "danger")
        return redirect(url_for("auth.logout"))

    employment_type = (request.form.get("employment_type", "") or "").strip()

    allowed_employment_types = {
        "Full Time",
        "Part Time",
    }

    if employment_type not in allowed_employment_types:
        flash("Please select a valid employment type.", "danger")
        return redirect(url_for("employer.joblisting"))

    salary_min = parse_salary(request.form.get("salary_min"))
    salary_max = parse_salary(request.form.get("salary_max"))

    if salary_min is None or salary_max is None:
        flash("Please enter valid salary amounts.", "danger")
        return redirect(url_for("employer.joblisting"))

    if salary_min < 0 or salary_max < 0:
        flash("Salary cannot be negative.", "danger")
        return redirect(url_for("employer.joblisting"))

    if salary_min > salary_max:
        flash("Minimum salary cannot exceed maximum salary.", "danger")
        return redirect(url_for("employer.joblisting"))

    vacancies = parse_vacancies(request.form.get("vacancies"))

    if vacancies is None or vacancies <= 0 or vacancies > 100000:
        flash("Vacancies must be a whole number from 1 to 100,000.", "danger")
        return redirect(url_for("employer.joblisting"))

    minimum_education = (
        request.form.get("minimum_education", "NONE")
        or "NONE"
    ).strip()

    if minimum_education not in VALID_EDUCATION_LEVELS:
        flash("Please select a valid minimum education requirement.", "danger")
        return redirect(url_for("employer.joblisting"))

    try:
        minimum_experience_months = int(
            request.form.get("minimum_experience_months", 0)
        )
    except (TypeError, ValueError):
        flash("Please select a valid minimum experience requirement.", "danger")
        return redirect(url_for("employer.joblisting"))

    if minimum_experience_months not in VALID_EXPERIENCE_MONTHS:
        flash("Please select a valid minimum experience requirement.", "danger")
        return redirect(url_for("employer.joblisting"))

    structured_qualifications = parse_structured_qualifications(request.form)

    if not structured_qualifications:
        flash("Please select at least one common qualification.", "danger")
        return redirect(url_for("employer.joblisting"))

    qualifications = [
        value.strip()
        for value in request.form.getlist("qualification[]")
        if value.strip()
    ]

    if not qualifications:
        flash("Please provide at least one additional qualification.", "danger")
        return redirect(url_for("employer.joblisting"))

    responsibilities = [
        value.strip()
        for value in request.form.getlist("responsibility[]")
        if value.strip()
    ]

    if not responsibilities:
        flash("Please provide at least one job responsibility.", "danger")
        return redirect(url_for("employer.joblisting"))

    location = get_company_location(employer_info)

    job_title = (request.form.get("job_title", "") or "").strip()
    category = (request.form.get("category", "") or "").strip()
    description = (request.form.get("description", "") or "").strip()

    if not job_title:
        flash("Job title is required.", "danger")
        return redirect(url_for("employer.joblisting"))

    if not category:
        flash("Job category is required.", "danger")
        return redirect(url_for("employer.joblisting"))

    if not description:
        flash("Job description is required.", "danger")
        return redirect(url_for("employer.joblisting"))

    if len(job_title) > 150:
        flash("Job title must not exceed 150 characters.", "danger")
        return redirect(url_for("employer.joblisting"))

    if len(category) > 100:
        flash("Job category must not exceed 100 characters.", "danger")
        return redirect(url_for("employer.joblisting"))

    if len(description) > 10000:
        flash("Job description must not exceed 10,000 characters.", "danger")
        return redirect(url_for("employer.joblisting"))

    if len(qualifications) > 30 or any(len(value) > 500 for value in qualifications):
        flash("Qualifications contain too many or overly long entries.", "danger")
        return redirect(url_for("employer.joblisting"))

    if len(responsibilities) > 30 or any(len(value) > 500 for value in responsibilities):
        flash("Responsibilities contain too many or overly long entries.", "danger")
        return redirect(url_for("employer.joblisting"))

    duplicate_cutoff = datetime.utcnow() - timedelta(seconds=60)

    existing_duplicate = (
        Job.query
        .filter(
            Job.employer_id == employer_info.employer_id,
            db.func.lower(Job.job_title) == job_title.lower(),
            Job.category == category,
            Job.employment_type == employment_type,
            Job.vacancies == vacancies,
            Job.salary_min == salary_min,
            Job.salary_max == salary_max,
            Job.minimum_education == minimum_education,
            Job.minimum_experience_months == minimum_experience_months,
            Job.created_at >= duplicate_cutoff,
        )
        .first()
    )

    if existing_duplicate:
        flash(
            "This job appears to have already been submitted. Please wait before submitting the same job again.",
            "warning",
        )
        return redirect(url_for("employer.joblisting"))

    job = Job(
        employer_id=employer_info.employer_id,
        job_title=job_title,
        category=category,
        description=description,
        employment_type=employment_type,
        vacancies=vacancies,
        salary_min=salary_min,
        salary_max=salary_max,
        location=location,
        minimum_education=minimum_education,
        minimum_experience_months=minimum_experience_months,
        job_type="Referral",
        deadline=None,
        status="Pending",
        approval_status="Pending",
        qualifications="\n".join(qualifications),
        responsibilities="\n".join(responsibilities),
    )

    try:
        db.session.add(job)
        db.session.flush()

        for item in structured_qualifications:
            qualification = JobQualification(
                job_id=job.job_id,
                qualification_code=item["code"],
                importance=item["importance"],
            )
            db.session.add(qualification)

        notify_admins(
            "New Job Submitted",
            f"{employer_info.company_name} submitted {job.job_title} for review.",
            "Job",
        )

        db.session.commit()

    except Exception as error:
        db.session.rollback()
        print("CREATE JOB ERROR:", error)
        flash("Failed to create job listing.", "danger")
        return redirect(url_for("employer.joblisting"))

    flash("Job submitted successfully and is awaiting admin review.", "success")
    return redirect(url_for("employer.joblisting"))


# --- UPDATE JOB ---

@employer.route("/update_job", methods=["POST"])
@login_required
def update_job():
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_info = get_current_employer()
    if employer_info is None:
        flash("Employer profile not found.", "danger")
        return redirect(url_for("auth.login"))

    job_id = request.form.get("job_id")
    if not job_id:
        flash("Invalid job listing.", "danger")
        return redirect(url_for("employer.joblisting"))

    job = Job.query.get_or_404(job_id)

    if job.employer_id != employer_info.employer_id:
        flash("Unauthorized.", "danger")
        return redirect(url_for("employer.joblisting"))

    if job.approval_status == "Approved":
        flash("Approved jobs cannot be edited.", "danger")
        return redirect(url_for("employer.joblisting"))

    if job.approval_status == "Rejected":
        flash(
            "Rejected jobs cannot be edited or resubmitted. Create a new job listing if you want to submit a revised posting.",
            "warning",
        )
        return redirect(url_for("employer.joblisting"))

    was_flagged = job.approval_status == "Flagged"

    salary_min = parse_salary(request.form.get("salary_min"))
    salary_max = parse_salary(request.form.get("salary_max"))

    if salary_min is None or salary_max is None:
        flash("Please enter valid salary amounts.", "danger")
        return redirect(url_for("employer.joblisting"))

    if salary_min < 0 or salary_max < 0:
        flash("Salary cannot be negative.", "danger")
        return redirect(url_for("employer.joblisting"))

    if salary_min > salary_max:
        flash("Minimum salary cannot exceed maximum salary.", "danger")
        return redirect(url_for("employer.joblisting"))

    vacancies = parse_vacancies(request.form.get("vacancies"))
    if vacancies is None or vacancies <= 0 or vacancies > 100000:
        flash("Vacancies must be a whole number from 1 to 100,000.", "danger")
        return redirect(url_for("employer.joblisting"))

    minimum_education = (
        request.form.get("minimum_education", "NONE")
        or "NONE"
    ).strip()

    if minimum_education not in VALID_EDUCATION_LEVELS:
        flash("Please select a valid minimum education requirement.", "danger")
        return redirect(url_for("employer.joblisting"))

    try:
        minimum_experience_months = int(
            request.form.get("minimum_experience_months", 0)
        )
    except (TypeError, ValueError):
        flash("Please select a valid minimum experience requirement.", "danger")
        return redirect(url_for("employer.joblisting"))

    if minimum_experience_months not in VALID_EXPERIENCE_MONTHS:
        flash("Please select a valid minimum experience requirement.", "danger")
        return redirect(url_for("employer.joblisting"))

    structured_qualifications = parse_structured_qualifications(request.form)

    if not structured_qualifications:
        flash("Please select at least one common qualification.", "danger")
        return redirect(url_for("employer.joblisting"))

    employment_type = request.form.get("employment_type", "").strip()
    allowed_employment_types = {"Full Time", "Part Time"}

    if employment_type not in allowed_employment_types:
        flash("Please select a valid employment type.", "danger")
        return redirect(url_for("employer.joblisting"))

    job_title = (request.form.get("job_title") or "").strip()
    category = (request.form.get("category") or "").strip()
    description = (request.form.get("description") or "").strip()

    if not job_title or not category or not description:
        flash("Job title, category, and description are required.", "danger")
        return redirect(url_for("employer.joblisting"))
    if len(job_title) > 150 or len(category) > 100:
        flash("Job title or category exceeds the allowed length.", "danger")
        return redirect(url_for("employer.joblisting"))
    if len(description) > 10000:
        flash("Job description must not exceed 10,000 characters.", "danger")
        return redirect(url_for("employer.joblisting"))

    job.job_title = job_title
    job.category = category
    job.location = get_company_location(employer_info)
    job.employment_type = employment_type
    job.vacancies = vacancies
    job.salary_min = salary_min
    job.salary_max = salary_max
    job.description = description
    job.minimum_education = minimum_education
    job.minimum_experience_months = minimum_experience_months

    qualifications = [
        val.strip() for val in request.form.getlist("qualification[]") if val.strip()
    ]
    responsibilities = [
        val.strip() for val in request.form.getlist("responsibility[]") if val.strip()
    ]

    if not qualifications or not responsibilities:
        flash("Please provide at least one qualification and responsibility.", "danger")
        return redirect(url_for("employer.joblisting"))
    if len(qualifications) > 30 or any(len(value) > 500 for value in qualifications):
        flash("Qualifications contain too many or overly long entries.", "danger")
        return redirect(url_for("employer.joblisting"))
    if len(responsibilities) > 30 or any(len(value) > 500 for value in responsibilities):
        flash("Responsibilities contain too many or overly long entries.", "danger")
        return redirect(url_for("employer.joblisting"))

    job.qualifications = "\n".join(qualifications)
    job.responsibilities = "\n".join(responsibilities)

    JobQualification.query.filter_by(
        job_id=job.job_id
    ).delete(
        synchronize_session=False
    )

    for item in structured_qualifications:
        job_qualification = JobQualification(
            job_id=job.job_id,
            qualification_code=item["code"],
            importance=item["importance"],
        )
        db.session.add(job_qualification)

    requested_status = request.form.get("status")
    if requested_status is not None and requested_status not in {"Closed", "Draft"}:
        flash("Please select a valid job status.", "danger")
        return redirect(url_for("employer.joblisting"))
    if requested_status in {"Closed", "Draft"}:
        job.status = requested_status

    if was_flagged:
        job.approval_status = "Pending"
        job.status = "Pending"
        job.reviewed_by = None
        job.reviewed_at = None

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Failed to update job listing.", "danger")
        return redirect(url_for("employer.joblisting"))

    if was_flagged:
        flash(
            "Corrections saved. The job has been resubmitted to PESO for review.",
            "success",
        )
    else:
        flash("Job updated successfully!", "success")
    return redirect(url_for("employer.joblisting"))



# --- CLOSE JOB ---

@employer.route("/close_job/<int:job_id>", methods=["POST"])
@login_required
def close_job(job_id):
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_info = get_current_employer()
    if employer_info is None:
        flash("Employer profile not found.", "danger")
        return redirect(url_for("auth.login"))

    job = Job.query.get_or_404(job_id)

    if job.employer_id != employer_info.employer_id:
        flash("Unauthorized.", "danger")
        return redirect(url_for("employer.joblisting"))

    if job.job_type == "Job Fair":
        flash(
            "Job Fair-exclusive positions are controlled from the Job Fair module.",
            "warning",
        )
        return redirect(url_for("employer.joblisting"))

    if job.approval_status != "Approved" or job.status != "Open":
        flash("Only approved open jobs can be closed.", "warning")
        return redirect(url_for("employer.joblisting"))

    job.status = "Closed"

    try:
        db.session.commit()
        flash(f"{job.job_title} has been closed.", "success")
    except Exception:
        db.session.rollback()
        flash("Failed to close the job listing.", "danger")

    return redirect(url_for("employer.joblisting"))


# --- REOPEN JOB ---

@employer.route("/reopen_job/<int:job_id>", methods=["POST"])
@login_required
def reopen_job(job_id):
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_info = get_current_employer()
    if employer_info is None:
        flash("Employer profile not found.", "danger")
        return redirect(url_for("auth.login"))

    job = Job.query.get_or_404(job_id)

    if job.employer_id != employer_info.employer_id:
        flash("Unauthorized.", "danger")
        return redirect(url_for("employer.joblisting"))

    if job.status != "Closed":
        flash("Only closed jobs can be reopened.", "warning")
        return redirect(url_for("employer.joblisting"))

    if job.approval_status != "Approved":
        flash("Only approved jobs can be reopened.", "warning")
        return redirect(url_for("employer.joblisting"))

    accepted_count, remaining_vacancies = get_job_vacancy_info(job)
    if remaining_vacancies <= 0:
        flash(
            "This job cannot be reopened because all vacancies have been filled.",
            "warning",
        )
        return redirect(url_for("employer.joblisting"))

    job.status = "Open"
    job.deadline = date.today() + timedelta(days=7)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Failed to reopen the job listing.", "danger")
        return redirect(url_for("employer.joblisting"))

    vac_str = "vacancy remains" if remaining_vacancies == 1 else "vacancies remain"
    flash(
        f"{job.job_title} has been reopened successfully. {remaining_vacancies} {vac_str}.",
        "success",
    )
    return redirect(url_for("employer.joblisting"))


# --- DELETE JOB ---

@employer.route("/delete_job/<int:job_id>", methods=["POST"])
@login_required
def delete_job(job_id):
    if current_user.role != "employer":
        return render_template("errors/404.html"), 404

    employer_info = get_current_employer()
    if employer_info is None:
        flash("Employer profile not found.", "danger")
        return redirect(url_for("auth.login"))

    job = Job.query.get_or_404(job_id)

    if job.employer_id != employer_info.employer_id:
        flash("Unauthorized.", "danger")
        return redirect(url_for("employer.joblisting"))

    try:
        db.session.delete(job)
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash("Failed to delete job listing.", "danger")
        return redirect(url_for("employer.joblisting"))

    flash("Job deleted successfully!", "success")
    return redirect(url_for("employer.joblisting"))