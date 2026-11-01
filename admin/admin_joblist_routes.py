import csv
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import os
import re
import secrets

from flask import (
    abort,
    current_app,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
    send_from_directory,
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
    JobQualification,
    JobFair,
    JobFairEmployerRegistration,
    JobFairJob,
    JobFairRegistration,
    JobSeeker,
    JobSeekerLicense,
    JobSeekerTVET,
    JobSeekerWorkHistory,
    Notification,
    Referral,
    User,
)
from website.utils.email import send_email
from website.utils.notifications import notify_role, notify_user
from website.utils.audit import record_audit
from website.utils.application_ranking import rank_job_applications
from website.utils.job_fair_lifecycle import (
    get_job_fair_for_job,
)

from . import admin


# --- ADMIN ACCESS GUARD ---

def check_admin_access():
    return bool(current_user.is_authenticated and current_user.role == "admin")


# --- JOBS LIST & FILTERING ---

@admin.route("/joblist")
@login_required
def joblist():
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    status = request.args.get("status", "All")
    page = request.args.get("page", 1, type=int)
    availability = request.args.get("availability", "All")
    current_search = request.args.get("q", "").strip()

    if availability not in ("All", "Open", "Closed"):
        availability = "All"

    query = Job.query

    # Any job linked through an approved Job Fair registration
    # is considered a Job Fair participant.
    job_fair_job_ids = (
        db.session.query(JobFairJob.job_id)
        .join(
            JobFairEmployerRegistration,
            JobFairJob.registration_id
            == JobFairEmployerRegistration.registration_id,
        )
        .filter(
            JobFairEmployerRegistration.status == "Approved"
        )
        .distinct()
    )

    # Approval status and job-source filters.
    if status in ("Pending", "Approved", "Flagged", "Rejected"):
        query = query.filter(Job.approval_status == status)

    elif status == "Job Fair":
        query = query.filter(Job.job_id.in_(job_fair_job_ids))

    elif status == "Referral":
        query = query.filter(Job.job_type == "Referral")

    # Availability is separate from approval status.
    if availability != "All":
        query = query.filter(Job.status == availability)

    # Search across job title, description, location, and employer.
    if current_search:
        search_term = f"%{current_search}%"

        query = query.filter(
            or_(
                Job.job_title.ilike(search_term),
                Job.description.ilike(search_term),
                Job.location.ilike(search_term),
                Job.employer.has(
                    Employer.company_name.ilike(search_term)
                ),
            )
        )

    jobs = query.order_by(
        Job.created_at.desc()
    ).paginate(
        page=page,
        per_page=15,
        error_out=False,
    )

    def first_available_value(obj, field_names):
        """Return the first populated value among possible model fields."""
        for field_name in field_names:
            value = getattr(obj, field_name, None)

            if value is not None and str(value).strip():
                return value

        return None

    def format_salary_amount(value):
        """Format numeric salary amounts without altering text values."""
        if value is None:
            return None

        try:
            amount = float(value)

            if amount.is_integer():
                return f"₱{amount:,.0f}"

            return f"₱{amount:,.2f}"

        except (TypeError, ValueError):
            return str(value).strip()

    # Prepare all data needed by the review modal.
    for job in jobs.items:
        accepted_count = (
            Application.query
            .filter_by(
                job_id=job.job_id,
                status="Accepted",
            )
            .count()
        )

        job.accepted_count = accepted_count
        job.remaining_vacancies = max(
            int(job.vacancies or 0) - accepted_count,
            0,
        )

        # Salary support: use whichever salary fields your Job model has.
        # If your model uses different names, add them to these lists.
        direct_salary = first_available_value(
            job,
            ("salary_range", "salary", "salary_display"),
        )

        salary_min = first_available_value(
            job,
            (
                "salary_min",
                "min_salary",
                "minimum_salary",
                "salary_minimum",
            ),
        )

        salary_max = first_available_value(
            job,
            (
                "salary_max",
                "max_salary",
                "maximum_salary",
                "salary_maximum",
            ),
        )

        salary_period = first_available_value(
            job,
            (
                "salary_period",
                "pay_period",
                "salary_frequency",
            ),
        )

        if direct_salary is not None:
            # Preserve an existing salary range or descriptive value.
            try:
                float(direct_salary)
                salary_display = format_salary_amount(direct_salary)
            except (TypeError, ValueError):
                salary_display = str(direct_salary).strip()
        elif salary_min is not None or salary_max is not None:
            minimum_text = format_salary_amount(salary_min)
            maximum_text = format_salary_amount(salary_max)

            if minimum_text and maximum_text:
                salary_display = (
                    minimum_text
                    if minimum_text == maximum_text
                    else f"{minimum_text} - {maximum_text}"
                )
            else:
                salary_display = minimum_text or maximum_text
        else:
            salary_display = "Not specified"

        if salary_period and salary_display != "Not specified":
            salary_display = f"{salary_display} / {salary_period}"

        job.salary_display = salary_display

        # Load qualifications as a single structured payload.
        qualifications = (
            JobQualification.query
            .filter_by(job_id=job.job_id)
            .order_by(
                JobQualification.job_qualification_id.asc()
            )
            .all()
        )

        job.structured_qualification_payload = [
            {
                "code": qualification.qualification_code,
                "importance": qualification.importance,
            }
            for qualification in qualifications
        ]

        # Load approved Job Fair participation details.
        job_fair_links = (
            JobFairJob.query
            .join(
                JobFairEmployerRegistration,
                JobFairJob.registration_id
                == JobFairEmployerRegistration.registration_id,
            )
            .join(
                JobFair,
                JobFairEmployerRegistration.job_fair_id
                == JobFair.job_fair_id,
            )
            .filter(
                JobFairJob.job_id == job.job_id,
                JobFairEmployerRegistration.status == "Approved",
            )
            .order_by(JobFair.start_datetime.desc())
            .all()
        )

        job.job_fair_participations = [
            {
                "title": link.registration.job_fair.title,
                "status": link.registration.job_fair.status,
                "job_fair_id": link.registration.job_fair_id,
            }
            for link in job_fair_links
            if link.registration and link.registration.job_fair
        ]

        job.is_job_fair_participant = bool(
            job.job_fair_participations
        )

    # Summary counts.
    total_jobs = Job.query.count()

    pending_count = Job.query.filter(
        Job.approval_status == "Pending"
    ).count()

    approved_count = Job.query.filter(
        Job.approval_status == "Approved"
    ).count()

    flagged_count = Job.query.filter(
        Job.approval_status == "Flagged"
    ).count()

    rejected_count = Job.query.filter(
        Job.approval_status == "Rejected"
    ).count()

    referral_count = Job.query.filter(
        Job.job_type == "Referral"
    ).count()

    job_fair_count = (
        db.session.query(
            func.count(func.distinct(JobFairJob.job_id))
        )
        .join(
            JobFairEmployerRegistration,
            JobFairJob.registration_id
            == JobFairEmployerRegistration.registration_id,
        )
        .filter(
            JobFairEmployerRegistration.status == "Approved"
        )
        .scalar()
        or 0
    )

    return render_template(
        "admin/admin_joblist.html",
        jobs=jobs,
        current_status=status,
        current_availability=availability,
        current_search=current_search,
        total_jobs=total_jobs,
        pending_count=pending_count,
        approved_count=approved_count,
        flagged_count=flagged_count,
        rejected_count=rejected_count,
        referral_count=referral_count,
        job_fair_count=job_fair_count,
    )


# --- FLAGGED JOBS MANAGEMENT ---

@admin.route("/flagged_page")
@login_required
def flagged_page():
    if not check_admin_access():
        return render_template("errors/404.html"), 404

    page = request.args.get("page", 1, type=int)
    flagged_jobs = (
        Job.query.filter(Job.approval_status == "Flagged")
        .order_by(Job.reviewed_at.desc())
        .paginate(page=page, per_page=15, error_out=False)
    )

    return render_template("admin/admin_flagged_page.html", jobs=flagged_jobs)


# --- JOB APPROVAL ROUTE ---

@admin.route("/job/<int:job_id>/status", methods=["POST"])
@login_required
def update_job_status(job_id):
    if not check_admin_access():
        return jsonify({
            "success": False,
            "message": "Unauthorized"
        }), 403

    job = Job.query.get_or_404(job_id)

    data = request.get_json(silent=True)
    if data is None:
        data = request.form
    if not hasattr(data, "get"):
        return jsonify({
            "success": False,
            "message": "Invalid request format.",
        }), 400

    status = data.get("status")
    raw_remarks = data.get("remarks", "")
    if not isinstance(status, str) or not isinstance(raw_remarks, str):
        return jsonify({
            "success": False,
            "message": "Status and remarks must be text.",
        }), 400
    remarks = raw_remarks.strip()
    # Matches the existing maxlength=2000 on the Admin Job Listing modal.
    if len(remarks) > 2000:
        return jsonify({
            "success": False,
            "message": "Remarks must not exceed 2,000 characters.",
        }), 400

    allowed = [
        "Pending",
        "Approved",
        "Rejected",
        "Flagged",
    ]

    if status not in allowed:
        return jsonify({
            "success": False,
            "message": "Invalid status."
        }), 400

    if status in ["Rejected", "Flagged"] and not remarks:
        return jsonify({
            "success": False,
            "message": "Remarks are required."
        }), 400

    previous_approval_status = job.approval_status

    # Enforce the approval lifecycle.
    # Pending: first review. Flagged: held for further review/correction.
    # Approved and Rejected are terminal decisions for this submission.
    allowed_transitions = {
        "Pending": {"Approved", "Flagged", "Rejected"},
        "Flagged": {"Pending", "Approved", "Rejected"},
        "Approved": set(),
        "Rejected": set(),
    }

    if status == previous_approval_status:
        return jsonify({
            "success": False,
            "message": f"This job is already {status.lower()}.",
        }), 400

    if status not in allowed_transitions.get(previous_approval_status, set()):
        return jsonify({
            "success": False,
            "message": (
                f"A {previous_approval_status.lower()} job cannot be changed "
                f"to {status.lower()}."
            ),
        }), 400

    # Core job action: must succeed independently.
    try:
        job.approval_status = status
        job.remarks = remarks
        job.reviewed_at = datetime.now(timezone.utc)
        job.reviewed_by = current_user.user_id
        record_audit(
            f"JOB_STATUS_CHANGED job_id={job.job_id} from={previous_approval_status} to={status}"
        )

        if status == "Approved" and previous_approval_status != "Approved":
            job.status = "Open"

            if job.job_type == "Job Fair":
                job_fair_entry = (
                    JobFairJob.query
                    .filter_by(job_id=job.job_id)
                    .first()
                )

                if job_fair_entry and job_fair_entry.registration:
                    job_fair = job_fair_entry.registration.job_fair

                    if job_fair and job_fair.end_datetime:
                        job.deadline = job_fair.end_datetime.date()
            else:
                job.deadline = (
                    datetime.now(timezone.utc).date()
                    + timedelta(days=7)
                )

        db.session.commit()

    except Exception as error:
        db.session.rollback()

        current_app.logger.exception(
            "Failed to update job %s to status %s: %s",
            job_id,
            status,
            error,
        )

        return jsonify({
            "success": False,
            "message": (
                "The job status could not be updated. "
                "Check the server terminal for the database error."
            )
        }), 500

    # Notification is secondary. A notification problem must not undo
    # a successful Approve / Reject / Flag action.
    try:
        if job.employer and job.employer.user_id:
            if status == "Flagged":
                notification_title = "Job Posting Needs Correction"
                notification_message = (
                    f"Your job posting '{job.job_title}' was flagged by PESO for "
                    f"further review. Please review the remarks, correct the listing, "
                    f"and resubmit it. Reason: {remarks}"
                )
            elif status == "Rejected":
                notification_title = "Job Posting Rejected"
                notification_message = (
                    f"Your job posting '{job.job_title}' was reviewed and rejected "
                    f"by PESO. This decision is final for this submission. "
                    f"Reason: {remarks}"
                )
            elif status == "Approved":
                notification_title = "Job Posting Approved"
                notification_message = (
                    f"Your job posting '{job.job_title}' was approved by PESO "
                    f"and is now eligible to be shown through the applicable job services."
                )
            else:
                notification_title = "Job Review Update"
                notification_message = (
                    f"Your job posting '{job.job_title}' was returned to pending review."
                )

            notify_user(
                job.employer.user_id,
                notification_title,
                notification_message,
                "Job",
            )
            db.session.commit()

    except Exception as notification_error:
        db.session.rollback()

        current_app.logger.exception(
            "Job %s updated, but notification failed: %s",
            job_id,
            notification_error,
        )

    return jsonify({
        "success": True,
        "status": status,
        "job_status": job.status,
        "job_type": job.job_type,
        "deadline": (
            job.deadline.strftime("%Y-%m-%d")
            if job.deadline
            else None
        ),
    })



# --- QUALIFICATION MATCHING HELPERS ---

def normalize_match_text(value):
    if value is None:
        return ""
    value = str(value).lower().strip()
    value = re.sub(r"[^a-z0-9+#.\s-]", " ", value)
    return re.sub(r"\s+", " ", value)


def contains_phrase(requirement, source):
    requirement = normalize_match_text(requirement)
    source = normalize_match_text(source)
    return bool(requirement and source and requirement in source)


def contains_important_words(requirement, source):
    requirement = normalize_match_text(requirement)
    source = normalize_match_text(source)

    if not requirement or not source:
        return False

    stop_words = {
        "a", "an", "the", "and", "or", "of", "in", "on",
        "with", "for", "to", "is", "are", "be", "must",
        "should", "preferably", "preferred", "required",
        "requirement", "applicant", "candidate",
    }

    words = [
        w for w in requirement.split() if w not in stop_words and len(w) >= 3
    ]

    return bool(words and all(word in source for word in words))


def get_work_experience_years(work_history):
    total_months = 0
    today = datetime.now().date()
    for work in work_history:
        direct = getattr(work, "number_of_months", None)
        if direct is not None:
            try:
                total_months += max(0, int(direct))
                continue
            except (TypeError, ValueError):
                pass
        if not getattr(work, "work_from", None):
            continue
        try:
            start = datetime.strptime(str(work.work_from), "%Y-%m-%d").date()
            end = datetime.strptime(str(work.work_to), "%Y-%m-%d").date() if work.work_to else today
        except (ValueError, TypeError):
            continue
        if end >= start:
            total_months += max(0, (end.year - start.year) * 12 + end.month - start.month + 1)
    return total_months / 12.0

def contains_any_phrase(source, alternatives):
    """Returns True if at least one accepted alternative appears in the applicant data."""
    source = normalize_match_text(source)

    if not source:
        return False

    return any(
        normalize_match_text(option) in source
        for option in alternatives
        if option
    )


def applicant_education_text(seeker):
    return normalize_match_text(
        " ".join([
            seeker.tertiary_course or "",
            seeker.tertiary_level_reached or "",
            seeker.tertiary_year_grad or "",
            seeker.grad_course or "",
            seeker.grad_level_reached or "",
            seeker.senior_high_strand or "",
            seeker.sec_level_reached or "",
        ])
    )


def applicant_skill_text(seeker, tvets):
    parts = [seeker.skills or ""]

    for tvet in tvets:
        parts.extend([
            tvet.training_course or "",
            tvet.skills_acquired or "",
        ])

    return normalize_match_text(" ".join(parts))

def match_job_requirement(requirement, seeker, work_history, tvets, licenses):
    req = normalize_match_text(requirement)
    if not req:
        return False

    # 1. Protected/Personal Characteristics Filter
    sensitive_terms = {
        "male", "female", "man", "woman", "men", "women",
        "single", "married", "widowed", "separated",
        "religion", "religious", "disabled", "disability",
        "pregnant", "pregnancy",
    }
    req_words = set(req.split())

    if req_words.intersection(sensitive_terms):
        return None

    if (
        "years old" in req
        or re.search(r"\bage\s*\d+", req)
        or re.search(r"\b\d{1,2}\s*(?:-|to|–)\s*\d{1,2}\b", req)
    ):
        return None

    education_keywords = {
        "bachelor",
        "degree",
        "college",
        "graduate",
        "undergraduate",
        "tertiary",
        "senior high",
        "high school",
        "course",
        "information technology",
        "computer science",
        "engineering",
        "accountancy",
        "nursing",
        "graphic design",
        "fine arts",
    }

    if any(keyword in req for keyword in education_keywords):
        education_text = applicant_education_text(seeker)

        # ----------------------------------------------------
        # HIGH SCHOOL GRADUATE
        # ----------------------------------------------------
        if "high school graduate" in req or "secondary graduate" in req:
            if seeker.sec_year_grad:
                return True

            sec_level = normalize_match_text(seeker.sec_level_reached)
            return "graduate" in sec_level

        # ----------------------------------------------------
        # COURSE / DEGREE FIELD ALTERNATIVES
        #
        # Applicant only needs ONE course explicitly accepted
        # by the employer.
        # ----------------------------------------------------
        course_groups = {
            "information_technology": [
                "information technology",
                "bachelor of science in information technology",
                "bs information technology",
                "bsit",
            ],
            "computer_science": [
                "computer science",
                "bachelor of science in computer science",
                "bs computer science",
                "bscs",
            ],
            "computer_engineering": [
                "computer engineering",
                "bachelor of science in computer engineering",
                "bscpe",
            ],
            "information_systems": [
                "information systems",
                "bachelor of science in information systems",
                "bsis",
            ],
            "graphic_design": [
                "graphic design",
                "visual communication",
            ],
            "fine_arts": [
                "fine arts",
            ],
            "multimedia": [
                "multimedia arts",
                "digital arts",
                "multimedia",
            ],
            "accounting": [
                "accountancy",
                "accounting",
                "bs accountancy",
            ],
            "nursing": [
                "nursing",
                "bs nursing",
                "bachelor of science in nursing",
            ],
        }

        accepted_course_aliases = []
        for aliases in course_groups.values():
            employer_mentions_this_field = any(
                normalize_match_text(alias) in req for alias in aliases
            )

            if employer_mentions_this_field:
                accepted_course_aliases.extend(aliases)

        # Employer explicitly named one or more accepted courses.
        if accepted_course_aliases:
            course_matches = contains_any_phrase(
                education_text, accepted_course_aliases
            )

            if not course_matches:
                return False

            # ------------------------------------------------
            # Explicit degree-completion requirement
            # ------------------------------------------------
            explicitly_requires_degree = any(
                phrase in req
                for phrase in [
                    "bachelor's degree",
                    "bachelors degree",
                    "bachelor degree",
                    "college graduate",
                ]
            )

            if explicitly_requires_degree:
                # Strongest evidence that the bachelor's degree
                # has actually been completed.
                if seeker.tertiary_year_grad:
                    return True

                tertiary_level = normalize_match_text(
                    seeker.tertiary_level_reached
                )

                if "graduate" in tertiary_level:
                    return True

                # Correct field, but applicant has not established
                # degree completion.
                return False

            return True

        # General education fallback
        return contains_phrase(req, education_text) or contains_important_words(
            req, education_text
        )

    # 3. License & Certification Matching
    license_keywords = {
        "license", "licensed", "eligibility", "eligible",
        "certificate", "certification", "board passer", "civil service",
        "tesda", "nc ii", "nc2", "nc iii", "nc3",
    }

    if any(keyword in req for keyword in license_keywords):
        license_text = []
        for record in licenses:
            license_text.extend([record.eligibility_name or "", record.license_name or ""])
        for tvet in tvets:
            license_text.append(tvet.certificate_received or "")

        full_license_text = " ".join(license_text)
        return contains_phrase(req, full_license_text) or contains_important_words(req, full_license_text)

    # 4. Experience Matching
    experience_terms = {
        "experience", "experienced", "work experience",
        "working experience", "year experience", "years experience",
    }

    if any(term in req for term in experience_terms):
        if not work_history:
            return False

        year_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:year|years|yr|yrs)", req)
        if year_match:
            required_years = float(year_match.group(1))
            if get_work_experience_years(work_history) < required_years:
                return False

        experience_text = " ".join(
            f"{w.position or ''} {w.company or ''} {w.appointment_status or ''}"
            for w in work_history
        )

        generic_patterns = [
            r"^\d+\s*(?:year|years|yr|yrs).*experience$",
            r"^with experience$",
            r"^work experience$",
        ]
        if any(re.search(p, req) for p in generic_patterns):
            return True

        role_req = re.sub(r"\b\d+(?:\.\d+)?\s*(?:year|years|yr|yrs)\b", "", req)
        role_req = re.sub(
            r"\b(?:with|minimum|at least|required|work|working|experience|experienced|in|as|of)\b",
            " ",
            role_req,
        )
        role_req = re.sub(r"\s+", " ", role_req).strip()

        if not role_req:
            return True

        return contains_phrase(role_req, experience_text) or contains_important_words(role_req, experience_text)
    
    
    # ========================================================
    # 5. PROGRAMMING LANGUAGE MATCHING
    # ========================================================

    programming_languages = {
        "python": ["python"],
        "javascript": ["javascript", "java script", "js"],
        "php": ["php"],
        "java": ["java"],
        "typescript": ["typescript"],
        "csharp": ["c#", "c sharp"],
        "cplusplus": ["c++"],
        "ruby": ["ruby"],
        "go": ["golang", "go programming"],
    }

    mentioned_languages = []
    for aliases in programming_languages.values():
        if any(normalize_match_text(alias) in req for alias in aliases):
            mentioned_languages.extend(aliases)

    if mentioned_languages:
        skill_text = applicant_skill_text(seeker, tvets)
        return contains_any_phrase(skill_text, mentioned_languages)


    # 6. Skills & TVET Matching
    skill_parts = [seeker.skills or ""]
    for tvet in tvets:
        skill_parts.extend([tvet.training_course or "", tvet.skills_acquired or ""])
    skill_text = " ".join(skill_parts)

    skill_text = applicant_skill_text(
        seeker,
        tvets
    )

    if (
        contains_phrase(req, skill_text)
        or contains_important_words(
            req,
            skill_text
        )
    ):
        return True

    # 6. Occupational/Career Preference Matching
    occupation_text = " ".join([
        seeker.pref_occupation_1 or "",
        seeker.pref_occupation_2 or "",
        seeker.pref_occupation_3 or "",
    ])
    if contains_phrase(req, occupation_text) or contains_important_words(req, occupation_text):
        return True

    # 7. Work History Title Fallback
    work_text = " ".join(f"{w.position or ''} {w.company or ''}" for w in work_history)
    return contains_phrase(req, work_text) or contains_important_words(req, work_text)


# --- JOB RANKINGS & APPLICANT SCORING ---

@admin.route("/job/<int:job_id>/rankings")
@login_required
def job_rankings(job_id):
    if not check_admin_access():
        return render_template(
            "errors/404.html"
        ), 404

    job = Job.query.get_or_404(
        job_id
    )

    search_query = request.args.get(
        "search",
        ""
    ).strip()

    # ========================================================
    # LOAD APPLICATIONS
    # ========================================================

    app_query = (
        Application.query
        .join(
            JobSeeker,
            Application.job_seeker_id
            == JobSeeker.job_seeker_id
        )
        .filter(
            Application.job_id
            == job.job_id
        )
    )

    if search_query:
        search = (
            f"%{search_query}%"
        )

        app_query = app_query.filter(
            db.or_(
                JobSeeker.first_name.ilike(
                    search
                ),
                JobSeeker.last_name.ilike(
                    search
                ),
                JobSeeker.skills.ilike(
                    search
                ),
                JobSeeker.experience.ilike(
                    search
                ),
                JobSeeker.pref_occupation_1.ilike(
                    search
                ),
                JobSeeker.pref_occupation_2.ilike(
                    search
                ),
                JobSeeker.pref_occupation_3.ilike(
                    search
                ),
            )
        )

    applications = (
        app_query.all()
    )

    # ========================================================
    # SHARED STRUCTURED MATCHING + RANKING
    # ========================================================

    ranking_result = (
        rank_job_applications(
            job,
            applications,
        )
    )

    ranked_applications = (
        ranking_result["ranked"]
    )

    unranked_applications = (
        ranking_result["unranked"]
    )

    return render_template(
        "admin/admin_job_rankings.html",
        job=job,
        ranked_applications=(
            ranked_applications
        ),
        unranked_applications=(
            unranked_applications
        ),
        search=search_query,
        today=datetime.now().date(),
    )


@admin.route("/jobseeker/<string:public_id>/resume")
@login_required
def view_jobseeker_resume(public_id):
    if not check_admin_access():
        abort(403)

    seeker = (JobSeeker.query.join(User).filter(User.public_id == public_id).first_or_404())

    if not seeker.resume:
        abort(404)

    # Resume uploads are stored privately, not under /static/uploads.
    # Use the same storage location as jobseeker and employer resume routes.
    from website.utils.private_resumes import private_resume_directory

    stored_name = str(seeker.resume)
    if (not stored_name or stored_name in {".", ".."}
            or os.path.basename(stored_name) != stored_name
            or "\\" in stored_name):
        abort(404)

    upload_folder = private_resume_directory()
    if not os.path.isfile(os.path.join(upload_folder, stored_name)):
        abort(404)

    return send_from_directory(
        directory=upload_folder,
        path=stored_name,
        as_attachment=False,
        download_name=getattr(seeker, "resume_filename", None) or stored_name,
    )