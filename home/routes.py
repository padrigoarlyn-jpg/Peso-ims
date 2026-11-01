from flask import Blueprint, render_template, jsonify
from website.models import Job, JobFair
from datetime import datetime, date, timezone


home = Blueprint("home", __name__)


# ============================================================
# HOME PAGE
# ============================================================

@home.route("/")
def index():
    return render_template("home/homepage.html")


# ============================================================
# JOB LISTING PAGE
# ============================================================

@home.route("/joblist")
def joblist():

    # --------------------------------------------------------
    # Only display jobs that are:
    #
    # 1. Open
    # 2. Approved
    # 3. Have a deadline that has not passed
    #
    # job_type is NOT used as a visibility restriction.
    # It is only used to distinguish Referral vs Job Fair.
    # --------------------------------------------------------

    jobs = (
        Job.query
        .filter(
            Job.status == "Open",
            Job.approval_status == "Approved",
            Job.deadline >= date.today()
        )
        .order_by(Job.created_at.desc())
        .all()
    )

    return render_template(
        "home/job_listing.html",
        jobs=jobs,
        current_date=datetime.utcnow()
    )


# ============================================================
# ABOUT PAGE
# ============================================================



# ============================================================
# PUBLIC ANNOUNCEMENTS / JOB FAIR PAGE
# ============================================================

@home.route("/announcements")
def announcements():
    """
    Public, read-only Job Fair announcements.

    Visibility intentionally mirrors the working Job Seeker
    announcement page so both pages show the same upcoming /
    ongoing events.
    """

    now = datetime.now(timezone.utc)

    def _to_aware(value):
        if value is None:
            return None

        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)

        return value

    db_fairs = (
        JobFair.query
        .order_by(JobFair.start_datetime.asc())
        .all()
    )

    public_announcements = []

    for fair in db_fairs:
        start_dt = _to_aware(fair.start_datetime)
        end_dt = _to_aware(fair.end_datetime)
        deadline_dt = _to_aware(fair.registration_deadline)

        # Same visibility rule used by Job Seeker announcements:
        # keep the event if its start is still ahead OR its end
        # has not yet passed.
        is_visible = (
            (start_dt is not None and start_dt >= now)
            or
            (end_dt is not None and end_dt >= now)
        )

        if not is_visible:
            continue

        if (
            start_dt is not None
            and start_dt <= now
            and (
                end_dt is None
                or end_dt >= now
            )
        ):
            display_status = "Ongoing"
        else:
            display_status = "Upcoming"

        public_announcements.append({
            "job_fair_id": fair.job_fair_id,
            "title": fair.title,
            "description": fair.description or "",
            "event_type": fair.event_type or "Job Fair",
            "start_datetime": fair.start_datetime,
            "end_datetime": fair.end_datetime,
            "registration_deadline": fair.registration_deadline,
            "venue": fair.venue or "TBA",
            "poster_image": fair.poster_image,
            "status": display_status,
            "registration_open": (
                deadline_dt is None
                or deadline_dt >= now
            ),
        })

    return render_template(
        "home/announcements.html",
        announcements=public_announcements,
    )


@home.route("/about")
def about():
    return render_template("home/about.html")


# ============================================================
# JOB DETAILS API
# ============================================================

@home.route("/job/<int:job_id>")
def get_job(job_id):

    job = Job.query.get_or_404(job_id)

    return jsonify({

        # --------------------------------------------------
        # BASIC JOB INFORMATION
        # --------------------------------------------------

        "job_id":
            job.job_id,

        "job_title":
            job.job_title or "",

        "category":
            job.category or "Other",

        "employment_type":
            job.employment_type or "Not specified",

        # --------------------------------------------------
        # JOB TYPE
        #
        # This is now the source of truth for:
        # Referral vs Job Fair Exclusive
        # --------------------------------------------------

        "job_type":
            job.job_type or "Referral",

        # --------------------------------------------------
        # STATUS
        #
        # This is only the actual job status:
        # Pending / Open / Closed
        # --------------------------------------------------

        "status":
            job.status or "",

        "approval_status":
            job.approval_status or "",

        # --------------------------------------------------
        # COMPANY
        # --------------------------------------------------

        "company_name":
            job.employer.company_name
            if job.employer
            else "Direct Hire",

        "company_logo":
            f"/static/uploads/company_logos/{job.employer.company_logo}"
            if job.employer and job.employer.company_logo
            else None,

        "company_description":
            job.employer.company_description
            if job.employer
            else None,

        "company_website":
            job.employer.website
            if job.employer
            else None,

        # --------------------------------------------------
        # LOCATION
        # --------------------------------------------------

        "location":
            job.location or "Not specified",

        "google_map_url":
            job.employer.google_map_url
            if job.employer
            else "",

        # --------------------------------------------------
        # SALARY
        # --------------------------------------------------

        "salary":
            (
                f"₱{job.salary_min:,.0f} - ₱{job.salary_max:,.0f}"
                if job.salary_min is not None
                and job.salary_max is not None
                else (
                    f"₱{job.salary_min:,.0f}+"
                    if job.salary_min is not None
                    else (
                        f"Up to ₱{job.salary_max:,.0f}"
                        if job.salary_max is not None
                        else "Salary Negotiable"
                    )
                )
            ),

        "salary_min":
            float(job.salary_min)
            if job.salary_min is not None
            else None,

        "salary_max":
            float(job.salary_max)
            if job.salary_max is not None
            else None,

        # --------------------------------------------------
        # VACANCIES
        # --------------------------------------------------

        "vacancies":
            job.vacancies
            if job.vacancies is not None
            else 0,

        # --------------------------------------------------
        # DEADLINE
        # --------------------------------------------------

        "deadline":
            job.deadline.strftime("%B %d, %Y")
            if job.deadline
            else "Not specified",

        # --------------------------------------------------
        # POSTED DATE
        # --------------------------------------------------

        "created_at":
            job.created_at.strftime("%B %d, %Y")
            if job.created_at
            else "",

        # --------------------------------------------------
        # DESCRIPTION
        # --------------------------------------------------

        "description":
            job.description or "No description available.",

        # --------------------------------------------------
        # RESPONSIBILITIES
        # --------------------------------------------------

        "responsibilities":
            job.responsibilities.split("\n")
            if job.responsibilities
            else [],

        # --------------------------------------------------
        # QUALIFICATIONS
        # --------------------------------------------------

        "qualifications":
            job.qualifications.split("\n")
            if job.qualifications
            else []
    })