from sqlalchemy.exc import IntegrityError
from datetime import datetime, timezone, date

from flask import (
    render_template,
    redirect,
    url_for,
    flash,
    request,
    current_app,
    send_from_directory,
    jsonify
)

from flask_login import login_required, current_user

import os
import uuid

from website.extensions import db

from website.models import (
    JobFairEmployerRegistration,
    JobSeeker,
    Job,
    Application,
    Announcement,
    Referral,
    Notification,
    JobSeekerLanguage,
    JobFairRegistration,
    JobFairJob,
    JobFairJobPreselection,
    JobSeekerLicense,
    JobSeekerTVET,
    JobSeekerWorkHistory,
    JobFair
)

from website.utils.notifications import notify_admins, notify_user
from website.utils.qrcode_gen import generate_qr_code
from website.utils.job_fair_lifecycle import is_queue_entry_open

from werkzeug.utils import secure_filename

from .forms import (
    NSRPRegistrationForm,
    ProfilePictureForm,
    ProfessionalSummaryForm,
    PersonalInformationForm,
    JobPreferencesForm,
    WorkExperienceForm,
    ResumeUploadForm,
    EducationForm,
    SkillsForm
)

from . import jobseeker


# ============================================================
# FILE EXTENSIONS
# ============================================================

ALLOWED_IMAGE_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "webp"
}

ALLOWED_RESUME_EXTENSIONS = {
    "pdf",
    "doc",
    "docx"
}


# =========================================================
# JOB LIST
# =========================================================

@jobseeker.route("/joblist")
@login_required
def joblist():

    # --------------------------------------------------------
    # SECURITY
    # --------------------------------------------------------

    if current_user.role != "jobseeker":
        return render_template("errors/404.html"), 404


    # --------------------------------------------------------
    # FILTERS
    # --------------------------------------------------------

    search_query = request.args.get(
        "search",
        "",
        type=str
    ).strip()

    category_filter = request.args.get(
        "category",
        "",
        type=str
    ).strip()

    employment_type_filter = request.args.get(
        "employment_type",
        "",
        type=str
    ).strip()

    salary_filter = request.args.get(
        "salary",
        "",
        type=str
    ).strip()

    source_filter = request.args.get(
        "source",
        "",
        type=str
    ).strip()


    # ========================================================
    # GET JOBSEEKER
    # ========================================================

    jobseeker_obj = (
        JobSeeker.query
        .filter_by(
            user_id=current_user.user_id
        )
        .first()
    )


    # ========================================================
    # JOB FAIR ASSOCIATION MAP
    #
    # IMPORTANT:
    #
    # JobFairJob association is NOT the same thing as
    # Job.job_type.
    #
    # A Referral job can be included in a Job Fair.
    #
    # Therefore:
    #
    # job_type:
    #   Referral
    #   Job Fair
    #
    # JobFairJob:
    #   Determines whether that job participates in a
    #   particular Job Fair.
    # ========================================================

    job_fair_job_map = {}

    job_fair_job_details = {}


    all_job_fair_jobs = (
        JobFairJob.query
        .all()
    )


    for fair_job in all_job_fair_jobs:

        if fair_job.job_id is None:
            continue


        # ----------------------------------------------------
        # BASIC JOB -> JOB FAIR JOB ID MAP
        # ----------------------------------------------------

        if fair_job.job_id not in job_fair_job_map:

            job_fair_job_map[
                fair_job.job_id
            ] = []


        job_fair_job_map[
            fair_job.job_id
        ].append(
            fair_job.job_fair_job_id
        )


        # ----------------------------------------------------
        # GET JOB FAIR
        # ----------------------------------------------------

        job_fair = None

        employer_registration = (
            JobFairEmployerRegistration.query
            .filter_by(
                registration_id=
                    fair_job.registration_id
            )
            .first()
        )


        if employer_registration:

            job_fair = (
                JobFair.query
                .filter_by(
                    job_fair_id=
                        employer_registration.job_fair_id
                )
                .first()
            )


        if fair_job.job_id not in job_fair_job_details:

            job_fair_job_details[
                fair_job.job_id
            ] = []


        job_fair_job_details[
            fair_job.job_id
        ].append({

            "job_fair_job_id":
                fair_job.job_fair_job_id,

            "job_fair_id":
                (
                    job_fair.job_fair_id
                    if job_fair
                    else None
                ),

            "job_fair_title":
                (
                    job_fair.title
                    if job_fair
                    else "Job Fair"
                ),

            "job_fair_status":
                (
                    job_fair.status
                    if job_fair
                    else None
                )

        })


    # ========================================================
    # JOB FAIR REGISTRATION / PRESELECTION DATA
    # ========================================================

    registered_job_fair_ids = set()

    preselected_job_fair_job_ids = set()

    preselected_job_ids = set()


    if jobseeker_obj:

        registrations = (
            JobFairRegistration.query
            .filter_by(
                job_seeker_id=
                    jobseeker_obj.job_seeker_id
            )
            .all()
        )


        registered_job_fair_ids = {

            registration.job_fair_id

            for registration in registrations

            if registration.job_fair_id is not None

        }


        if registrations:

            registration_ids = [

                registration.registration_id

                for registration in registrations

            ]


            existing_preselections = (
                JobFairJobPreselection.query
                .filter(
                    JobFairJobPreselection
                    .job_fair_registration_id
                    .in_(registration_ids)
                )
                .all()
            )


            preselected_job_fair_job_ids = {

                preselection.job_fair_job_id

                for preselection
                in existing_preselections

            }


            if preselected_job_fair_job_ids:

                preselected_fair_jobs = (
                    JobFairJob.query
                    .filter(
                        JobFairJob.job_fair_job_id.in_(
                            preselected_job_fair_job_ids
                        )
                    )
                    .all()
                )


                preselected_job_ids = {

                    fair_job.job_id

                    for fair_job
                    in preselected_fair_jobs

                    if fair_job.job_id is not None

                }


    # ========================================================
    # ADD REGISTRATION STATE TO JOB FAIR ASSOCIATIONS
    #
    # This lets the frontend determine:
    #
    # Registered:
    #     Show Preselect
    #
    # Not registered:
    #     Show Pre-Register First
    # ========================================================

    for job_id, fair_entries in job_fair_job_details.items():

        for fair_entry in fair_entries:

            fair_id = fair_entry.get(
                "job_fair_id"
            )


            fair_entry["registered"] = (
                fair_id in registered_job_fair_ids
                if fair_id is not None
                else False
            )


    # ========================================================
    # BASE JOB QUERY
    #
    # NEW MODEL:
    #
    # status:
    #     Pending
    #     Open
    #     Closed
    #
    # job_type:
    #     Referral
    #     Job Fair
    #
    # A job is publicly displayed only when:
    #
    #     status == Open
    #     approval_status == Approved
    #     deadline is not expired
    # ========================================================

    query = Job.query.filter(

        Job.status == "Open",

        Job.approval_status == "Approved",

        db.or_(
            Job.deadline.is_(None),
            Job.deadline >= date.today()
        )

    )


    # ========================================================
    # SEARCH
    # ========================================================

    if search_query:

        search_pattern = (
            f"%{search_query}%"
        )


        query = query.filter(

            db.or_(

                Job.job_title.ilike(
                    search_pattern
                ),

                Job.category.ilike(
                    search_pattern
                ),

                Job.description.ilike(
                    search_pattern
                ),

                Job.location.ilike(
                    search_pattern
                )

            )

        )


    # ========================================================
    # CATEGORY
    # ========================================================

    if category_filter:

        query = query.filter(

            Job.category ==
            category_filter

        )


    # ========================================================
    # EMPLOYMENT TYPE
    # ========================================================

    if employment_type_filter:

        query = query.filter(

            Job.employment_type ==
            employment_type_filter

        )


    # ========================================================
    # SALARY
    # ========================================================

    if salary_filter == "500-1000":

        query = query.filter(

            Job.salary_min <= 1000,

            Job.salary_max >= 500

        )

    elif salary_filter == "1001-2000":

        query = query.filter(

            Job.salary_min <= 2000,

            Job.salary_max >= 1001

        )

    elif salary_filter == "2001-3000":

        query = query.filter(

            Job.salary_min <= 3000,

            Job.salary_max >= 2001

        )

    elif salary_filter == "3001+":

        query = query.filter(

            db.or_(

                Job.salary_max >= 3001,

                Job.salary_min >= 3001

            )

        )
        
    # ========================================================
    # JOB TYPE FILTER
    # ========================================================

    if source_filter == "regular":

        query = query.filter(
            Job.job_type == "Referral"
        )


    elif source_filter == "job-fair":

        query = (
            query

            # Job must actually be a Job Fair-exclusive job
            .filter(
                Job.job_type == "Job Fair"
            )

            # Job must be linked to a Job Fair entry
            .join(
                JobFairJob,
                JobFairJob.job_id == Job.job_id
            )

            # Job Fair job must belong to an employer registration
            .join(
                JobFairEmployerRegistration,
                JobFairEmployerRegistration.registration_id
                == JobFairJob.registration_id
            )

            # Employer must be approved for the Job Fair
            .filter(
                JobFairEmployerRegistration.status == "Approved"
            )

            # Connect to the actual Job Fair
            .join(
                JobFair,
                JobFair.job_fair_id
                == JobFairEmployerRegistration.job_fair_id
            )

            # Only Upcoming or Ongoing Job Fairs are visible
            .filter(
                JobFair.status.in_([
                    "Upcoming",
                    "Ongoing"
                ])
            )
        )


    # ========================================================
    # FETCH JOBS
    # ========================================================
    jobs = (

        query

        .distinct()

        .order_by(
            Job.created_at.desc()
        )

        .all()

    )


    # ========================================================
    # APPLICATION DATA
    # ========================================================

    applied_job_ids = set()

    referred_job_ids = set()


    if jobseeker_obj:

        applications = (

            Application.query

            .filter_by(
                job_seeker_id=
                    jobseeker_obj.job_seeker_id
            )

            .all()

        )


        applied_job_ids = { application.job_id
            for application in applications
            if application.job_id is not None
        }


       # ----------------------------------------------------
        # REFERRAL REQUESTS
        #
        # An existing Referral record means the jobseeker
        # has already submitted a referral request for the
        # associated job.
        #
        # Referral.status may later become:
        #
        # Pending  -> Referral (Admin)
        # Referred -> Referred
        #
        # For the Jobseeker Job List, either state means
        # another referral request must NOT be allowed.
        # ----------------------------------------------------

        for application in applications:

            if application.job_id is None:
                continue

            existing_referral = Referral.query.filter_by(
                application_id=application.application_id
            ).first()

            if existing_referral:

                referred_job_ids.add(
                    application.job_id
                )


    # ========================================================
    # CURRENT DATE
    # ========================================================

    current_date = datetime.now(
        timezone.utc
    )


    # ========================================================
    # RENDER
    # ========================================================

    return render_template(

        "jobseeker/jobseeker_joblist.html",

        jobs=jobs,

        jobseeker=jobseeker_obj,

        current_date=current_date,

        search_query=search_query,

        category_filter=category_filter,

        employment_type_filter=
            employment_type_filter,

        salary_filter=
            salary_filter,

        source_filter=
            source_filter,

        referred_job_ids=
            list(referred_job_ids),

        registered_job_fair_ids=
            list(registered_job_fair_ids),

        preselected_job_fair_job_ids=
            list(preselected_job_fair_job_ids),

        preselected_job_ids=
            list(preselected_job_ids),

        applied_job_ids=
            list(applied_job_ids),

        job_fair_job_map=
            job_fair_job_map,

        job_fair_job_details=
            job_fair_job_details

    )


# =========================================================
# PRESELECT JOB FAIR JOB
# =========================================================

@jobseeker.route(
    "/jobfair/preselect",
    methods=["POST"]
)
@login_required
def preselect_job_fair_job():

    # --------------------------------------------------------
    # SECURITY
    # --------------------------------------------------------

    if current_user.role != "jobseeker":

        return jsonify({

            "success": False,

            "message":
                "Unauthorized."

        }), 403


    # --------------------------------------------------------
    # GET JOB FAIR JOB ID
    # --------------------------------------------------------

    job_fair_job_id = request.form.get(
        "job_fair_job_id",
        type=int
    )


    if not job_fair_job_id:

        return jsonify({

            "success": False,

            "message":
                "Invalid job fair job."

        }), 400


    # --------------------------------------------------------
    # GET JOBSEEKER
    # --------------------------------------------------------

    jobseeker_obj = (

        JobSeeker.query

        .filter_by(
            user_id=current_user.user_id
        )

        .first()

    )


    if not jobseeker_obj:

        return jsonify({

            "success": False,

            "message":
                "Jobseeker profile not found."

        }), 404


    # --------------------------------------------------------
    # GET JOB FAIR JOB
    # --------------------------------------------------------

    job_fair_job = (

        JobFairJob.query.get(
            job_fair_job_id
        )

    )


    if not job_fair_job:

        return jsonify({

            "success": False,

            "message":
                "Job fair job not found."

        }), 404


    # --------------------------------------------------------
    # GET ACTUAL JOB
    # --------------------------------------------------------

    job = (

        Job.query.get(
            job_fair_job.job_id
        )

    )


    if not job:

        return jsonify({

            "success": False,

            "message": (
                "Job associated with this Job Fair "
                "was not found."
            )

        }), 404


    # --------------------------------------------------------
    # VERIFY JOB
    # --------------------------------------------------------

    if job.status != "Open":

        return jsonify({

            "success": False,

            "message":
                "This job is no longer open."

        }), 400


    if job.approval_status != "Approved":

        return jsonify({

            "success": False,

            "message":
                "This job is not approved."

        }), 400


    if (
        job.deadline
        and job.deadline < date.today()
    ):

        return jsonify({

            "success": False,

            "message":
                "This job listing has expired."

        }), 400


    # --------------------------------------------------------
    # GET EMPLOYER REGISTRATION
    # --------------------------------------------------------

    employer_registration = (

        JobFairEmployerRegistration.query

        .filter_by(

            registration_id=
                job_fair_job.registration_id

        )

        .first()

    )


    if not employer_registration:

        return jsonify({

            "success": False,

            "message":
                "Invalid job fair registration."

        }), 404


    # --------------------------------------------------------
    # VERIFY EMPLOYER REGISTRATION
    # --------------------------------------------------------

    if employer_registration.status != "Approved":

        return jsonify({

            "success": False,

            "message": (
                "This employer has not been approved "
                "for the Job Fair."
            )

        }), 400


    # --------------------------------------------------------
    # GET JOB FAIR
    # --------------------------------------------------------

    job_fair = (

        JobFair.query.get(

            employer_registration.job_fair_id

        )

    )


    if not job_fair:

        return jsonify({

            "success": False,

            "message":
                "Job fair not found."

        }), 404


    # --------------------------------------------------------
    # VERIFY JOB FAIR STATUS
    # --------------------------------------------------------

    if job_fair.status not in (

        "Upcoming",
        "Ongoing"

    ):

        return jsonify({

            "success": False,

            "message": (
                "Jobs can only be preselected before "
                "or during the Job Fair."
            )

        }), 400


    # --------------------------------------------------------
    # VERIFY JOBSEEKER REGISTRATION
    # --------------------------------------------------------

    job_fair_registration = (

        JobFairRegistration.query

        .filter_by(

            job_fair_id=
                job_fair.job_fair_id,

            job_seeker_id=
                jobseeker_obj.job_seeker_id

        )

        .first()

    )


    # --------------------------------------------------------
    # IMPORTANT:
    #
    # DO NOT allow direct preselection when the user has
    # not registered.
    #
    # The frontend normally catches this first and displays
    # "Pre-Register First", but the backend must enforce it
    # too.
    # --------------------------------------------------------

    if not job_fair_registration:

        return jsonify({

            "success": False,

            "registration_required":
                True,

            "job_fair_id":
                job_fair.job_fair_id,

            "message": (
                "You must register for this "
                "Job Fair before preselecting a job."
            )

        }), 403


    # --------------------------------------------------------
    # PHASE 1D: QUEUE ENTRY / JOB SELECTION CUTOFF
    # --------------------------------------------------------

    if not is_queue_entry_open(job_fair):
        return jsonify({
            "success": False,
            "message": "Queue entry for this Job Fair has closed. Existing queues may continue processing."
        }), 403

    # --------------------------------------------------------
    # VERIFY ATTENDANCE
    # --------------------------------------------------------

    if (

        job_fair_registration.attendance_status
        == "Absent"

    ):

        return jsonify({

            "success": False,

            "message": (
                "Your Job Fair registration "
                "is no longer valid."
            )

        }), 403


    # --------------------------------------------------------
    # CHECK EXISTING APPLICATION
    # --------------------------------------------------------

    existing_application = (

        Application.query

        .filter_by(

            job_seeker_id=
                jobseeker_obj.job_seeker_id,

            job_id=
                job.job_id

        )

        .first()

    )


    if existing_application:

        return jsonify({

            "success": False,

            "message": (
                "You have already applied for this job "
                "and cannot preselect it."
            )

        }), 400


    # --------------------------------------------------------
    # CHECK EXISTING PRESELECTION
    # --------------------------------------------------------

    existing = (

        JobFairJobPreselection.query

        .filter_by(

            job_fair_registration_id=
                job_fair_registration.registration_id,

            job_fair_job_id=
                job_fair_job.job_fair_job_id

        )

        .first()

    )


    if existing:

        return jsonify({

            "success": True,

            "already_preselected":
                True,

            "message":
                "Job is already preselected.",

            "preselection_id":
                existing.preselection_id,

            "job_fair_job_id":
                job_fair_job.job_fair_job_id

        }), 200


    # --------------------------------------------------------
    # CREATE PRESELECTION
    # --------------------------------------------------------

    preselection = JobFairJobPreselection(

        job_fair_registration_id=
            job_fair_registration.registration_id,

        job_fair_job_id=
            job_fair_job.job_fair_job_id,

        preselected_at=
            datetime.now(timezone.utc)

    )


    db.session.add(
        preselection
    )


    try:

        db.session.commit()

    except IntegrityError:

        # Another request may have inserted this same preselection between
        # the initial lookup and commit. The database unique constraint is
        # authoritative; return the existing selection as an idempotent result.
        db.session.rollback()
        concurrent_selection = JobFairJobPreselection.query.filter_by(
            job_fair_registration_id=job_fair_registration.registration_id,
            job_fair_job_id=job_fair_job.job_fair_job_id,
        ).first()
        if concurrent_selection:
            return jsonify({
                "success": True,
                "already_preselected": True,
                "message": "Job is already preselected.",
                "preselection_id": concurrent_selection.preselection_id,
                "job_fair_job_id": job_fair_job.job_fair_job_id,
            }), 200
        current_app.logger.exception(
            "Job fair preselection integrity failure"
        )
        return jsonify({
            "success": False,
            "message": "Unable to preselect this job.",
        }), 409

    except Exception:

        db.session.rollback()

        current_app.logger.exception(
            "Failed to create job fair preselection"
        )

        return jsonify({

            "success": False,

            "message":
                "Unable to preselect this job."

        }), 500


    return jsonify({

        "success": True,

        "already_preselected":
            False,

        "message":
            "Job successfully preselected.",

        "preselection_id":
            preselection.preselection_id,

        "job_fair_job_id":
            job_fair_job.job_fair_job_id

    }), 200


# =========================================================
# REMOVE JOB FAIR PRESELECTION
# =========================================================

@jobseeker.route(
    "/jobfair/preselect/remove",
    methods=["POST"]
)
@login_required
def remove_job_fair_preselection():

    if current_user.role != "jobseeker":

        return jsonify({

            "success": False,

            "message":
                "Unauthorized."

        }), 403


    job_fair_job_id = request.form.get(
        "job_fair_job_id",
        type=int
    )


    if not job_fair_job_id:

        return jsonify({

            "success": False,

            "message":
                "Invalid job fair job."

        }), 400


    jobseeker_obj = (

        JobSeeker.query

        .filter_by(
            user_id=current_user.user_id
        )

        .first()

    )


    if not jobseeker_obj:

        return jsonify({

            "success": False,

            "message":
                "Jobseeker profile not found."

        }), 404


    job_fair_job = (

        JobFairJob.query.get(
            job_fair_job_id
        )

    )


    if not job_fair_job:

        return jsonify({

            "success": False,

            "message":
                "Job fair job not found."

        }), 404


    employer_registration = (

        JobFairEmployerRegistration.query

        .filter_by(

            registration_id=
                job_fair_job.registration_id

        )

        .first()

    )


    if not employer_registration:

        return jsonify({

            "success": False,

            "message":
                "Invalid job fair registration."

        }), 404


    job_fair_registration = (

        JobFairRegistration.query

        .filter_by(

            job_fair_id=
                employer_registration.job_fair_id,

            job_seeker_id=
                jobseeker_obj.job_seeker_id

        )

        .first()

    )


    if not job_fair_registration:

        return jsonify({

            "success": False,

            "message":
                "Job Fair registration not found."

        }), 403


    job_fair = JobFair.query.get(employer_registration.job_fair_id)
    if not job_fair or not is_queue_entry_open(job_fair):
        return jsonify({
            "success": False,
            "message": "Queue entry for this Job Fair has closed. Existing job selections can no longer be changed."
        }), 403


    preselection = (

        JobFairJobPreselection.query

        .filter_by(

            job_fair_registration_id=
                job_fair_registration.registration_id,

            job_fair_job_id=
                job_fair_job.job_fair_job_id

        )

        .first()

    )


    if not preselection:

        return jsonify({

            "success": True,

            "message":
                "Job was not preselected."

        }), 200


    db.session.delete(
        preselection
    )


    try:

        db.session.commit()

    except Exception:

        db.session.rollback()

        current_app.logger.exception(
            "Failed to remove job fair preselection"
        )

        return jsonify({

            "success": False,

            "message":
                "Unable to remove preselection."

        }), 500


    return jsonify({

        "success": True,

        "message":
            "Job preselection removed."

    }), 200


# =========================================================
# GET JOB DETAILS
# =========================================================

@jobseeker.route(
    "/job/<int:job_id>"
)
@login_required
def get_job_details(job_id):

    # --------------------------------------------------------
    # SECURITY
    # --------------------------------------------------------

    if current_user.role != "jobseeker":

        return jsonify({

            "success": False,

            "message":
                "Unauthorized."

        }), 403


    # --------------------------------------------------------
    # GET JOB
    #
    # status:
    #     availability
    #
    # job_type:
    #     Referral / Job Fair
    # --------------------------------------------------------

    job = (

        Job.query

        .filter(

            Job.job_id == job_id,

            Job.status == "Open",

            Job.approval_status == "Approved"

        )

        .first()

    )


    if not job:

        return jsonify({

            "success": False,

            "message":
                "Job not found."

        }), 404


    # --------------------------------------------------------
    # CHECK DEADLINE
    # --------------------------------------------------------

    if (

        job.deadline
        and job.deadline < date.today()

    ):

        return jsonify({

            "success": False,

            "message":
                "This job listing has expired."

        }), 404


    # --------------------------------------------------------
    # EMPLOYER
    # --------------------------------------------------------

    employer = job.employer


    # --------------------------------------------------------
    # VACANCY AVAILABILITY
    # --------------------------------------------------------

    accepted_count = (
        Application.query
        .filter_by(job_id=job.job_id, status="Accepted")
        .count()
    )

    total_vacancies = int(job.vacancies or 0)
    remaining_vacancies = max(total_vacancies - accepted_count, 0)

    # --------------------------------------------------------
    # STRUCTURED REQUIREMENTS
    # --------------------------------------------------------

    structured_requirements = [
        {
            "code": qualification.qualification_code,
            "importance": qualification.importance,
        }
        for qualification in (job.structured_qualifications or [])
    ]

    education_labels = {
        "NONE": "No Minimum Education",
        "ELEMENTARY": "Elementary Level",
        "ELEMENTARY_GRAD": "Elementary Graduate",
        "JHS": "Junior High School",
        "JUNIOR_HIGH": "Junior High School",
        "SHS": "Senior High School",
        "SENIOR_HIGH": "Senior High School",
        "TVET": "TESDA / TVET",
        "VOCATIONAL": "Vocational / Technical",
        "COLLEGE_LEVEL": "College Level",
        "BACHELOR": "Bachelor's Degree",
        "BACHELORS": "Bachelor's Degree",
        "MASTER": "Master's Degree",
        "DOCTORATE": "Doctorate Degree",
    }

    # --------------------------------------------------------
    # JOBSEEKER REFERRAL STATE FOR THIS JOB
    # --------------------------------------------------------

    referral_status = None

    jobseeker_obj = (
        JobSeeker.query
        .filter_by(user_id=current_user.user_id)
        .first()
    )

    if jobseeker_obj:
        application = (
            Application.query
            .filter_by(
                job_id=job.job_id,
                job_seeker_id=jobseeker_obj.job_seeker_id
            )
            .first()
        )

        if application:
            referral = (
                Referral.query
                .filter_by(application_id=application.application_id)
                .first()
            )

            if referral:
                referral_status = referral.status


    # --------------------------------------------------------
    # SUCCESS
    # --------------------------------------------------------

    return jsonify({

        "success": True,

        "job_id":
            job.job_id,

        "job_title":
            job.job_title,

        "category":
            job.category,

        "employment_type":
            job.employment_type,

        "minimum_education":
            job.minimum_education,

        "minimum_education_label":
            education_labels.get(
                job.minimum_education,
                (job.minimum_education or "Not specified")
                .replace("_", " ")
                .title()
            ),

        "minimum_experience_months":
            job.minimum_experience_months or 0,

        "structured_requirements":
            structured_requirements,

        "job_type":
            job.job_type,

        "referral_status":
            referral_status,

        "description":
            job.description,

        "location":
            job.location,

        "salary_min":
            job.salary_min,

        "salary_max":
            job.salary_max,

        "salary": (

            f"₱{job.salary_min:,.0f} - "
            f"₱{job.salary_max:,.0f}"

            if (

                job.salary_min is not None
                and
                job.salary_max is not None

            )

            else "Salary Negotiable"

        ),

        "vacancies":
            remaining_vacancies,

        "total_vacancies":
            total_vacancies,

        "deadline": (

            job.deadline.strftime(
                "%Y-%m-%d"
            )

            if job.deadline

            else None

        ),

        "created_at": (

            job.created_at.strftime(
                "%Y-%m-%d %H:%M"
            )

            if job.created_at

            else None

        ),

        "status":
            job.status,

        "responsibilities":
            job.responsibilities,

        "qualifications":
            job.qualifications,

        "google_map_url":
            getattr(
                job,
                "google_map_url",
                None
            ),

        "company_name": (

            employer.company_name

            if employer

            else "Direct Hire"

        ),

        "industry": (

            employer.company_industry

            if employer

            else None

        ),

        "company_description": (

            getattr(
                employer,
                "company_description",
                None
            )

            if employer

            else None

        ),

        "company_logo": (

            employer.company_logo

            if employer

            else None

        ),

        "company_website": (

            getattr(
                employer,
                "website",
                None
            )

            if employer

            else None

        )

    }), 200