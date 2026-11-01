from zoneinfo import ZoneInfo
import base64
from io import BytesIO
import qrcode
import hashlib
import secrets
from datetime import datetime, timedelta
from flask import (
    abort,
    current_app,
    jsonify,
    render_template,
    request,
    url_for,
    session,
    redirect
)

from flask_login import (
    login_required,
    current_user
)

from website.extensions import db, limiter
from website.utils.job_matching import calculate_job_match
from website.utils.notifications import notify_user
from website.utils.email import send_email
from website.utils.audit import record_audit
from website.utils.application_state import (
    mark_application_accepted,
)
from website.utils.referral_flow import (
    lock_job_for_hiring,
    validate_hiring_capacity_locked,
)

from website.models import (
    Employer,
    JobFair,
    JobFairEmployerRegistration,
    JobFairRegistration,
    JobFairQueue,
    JobFairJobGrouping,
    JobFairJobPreselection,
    Application,
    JobFairCandidateEvaluation,
    JobFairInterviewStation
)

from website.kiosk.queue_helpers import (
    activate_next_dormant_queue,
    activate_next_after_cancelled_queue,
)

from website.kiosk.kiosk_queue_routes import (
    get_queue_reference
)

from . import employer


# ============================================================
# PHASE 1B-V - TEMPORARY INTERVIEW STATION SESSION
# ============================================================

def _hash_station_secret(value):
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _station_is_usable(station):
    """Validate that a temporary station still belongs to a live approved fair registration."""
    if not station or station.is_revoked:
        return False
    if station.token_expires_at and station.token_expires_at < datetime.utcnow():
        return False

    reg = station.employer_registration
    if not reg or reg.status != "Approved":
        return False
    if station.job_fair_id != reg.job_fair_id:
        return False
    if station.station_number < 1 or station.station_number > min(reg.simultaneous_interview_capacity or 1, 15):
        return False

    fair = JobFair.query.get(station.job_fair_id)
    return bool(fair and fair.status == "Ongoing")


def get_station_session():
    station_id = session.get("job_fair_station_id")
    if not station_id:
        return None
    station = JobFairInterviewStation.query.get(station_id)
    if not _station_is_usable(station):
        session.pop("job_fair_station_id", None)
        return None
    station.last_seen_at = datetime.utcnow()
    return station

def ensure_main_station(registration):
    station = JobFairInterviewStation.query.filter_by(
        employer_registration_id=registration.registration_id, station_number=1
    ).first()
    if not station:
        station = JobFairInterviewStation(
            job_fair_id=registration.job_fair_id,
            employer_registration_id=registration.registration_id,
            station_number=1,
            is_revoked=False
        )
        db.session.add(station)
        db.session.flush()
    return station

def get_current_station(registration_ids=None):
    # A signed-in employer always operates Station 1, even when this browser
    # retains a stale temporary-station cookie after Station 2 disconnects.
    if current_user.is_authenticated and current_user.role == "employer":
        if not registration_ids:
            return None
        reg = JobFairEmployerRegistration.query.filter(
            JobFairEmployerRegistration.registration_id.in_(registration_ids)
        ).first()
        return ensure_main_station(reg) if reg else None
    station = get_station_session()
    if station:
        if registration_ids and station.employer_registration_id not in registration_ids:
            return None
        return station
    # A temporary station must never silently become Station 1 after expiry/revocation.
    if session.get("job_fair_temporary_station_mode"):
        return None
    if current_user.is_authenticated and current_user.role == "employer" and registration_ids:
        reg = JobFairEmployerRegistration.query.filter(
            JobFairEmployerRegistration.registration_id.in_(registration_ids)
        ).first()
        return ensure_main_station(reg) if reg else None
    return None

# ============================================================
# EMPLOYER ACCESS
# ============================================================

def employer_access_required():
    """Allow the authenticated employer or a valid temporary station."""
    if current_user.is_authenticated and current_user.role == "employer":
        return
    if session.get("job_fair_temporary_station_mode"):
        if get_station_session():
            return
        abort(403, description="Interview station access expired or was revoked. Reconnect using a new station link.")
    if get_station_session():
        return
    abort(403)


# ============================================================
# GET CURRENT EMPLOYER
# ============================================================

def get_current_employer():
    """
    Return the Employer record belonging to the
    currently logged-in user.
    """

    if current_user.is_authenticated and current_user.role == "employer":
        return Employer.query.filter_by(user_id=current_user.user_id).first()
    if session.get("job_fair_temporary_station_mode"):
        station = get_station_session()
        return station.employer_registration.employer if station and station.employer_registration else None
    station = get_station_session()
    if station and station.employer_registration:
        return station.employer_registration.employer
    return None


# ============================================================
# GET ONGOING JOB FAIR
# ============================================================

def get_ongoing_job_fair():
    """
    Return the currently ongoing job fair.
    """

    # An employer can participate in overlapping Job Fairs. Selecting the
    # globally earliest ongoing fair may show a different event from the
    # kiosk's assigned fair, leaving its queue apparently empty.
    employer_profile = get_current_employer()
    if employer_profile is None:
        return None

    now = datetime.now(ZoneInfo("Asia/Manila")).replace(tzinfo=None)
    return (
        JobFair.query
        .join(
            JobFairEmployerRegistration,
            JobFairEmployerRegistration.job_fair_id == JobFair.job_fair_id,
        )
        .filter(
            JobFairEmployerRegistration.employer_id == employer_profile.employer_id,
            JobFairEmployerRegistration.status == "Approved",
            JobFair.status == "Ongoing",
            JobFair.start_datetime <= now,
            JobFair.end_datetime > now,
        )
        .order_by(JobFair.start_datetime.desc(), JobFair.job_fair_id.desc())
        .first()
    )


# ============================================================
# GET EMPLOYER REGISTRATION IDS
# ============================================================

def get_employer_registration_ids(
    employer_id,
    job_fair_id
):
    """
    Return all approved employer registration IDs
    belonging to this employer for the specified job fair.
    """

    registrations = (
        JobFairEmployerRegistration.query
        .filter(

            JobFairEmployerRegistration.job_fair_id
            == job_fair_id,

            JobFairEmployerRegistration.employer_id
            == employer_id,

            JobFairEmployerRegistration.status
            == "Approved"

        )
        .all()
    )

    return [
        registration.registration_id
        for registration in registrations
    ]


# ============================================================
# GET QUEUE JOBS
# ============================================================

def get_queue_jobs(queue):
    """
    Return the jobs grouped under a queue.
    """

    groupings = (
        JobFairJobGrouping.query
        .filter(
            JobFairJobGrouping.queue_group_id
            == queue.queue_group_id
        )
        .order_by(
            JobFairJobGrouping.grouping_id.asc()
        )
        .all()
    )

    jobs = []

    for grouping in groupings:

        job_fair_job = grouping.job_fair_job

        if not job_fair_job:
            continue

        job = job_fair_job.job

        if not job:
            continue

        accepted_count = (
            Application.query
            .filter_by(
                job_id=job.job_id,
                status="Accepted"
            )
            .count()
        )

        total_vacancies = int(
            job.vacancies or 0
        )

        remaining_vacancies = max(
            total_vacancies - accepted_count,
            0
        )

        jobs.append({

            "grouping_id":
                grouping.grouping_id,

            "job_fair_job_id":
                job_fair_job.job_fair_job_id,

            "job_id":
                job.job_id,

            "job_title":
                job.job_title,

            "job_type":
                job.job_type,

            "job_status":
                job.status,

            "total_vacancies":
                total_vacancies,

            "accepted_count":
                accepted_count,

            "remaining_vacancies":
                remaining_vacancies,

        })

    return jobs


# ============================================================
# GET QUEUE JOB EVALUATIONS
# ============================================================

def get_queue_job_evaluations(queue):
    """
    Return all evaluations belonging to the current queue,
    organized by job_fair_job_id.

    Each selected job gets its own evaluation.
    """

    if not queue:
        return {}

    evaluations = (
        JobFairCandidateEvaluation.query
        .filter(

            JobFairCandidateEvaluation.job_fair_id
            == queue.job_fair_id,

            JobFairCandidateEvaluation
            .job_fair_registration_id
            == queue.job_fair_registration_id,

            JobFairCandidateEvaluation
            .employer_registration_id
            == queue.employer_registration_id

        )
        .all()
    )

    return {
        evaluation.job_fair_job_id:
            evaluation
        for evaluation in evaluations
    }


# ============================================================
# GET SINGLE QUEUE JOB EVALUATION
# ============================================================

def get_queue_job_evaluation(
    queue,
    job_fair_job_id
):
    """
    Return the evaluation for one specific job
    belonging to the current queue.
    """

    if not queue or not job_fair_job_id:
        return None

    return (
        JobFairCandidateEvaluation.query
        .filter(

            JobFairCandidateEvaluation.job_fair_id
            == queue.job_fair_id,

            JobFairCandidateEvaluation
            .job_fair_registration_id
            == queue.job_fair_registration_id,

            JobFairCandidateEvaluation
            .employer_registration_id
            == queue.employer_registration_id,

            JobFairCandidateEvaluation
            .job_fair_job_id
            == job_fair_job_id

        )
        .first()
    )


# ============================================================
# CHECK ALL QUEUE JOBS EVALUATED
# ============================================================

def get_queue_evaluation_completion(queue):
    """
    Determine whether every job in the queue has
    a saved evaluation.

    Returns:

        {
            "complete": True/False,
            "total_jobs": int,
            "evaluated_jobs": int,
            "missing_job_fair_job_ids": [...]
        }
    """

    queue_jobs = get_queue_jobs(queue)

    if not queue_jobs:

        return {
            "complete": True,
            "total_jobs": 0,
            "evaluated_jobs": 0,
            "missing_job_fair_job_ids": []
        }

    evaluations = get_queue_job_evaluations(queue)

    job_ids = {
        job["job_fair_job_id"]
        for job in queue_jobs
    }

    evaluated_job_ids = {
        job_id
        for job_id in evaluations.keys()
        if job_id in job_ids
    }

    missing_job_ids = sorted(
        job_ids - evaluated_job_ids
    )

    return {
        "complete":
            len(missing_job_ids) == 0,

        "total_jobs":
            len(job_ids),

        "evaluated_jobs":
            len(evaluated_job_ids),

        "missing_job_fair_job_ids":
            missing_job_ids
    }


# ============================================================
# CREATE APPLICATIONS FROM QUEUE
# ============================================================

def create_queue_applications(queue):
    """
    Create Application records for every job associated
    with the completed queue.

    Existing applications are reused.

    This function does NOT commit.
    """

    registration = queue.job_fair_registration

    if not registration:
        raise ValueError(
            "Job fair registration not found for queue."
        )

    job_seeker_id = registration.job_seeker_id

    if not job_seeker_id:
        raise ValueError(
            "The queue does not have a valid job seeker."
        )

    groupings = (
        JobFairJobGrouping.query
        .filter(
            JobFairJobGrouping.queue_group_id
            == queue.queue_group_id
        )
        .order_by(
            JobFairJobGrouping.grouping_id.asc()
        )
        .all()
    )

    if not groupings:

        current_app.logger.warning(
            "Queue %s has no JobFairJobGrouping records.",
            queue.queue_group_id
        )

    created_application_ids = []

    for grouping in groupings:

        job_fair_job = grouping.job_fair_job

        if not job_fair_job:

            current_app.logger.warning(
                "Grouping %s has no JobFairJob.",
                grouping.grouping_id
            )

            continue

        job = job_fair_job.job

        if not job:

            current_app.logger.warning(
                "JobFairJob %s has no Job.",
                job_fair_job.job_fair_job_id
            )

            continue

        application = (
            Application.query
            .filter(

                Application.job_id
                == job.job_id,

                Application.job_seeker_id
                == job_seeker_id

            )
            .first()
        )

        if not application:

            application = Application(

                job_id=job.job_id,

                job_seeker_id=job_seeker_id,

                status="Pending"

            )

            db.session.add(application)

            db.session.flush()

            created_application_ids.append(
                application.application_id
            )

            current_app.logger.info(
                "Created application: "
                "application_id=%s, "
                "job_id=%s, "
                "job_seeker_id=%s",

                application.application_id,

                job.job_id,

                job_seeker_id
            )

        else:

            current_app.logger.info(
                "Existing application reused: "
                "application_id=%s, "
                "job_id=%s, "
                "job_seeker_id=%s",

                application.application_id,

                job.job_id,

                job_seeker_id
            )

        # ====================================================
        # REMOVE PRESELECTION
        # ====================================================

        preselection = (
            JobFairJobPreselection.query
            .filter(

                JobFairJobPreselection
                .job_fair_registration_id
                == registration.registration_id,

                JobFairJobPreselection
                .job_fair_job_id
                == job_fair_job.job_fair_job_id

            )
            .first()
        )

        if preselection:

            current_app.logger.info(
                "Removing Job Fair preselection: "
                "preselection_id=%s, "
                "registration_id=%s, "
                "job_fair_job_id=%s",

                preselection.preselection_id,

                registration.registration_id,

                job_fair_job.job_fair_job_id
            )

            db.session.delete(
                preselection
            )

    return {
        "applications_created":
            created_application_ids
    }


# ============================================================
# BUILD QUEUE DATA
# ============================================================

def build_employer_queue_data(queues):

    queue_data = []

    for queue in queues:

        employer_registration = (
            queue.employer_registration
        )

        employer_obj = None

        if employer_registration:
            employer_obj = (
                employer_registration.employer
            )

        queue_reference = get_queue_reference(
            queue
        )

        queue_data.append({

            "queue_group_id":
                queue.queue_group_id,

            "queue_number":
                queue_reference,

            "numeric_queue_number":
                queue.queue_number,

            "queue_reference":
                queue_reference,

            "priority_order":
                queue.priority_order,

            "status":
                queue.status,

            "assigned_station_id": queue.assigned_station_id,
            "station_number": queue.assigned_station.station_number if queue.assigned_station else None,
            "serving_started_at": queue.serving_started_at.isoformat() if queue.serving_started_at else None,

            "queued_at":
                queue.queued_at.isoformat()
                if queue.queued_at
                else None,

            "activated_at":
                queue.activated_at.isoformat()
                if queue.activated_at
                else None,

            "finished_at":
                queue.finished_at.isoformat()
                if queue.finished_at
                else None,

            "job_fair_registration_id":
                queue.job_fair_registration_id,

            "employer_registration_id":
                queue.employer_registration_id,

            "company":
                employer_obj.company_name
                if employer_obj
                else "Unknown Employer",

            "jobs":
                get_queue_jobs(queue)

        })

    return queue_data


# ============================================================
# BUILD REGISTRATION MAP
# ============================================================

def build_queue_registration_map(queues):

    registration_ids = {
        queue.job_fair_registration_id
        for queue in queues
        if queue.job_fair_registration_id
    }

    if not registration_ids:
        return {}

    registrations = (
        JobFairRegistration.query
        .filter(
            JobFairRegistration.registration_id.in_(
                registration_ids
            )
        )
        .all()
    )

    return {
        registration.registration_id:
            registration
        for registration in registrations
    }


# ============================================================
# BUILD EMPLOYER REGISTRATION MAP
# ============================================================

def build_employer_registration_map(
    employer_registration_ids
):

    if not employer_registration_ids:
        return {}

    registrations = (
        JobFairEmployerRegistration.query
        .filter(
            JobFairEmployerRegistration.registration_id.in_(
                employer_registration_ids
            )
        )
        .all()
    )

    return {
        registration.registration_id:
            registration
        for registration in registrations
    }


# ============================================================
# BUILD SELECTED QUEUE DATA MAP
# ============================================================

def build_selected_queue_data_map(
    active_queue_data
):

    return {
        queue["queue_group_id"]:
            queue
        for queue in active_queue_data
    }



def queue_audit_identity(queue):
    reg = queue.job_fair_registration if queue else None
    seeker = reg.job_seeker if reg else None
    applicant = seeker.full_name if seeker else "Unknown applicant"
    reference = reg.reference_id if reg and reg.reference_id else "N/A"
    return f"applicant={applicant} reference={reference}"

def queue_state_signature(job_fair_id, registration_ids):
    rows = (JobFairQueue.query.filter(JobFairQueue.job_fair_id == job_fair_id, JobFairQueue.employer_registration_id.in_(registration_ids), JobFairQueue.status.in_(["Active","Dormant"])).order_by(JobFairQueue.queue_group_id.asc()).all())
    return "|".join(f"{q.queue_group_id}:{q.status}:{q.assigned_station_id or 0}:{q.skip_count or 0}" for q in rows)



def auto_claim_initial_station_queue(fair_id, registration_ids):
    """Claim exactly the first applicant for this station, never auto-advance.

    Uses the same fair-level lock and eligibility rules as Call Next.
    An already-used station is marked initialized without claiming anyone.
    """
    station = get_current_station(registration_ids)
    if not station or not _station_is_usable(station) or station.initial_auto_claim_done:
        return False

    try:
        fair = (JobFair.query.filter_by(job_fair_id=fair_id)
                .with_for_update().first())
        if not fair or fair.status != "Ongoing":
            db.session.rollback()
            return False
        station = (JobFairInterviewStation.query
                   .filter_by(station_id=station.station_id)
                   .with_for_update().first())
        if (not _station_is_usable(station)
                or station.employer_registration_id not in registration_ids
                or station.initial_auto_claim_done):
            db.session.rollback()
            return False

        # Existing or historical assignments mean this is NOT a fresh station.
        # Do not advance it merely because its current interview has ended.
        previously_assigned = JobFairQueue.query.filter(
            JobFairQueue.assigned_station_id == station.station_id
        ).first()
        if previously_assigned:
            station.initial_auto_claim_done = True
            db.session.commit()
            return False

        candidate = get_next_callable_queue(fair_id, registration_ids)
        if not candidate:
            db.session.rollback()
            return False
        applicant_queues = (JobFairQueue.query.filter_by(
            job_fair_id=fair_id,
            job_fair_registration_id=candidate.job_fair_registration_id,
            status="Active"
        ).order_by(JobFairQueue.queue_group_id.asc()).with_for_update().all())
        if any(q.assigned_station_id is not None for q in applicant_queues):
            db.session.rollback()
            return False

        candidate.assigned_station_id = station.station_id
        candidate.serving_started_at = datetime.utcnow()
        station.initial_auto_claim_done = True
        record_audit(
            f"JOB_FAIR_QUEUE_INITIAL_AUTO_CALLED queue_group_id={candidate.queue_group_id} "
            f"station={station.station_number} {queue_audit_identity(candidate)}"
        )
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        raise

# ============================================================
# QUEUE MANAGEMENT PAGE
# ============================================================

@employer.route(
    "/job-fair/queue-management"
)
def queue_management():

    employer_access_required()

    current_employer = get_current_employer()

    if not current_employer:

        return (
            "Employer profile not found.",
            404
        )

    ongoing_job_fair = get_ongoing_job_fair()

    if not ongoing_job_fair:

        return render_template(
            "employer/employer_queue_management.html",

            employer=current_employer,

            ongoing_job_fair=None,

            active_queues=[],
            dormant_queues=[],

            active_queue_data=[],
            dormant_queue_data=[],

            active_queue_data_by_id={},
            dormant_queue_data_by_id={},

            queue_registration_map={},
            employer_registration_map={},

            selected_queue_data_map={},

            selected_queue=None,

            selected_evaluation=None,

            queue_job_evaluations={},

            queue_evaluation_completion={
                "complete": False,
                "total_jobs": 0,
                "evaluated_jobs": 0,
                "missing_job_fair_job_ids": []
            },

            selected_job_fair_job_id=None

        )

    registration_ids = get_employer_registration_ids(
        current_employer.employer_id,
        ongoing_job_fair.job_fair_id
    )

    if not registration_ids:

        return render_template(
            "employer/employer_queue_management.html",

            employer=current_employer,

            ongoing_job_fair=ongoing_job_fair,

            active_queues=[],
            dormant_queues=[],

            active_queue_data=[],
            dormant_queue_data=[],

            active_queue_data_by_id={},
            dormant_queue_data_by_id={},

            queue_registration_map={},
            employer_registration_map={},

            selected_queue_data_map={},

            selected_queue=None,

            selected_evaluation=None,

            queue_job_evaluations={},

            queue_evaluation_completion={
                "complete": False,
                "total_jobs": 0,
                "evaluated_jobs": 0,
                "missing_job_fair_job_ids": []
            },

            selected_job_fair_job_id=None

        )

    # First assignment is automatic for every authorized station (1..capacity).
    # Do this before loading page data so Now Serving is immediately accurate.
    auto_claim_initial_station_queue(ongoing_job_fair.job_fair_id, registration_ids)

    active_queues = (
        JobFairQueue.query
        .filter(

            JobFairQueue.job_fair_id
            == ongoing_job_fair.job_fair_id,

            JobFairQueue.employer_registration_id.in_(
                registration_ids
            ),

            JobFairQueue.status
            == "Active"

        )
        .order_by(
            JobFairQueue.queued_at.asc()
        )
        .all()
    )

    dormant_queues = (
        JobFairQueue.query
        .filter(

            JobFairQueue.job_fair_id
            == ongoing_job_fair.job_fair_id,

            JobFairQueue.employer_registration_id.in_(
                registration_ids
            ),

            JobFairQueue.status
            == "Dormant"

        )
        .order_by(
            JobFairQueue.queued_at.asc()
        )
        .all()
    )

    active_queue_data = build_employer_queue_data(
        active_queues
    )

    dormant_queue_data = build_employer_queue_data(
        dormant_queues
    )

    active_queue_data_by_id = {
        item["queue_group_id"]: item
        for item in active_queue_data
    }

    dormant_queue_data_by_id = {
        item["queue_group_id"]: item
        for item in dormant_queue_data
    }

    all_queues = (
        active_queues +
        dormant_queues
    )

    queue_registration_map = (
        build_queue_registration_map(
            all_queues
        )
    )

    employer_registration_map = (
        build_employer_registration_map(
            registration_ids
        )
    )

    selected_queue_data_map = (
        build_selected_queue_data_map(
            active_queue_data
        )
    )

    # Phase 1B-V/VI: an Active queue without a station assignment is
    # Currently Waiting.  Only the Active queue assigned to this
    # station is Currently Serving and may be processed here.
    selected_queue = None

    current_station = get_current_station(registration_ids)
    requested_queue_id = request.args.get("queue", type=int)
    if current_station:
        selected_queue = next((
            queue for queue in active_queues
            if queue.assigned_station_id == current_station.station_id
        ), None)
        if requested_queue_id and selected_queue and selected_queue.queue_group_id != requested_queue_id:
            # Stale browser history is not an authorization failure. Keep the
            # currently assigned queue, never load the requested foreign queue.
            return redirect(url_for("employer.queue_management"))

    queue_job_evaluations = (
        get_queue_job_evaluations(
            selected_queue
        )
        if selected_queue
        else {}
    )

    queue_evaluation_completion = (
        get_queue_evaluation_completion(
            selected_queue
        )
        if selected_queue
        else {
            "complete": False,
            "total_jobs": 0,
            "evaluated_jobs": 0,
            "missing_job_fair_job_ids": []
        }
    )

    selected_job_fair_job_id = None
    selected_evaluation = None

    if selected_queue:

        queue_jobs = get_queue_jobs(
            selected_queue
        )

        if queue_jobs:

            selected_job_fair_job_id = (
                queue_jobs[0]["job_fair_job_id"]
            )

            selected_evaluation = (
                queue_job_evaluations.get(
                    selected_job_fair_job_id
                )
            )

    # Reuse the existing employer qualification matcher for each selected
    # Job Fair position; never infer a single score across different jobs.
    queue_qualification_matches = {}
    if selected_queue and selected_queue.job_fair_registration:
        seeker = selected_queue.job_fair_registration.job_seeker
        if seeker:
            for grouping in JobFairJobGrouping.query.filter_by(
                queue_group_id=selected_queue.queue_group_id
            ).all():
                fair_job = grouping.job_fair_job
                job = fair_job.job if fair_job else None
                if job:
                    result = calculate_job_match(job, seeker)
                    queue_qualification_matches[str(fair_job.job_fair_job_id)] = {
                        "percentage": round(result["match_percentage"]),
                        "required": result["required"],
                        "preferred": result["preferred"],
                        "details": [
                            {"label": detail.get("label") or detail.get("name") or "Qualification",
                             "matched": bool(detail.get("matched"))}
                            for detail in result["details"]
                        ],
                        "has_criteria": bool(result["criteria_total"]),
                    }

    return render_template(
        "employer/employer_queue_management.html",

        employer=current_employer,

        ongoing_job_fair=ongoing_job_fair,

        active_queues=active_queues,
        dormant_queues=dormant_queues,

        active_queue_data=active_queue_data,
        dormant_queue_data=dormant_queue_data,

        active_queue_data_by_id=
            active_queue_data_by_id,

        dormant_queue_data_by_id=
            dormant_queue_data_by_id,

        queue_registration_map=
            queue_registration_map,

        employer_registration_map=
            employer_registration_map,

        selected_queue_data_map=
            selected_queue_data_map,

        selected_queue=selected_queue,

        selected_evaluation=
            selected_evaluation,

        queue_job_evaluations=
            queue_job_evaluations,
        queue_qualification_matches=queue_qualification_matches,

        queue_evaluation_completion=
            queue_evaluation_completion,

        selected_job_fair_job_id=
            selected_job_fair_job_id,

        current_station=current_station,
        is_temporary_station=bool(get_station_session()),
        station_capacity=max((r.simultaneous_interview_capacity or 1) for r in employer_registration_map.values()) if employer_registration_map else 1,
        queue_state_signature=queue_state_signature(ongoing_job_fair.job_fair_id, registration_ids)

    )


@employer.route("/job-fair/queue-management/state")
def queue_management_state():
    employer_access_required()
    current_employer = get_current_employer()
    fair = get_ongoing_job_fair()
    if not current_employer or not fair:
        return jsonify({"success": True, "signature": ""})
    registration_ids = get_employer_registration_ids(current_employer.employer_id, fair.job_fair_id)
    if not registration_ids:
        return jsonify({"success": True, "signature": ""})
    # Polling can discover the first waiting applicant after the station opened.
    auto_claim_initial_station_queue(fair.job_fair_id, registration_ids)
    return jsonify({"success": True, "signature": queue_state_signature(fair.job_fair_id, registration_ids)})

# ============================================================
# PHASE 1B-V - TEMPORARY STATION MANAGEMENT
# ============================================================

@employer.route("/job-fair/stations", methods=["GET"])
@login_required
def station_manager():
    employer_access_required()
    current_employer = get_current_employer()
    fair = get_ongoing_job_fair()
    if not current_employer or not fair:
        return jsonify({"success": False, "message": "No ongoing Job Fair."}), 400
    regs = JobFairEmployerRegistration.query.filter_by(
        job_fair_id=fair.job_fair_id, employer_id=current_employer.employer_id, status="Approved"
    ).all()
    if not regs:
        return jsonify({"success": False, "message": "No approved Job Fair registration."}), 403
    reg = regs[0]
    ensure_main_station(reg)
    db.session.commit()
    stations = JobFairInterviewStation.query.filter_by(employer_registration_id=reg.registration_id).order_by(JobFairInterviewStation.station_number).all()
    return jsonify({"success": True, "capacity": reg.simultaneous_interview_capacity or 1, "stations": [
        {"station_id": x.station_id, "station_number": x.station_number, "revoked": x.is_revoked, "expires_at": x.token_expires_at.isoformat() if x.token_expires_at else None}
        for x in stations
    ]})

@employer.route("/job-fair/stations/<int:station_number>/connect", methods=["POST"])
@limiter.limit("5 per minute; 20 per hour")
@login_required
def create_station_access(station_number):
    employer_access_required()
    current_employer = get_current_employer(); fair = get_ongoing_job_fair()
    reg = JobFairEmployerRegistration.query.filter_by(job_fair_id=fair.job_fair_id, employer_id=current_employer.employer_id, status="Approved").first() if fair else None
    if not reg:
        return jsonify({"success": False, "message": "Approved registration not found."}), 404
    capacity = min(max(reg.simultaneous_interview_capacity or 1, 1), 15)
    if station_number < 2 or station_number > capacity:
        return jsonify({"success": False, "message": f"Station must be between 2 and {capacity}."}), 400
    station = JobFairInterviewStation.query.filter_by(employer_registration_id=reg.registration_id, station_number=station_number).first()
    if not station:
        station = JobFairInterviewStation(job_fair_id=fair.job_fair_id, employer_registration_id=reg.registration_id, station_number=station_number)
        db.session.add(station)
    token = secrets.token_urlsafe(32); code = f"{secrets.randbelow(1000000):06d}"
    station.access_token_hash = _hash_station_secret(token); station.short_code_hash = _hash_station_secret(code)
    station.token_expires_at = min(fair.end_datetime + timedelta(hours=2), datetime.utcnow() + timedelta(hours=12))
    station.is_revoked = False
    access_url = url_for("employer.station_access", token=token, _external=True)
    qr = qrcode.make(access_url)
    qr_buffer = BytesIO(); qr.save(qr_buffer, format="PNG")
    qr_data_url = "data:image/png;base64," + base64.b64encode(qr_buffer.getvalue()).decode("ascii")
    record_audit(f"JOB_FAIR_STATION_ACCESS_CREATED registration_id={reg.registration_id} station={station_number}")
    db.session.commit()
    return jsonify({"success": True, "station_number": station_number, "short_code": code, "access_url": access_url, "qr_data_url": qr_data_url, "code_url": url_for("employer.station_code_access", station_id=station.station_id, _external=True), "expires_at": station.token_expires_at.isoformat()})

@employer.route("/job-fair/stations/<int:station_number>/revoke", methods=["POST"])
@login_required
def revoke_station(station_number):
    employer_access_required(); current_employer=get_current_employer(); fair=get_ongoing_job_fair()
    reg=JobFairEmployerRegistration.query.filter_by(job_fair_id=fair.job_fair_id, employer_id=current_employer.employer_id, status="Approved").first() if fair else None
    station=JobFairInterviewStation.query.filter_by(employer_registration_id=reg.registration_id, station_number=station_number).first() if reg else None
    if not station or station_number == 1:
        return jsonify({"success": False, "message": "Station not found or cannot be revoked."}), 404
    serving=JobFairQueue.query.filter_by(assigned_station_id=station.station_id, status="Active").first()
    if serving:
        serving.assigned_station_id=None; serving.serving_started_at=None
    station.is_revoked=True; station.access_token_hash=None; station.short_code_hash=None
    record_audit(f"JOB_FAIR_STATION_REVOKED station_id={station.station_id}")
    db.session.commit()
    return jsonify({"success": True, "message": "Station disconnected. Any assigned applicant returned to waiting."})

@employer.route("/job-fair/station/access/<token>")
def station_access(token):
    # Refuse oversized or malformed bearer tokens before touching the database.
    if not token or len(token) > 128 or not all(c.isalnum() or c in "-_" for c in token):
        abort(403)
    hashed = _hash_station_secret(token)
    try:
        # Lock before verifying the one-time token. A concurrent code/token
        # redemption must see the credentials already consumed.
        station = (JobFairInterviewStation.query
                   .filter_by(access_token_hash=hashed, is_revoked=False)
                   .with_for_update().first())
        if not _station_is_usable(station) or not station.access_token_hash:
            db.session.rollback()
            abort(403)
        station.access_token_hash = None
        station.short_code_hash = None
        station.last_seen_at = datetime.utcnow()
        station_id = station.station_id
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    session["job_fair_station_id"] = station_id
    session["job_fair_temporary_station_mode"] = True
    return redirect(url_for("employer.queue_management"))

@employer.route("/job-fair/station/connect/<int:station_id>", methods=["GET", "POST"])
@limiter.limit("5 per minute; 20 per hour", methods=["POST"])
def station_code_access(station_id):
    if request.method == "GET":
        station = JobFairInterviewStation.query.get_or_404(station_id)
        return render_template("employer/interview_station_connect.html", station=station)
    code = (request.form.get("code") or "").strip()
    # Codes are generated as exactly six ASCII digits; reject all other input.
    if len(code) != 6 or not code.isascii() or not code.isdecimal():
        return render_template(
            "employer/interview_station_connect.html",
            station=JobFairInterviewStation.query.get_or_404(station_id),
            error="Enter the six-digit station code.",
        ), 400
    try:
        station = (JobFairInterviewStation.query
                   .filter_by(station_id=station_id)
                   .with_for_update().first_or_404())
        valid = (
            _station_is_usable(station)
            and bool(station.short_code_hash)
            and secrets.compare_digest(station.short_code_hash, _hash_station_secret(code))
        )
        if not valid:
            db.session.rollback()
            return render_template("employer/interview_station_connect.html", station=station, error="Invalid or expired station code."), 403
        station.access_token_hash = None
        station.short_code_hash = None
        station.last_seen_at = datetime.utcnow()
        connected_station_id = station.station_id
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    session["job_fair_station_id"] = connected_station_id
    session["job_fair_temporary_station_mode"] = True
    return redirect(url_for("employer.queue_management"))

@employer.route("/job-fair/station/disconnect", methods=["POST"])
def station_disconnect():
    session.pop("job_fair_station_id", None)
    session.pop("job_fair_temporary_station_mode", None)
    return jsonify({"success": True})

# ============================================================
# PHASE 1B-III - NEXT CALLABLE QUEUE
# ============================================================

def get_next_callable_queue(job_fair_id, registration_ids):
    """Oldest eligible waiting queue for this employer.

    Eligible means Active, not already assigned to a station, and the
    applicant is not currently serving at another employer in this fair.
    """
    busy_registration_ids = db.session.query(JobFairQueue.job_fair_registration_id).filter(
        JobFairQueue.job_fair_id == job_fair_id,
        JobFairQueue.status == "Active",
        JobFairQueue.assigned_station_id.isnot(None)
    )
    return (
        JobFairQueue.query
        .filter(
            JobFairQueue.job_fair_id == job_fair_id,
            JobFairQueue.employer_registration_id.in_(registration_ids),
            JobFairQueue.status == "Active",
            JobFairQueue.assigned_station_id.is_(None),
            ~JobFairQueue.job_fair_registration_id.in_(busy_registration_ids)
        )
        .order_by(
            # FCFS across pre-registered and on-site applicants.
            # Skip history must not override the original queue-entry time.
            JobFairQueue.queued_at.asc(),
            JobFairQueue.queue_group_id.asc()
        ).first()
    )


# ============================================================
# CALL NEXT ACTIVE APPLICANT
# ============================================================
@employer.route("/job-fair/queue/call-next", methods=["POST"])
def call_next_queue():
    employer_access_required()
    current_employer = get_current_employer()

    if not current_employer:
        return jsonify({
            "success": False,
            "message": "Employer profile not found."
        }), 403

    ongoing_job_fair = get_ongoing_job_fair()

    if not ongoing_job_fair:
        return jsonify({
            "success": False,
            "message": "There is currently no ongoing job fair."
        }), 400

    registration_ids = get_employer_registration_ids(
        current_employer.employer_id,
        ongoing_job_fair.job_fair_id
    )

    if not registration_ids:
        return jsonify({
            "success": False,
            "message": "You are not approved for this Job Fair."
        }), 403

    locked_fair = (
        db.session.query(JobFair)
        .filter(JobFair.job_fair_id == ongoing_job_fair.job_fair_id)
        .with_for_update()
        .first()
    )
    if locked_fair is None or locked_fair.status != "Ongoing":
        db.session.rollback()
        return jsonify({"success": False, "message": "The Job Fair is no longer ongoing."}), 409

    station = get_current_station(registration_ids)
    if not station or not _station_is_usable(station):
        db.session.rollback()
        return jsonify({"success": False, "message": "No active interview station is available."}), 403

    existing = JobFairQueue.query.filter_by(
        assigned_station_id=station.station_id, status="Active"
    ).first()
    if existing:
        db.session.rollback()
        return jsonify({"success": False, "message": "Finish or release the applicant currently assigned to this station first."}), 409

    next_queue = get_next_callable_queue(
        ongoing_job_fair.job_fair_id,
        registration_ids
    )

    if not next_queue:
        db.session.rollback()
        return jsonify({
            "success": False,
            "message": "There are no eligible Active applicants waiting to be called."
        }), 404

    applicant_active_queues = JobFairQueue.query.filter_by(
        job_fair_id=ongoing_job_fair.job_fair_id,
        job_fair_registration_id=next_queue.job_fair_registration_id,
        status="Active"
    ).order_by(JobFairQueue.queue_group_id.asc()).with_for_update().all()
    if any(q.assigned_station_id is not None for q in applicant_active_queues):
        db.session.rollback()
        return jsonify({"success": False, "message": "That applicant has just been called by another employer. Please use Call Next again."}), 409

    next_queue.assigned_station_id = station.station_id
    next_queue.serving_started_at = datetime.utcnow()
    station.initial_auto_claim_done = True
    record_audit(f"JOB_FAIR_QUEUE_CALLED queue_group_id={next_queue.queue_group_id} station={station.station_number} {queue_audit_identity(next_queue)}")
    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Next applicant selected.",
        "queue_group_id": next_queue.queue_group_id,
        "queue_reference": get_queue_reference(next_queue),
        "queue_number": next_queue.queue_number,
        "open_url": url_for(
            "employer.queue_management",
            queue=next_queue.queue_group_id
        )
    }), 200


# ============================================================
# SKIP APPLICANT (TEMPORARY)
# ============================================================

@employer.route(
    "/job-fair/queue/<int:queue_group_id>/skip",
    methods=["POST"]
)
def skip_queue(queue_group_id):
    """Temporarily skip the currently callable applicant.

    The queue remains Active, keeps the same queue number/priority, and
    becomes eligible again on a later Call Next.  There is deliberately
    no timeout or automatic cancellation.
    """
    employer_access_required()
    current_employer = get_current_employer()
    ongoing_job_fair = get_ongoing_job_fair()

    if not current_employer or not ongoing_job_fair:
        return jsonify({"success": False, "message": "The Job Fair queue is not available."}), 400

    registration_ids = get_employer_registration_ids(
        current_employer.employer_id,
        ongoing_job_fair.job_fair_id
    )
    if not registration_ids:
        return jsonify({"success": False, "message": "You are not approved for this Job Fair."}), 403

    # Lock the queue row before validating/mutating it. This makes repeated
    # Skip/No-show requests deterministic instead of allowing two requests
    # to transition the same Active queue concurrently.
    queue = (
        JobFairQueue.query
        .filter_by(queue_group_id=queue_group_id)
        .with_for_update()
        .first()
    )
    if not queue or queue.job_fair_id != ongoing_job_fair.job_fair_id or queue.employer_registration_id not in registration_ids:
        return jsonify({"success": False, "message": "Queue entry not found for this employer."}), 404
    if queue.status != "Active":
        return jsonify({"success": False, "message": "Only an Active queue can be skipped."}), 409

    station = get_current_station(registration_ids)
    if not station or queue.assigned_station_id != station.station_id:
        return jsonify({"success": False, "message": "This applicant is not assigned to this interview station."}), 409

    # Once any job in this employer visit has been evaluated, the applicant
    # has attended an interview and cannot be skipped or marked absent.
    if get_queue_job_evaluations(queue):
        db.session.rollback()
        return jsonify({
            "success": False,
            "message": "This applicant has already been evaluated. Update the evaluation or finish the applicant instead."
        }), 409

    queue.assigned_station_id = None
    queue.serving_started_at = None
    queue.skip_count = (queue.skip_count or 0) + 1
    record_audit(
        f"JOB_FAIR_QUEUE_SKIPPED queue_group_id={queue.queue_group_id} "
        f"queue_number={queue.queue_number} skip_count={queue.skip_count} {queue_audit_identity(queue)}"
    )
    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Applicant skipped temporarily. Their queue remains Active and eligible for a later call.",
        "skip_count": queue.skip_count
    }), 200


# ============================================================
# MARK APPLICANT NO-SHOW / CANCEL THIS EMPLOYER QUEUE
# ============================================================

@employer.route(
    "/job-fair/queue/<int:queue_group_id>/no-show",
    methods=["POST"]
)
def mark_queue_no_show(queue_group_id):
    """Explicitly cancel one employer queue as No-show.

    This is separate from the Mismatch evaluation.  Registration,
    attendance, completed queues, and existing evaluations are preserved.
    """
    employer_access_required()
    current_employer = get_current_employer()
    ongoing_job_fair = get_ongoing_job_fair()

    if not current_employer or not ongoing_job_fair:
        return jsonify({"success": False, "message": "The Job Fair queue is not available."}), 400

    registration_ids = get_employer_registration_ids(
        current_employer.employer_id,
        ongoing_job_fair.job_fair_id
    )
    if not registration_ids:
        return jsonify({"success": False, "message": "You are not approved for this Job Fair."}), 403

    # Lock the queue row before validating/mutating it. This makes repeated
    # Skip/No-show requests deterministic instead of allowing two requests
    # to transition the same Active queue concurrently.
    queue = (
        JobFairQueue.query
        .filter_by(queue_group_id=queue_group_id)
        .with_for_update()
        .first()
    )
    if not queue or queue.job_fair_id != ongoing_job_fair.job_fair_id or queue.employer_registration_id not in registration_ids:
        return jsonify({"success": False, "message": "Queue entry not found for this employer."}), 404
    if queue.status != "Active":
        return jsonify({"success": False, "message": "Only an Active queue can be marked as No-show."}), 409

    station = get_current_station(registration_ids)
    if not station or queue.assigned_station_id != station.station_id:
        return jsonify({"success": False, "message": "This applicant is not assigned to this interview station."}), 409

    # Once any job in this employer visit has been evaluated, the applicant
    # has attended an interview and cannot be skipped or marked absent.
    if get_queue_job_evaluations(queue):
        db.session.rollback()
        return jsonify({
            "success": False,
            "message": "This applicant has already been evaluated. Update the evaluation or finish the applicant instead."
        }), 409

    queue.assigned_station_id = None
    queue.serving_started_at = None
    queue.status = "Cancelled"
    queue.cancellation_reason = "No-show"

    # Cancelling this employer visit must not strand the applicant's later
    # employer selections.  Continue their personal progression normally.
    activate_next_after_cancelled_queue(queue)

    record_audit(
        f"JOB_FAIR_QUEUE_NO_SHOW queue_group_id={queue.queue_group_id} "
        f"queue_number={queue.queue_number} reason=No-show {queue_audit_identity(queue)}"
    )
    db.session.commit()

    return jsonify({
        "success": True,
        "message": "This employer queue was cancelled as No-show. The applicant's other Job Fair history was preserved."
    }), 200


# ============================================================
# FINISH APPLICANT
# ============================================================

@employer.route(
    "/job-fair/queue/<int:queue_group_id>/finish",
    methods=["POST"]
)
@employer.route(
    "/job-fair/queue/<int:queue_group_id>/next",
    methods=["POST"]
)
def finish_queue(queue_group_id):
    """
    Finish only the applicant currently being processed by this
    employer.  This deliberately does NOT choose/call another
    applicant for the employer.  The applicant's next personal
    employer queue may still be activated as part of their normal
    progression.

    The legacy /next URL is retained as a compatibility alias while
    the UI moves to the explicit /finish action.
    """

    employer_access_required()

    current_employer = get_current_employer()

    if not current_employer:

        return jsonify({
            "success": False,
            "message":
                "Employer profile not found."
        }), 403

    ongoing_job_fair = get_ongoing_job_fair()

    if not ongoing_job_fair:

        return jsonify({
            "success": False,
            "message":
                "There is currently no ongoing job fair."
        }), 400

    registration_ids = get_employer_registration_ids(
        current_employer.employer_id,
        ongoing_job_fair.job_fair_id
    )

    if not registration_ids:

        return jsonify({
            "success": False,
            "message":
                "You are not approved for this Job Fair."
        }), 403

    # Serialize Finish against duplicate Finish/Skip/No-show requests and
    # against capacity changes that may release this station assignment.
    queue = (
        JobFairQueue.query
        .filter(
            JobFairQueue.queue_group_id
            == queue_group_id
        )
        .with_for_update()
        .first()
    )

    if not queue:

        return jsonify({
            "success": False,
            "message":
                "Queue entry not found.",
            "queue_group_id":
                queue_group_id
        }), 404

    if queue.job_fair_id != ongoing_job_fair.job_fair_id:

        return jsonify({
            "success": False,
            "message":
                "This queue belongs to a different Job Fair."
        }), 403

    if (
        queue.employer_registration_id
        not in registration_ids
    ):

        return jsonify({
            "success": False,
            "message":
                "This queue does not belong to your employer."
        }), 403

    employer_registration = (
        JobFairEmployerRegistration.query
        .filter(
            JobFairEmployerRegistration.registration_id
            == queue.employer_registration_id
        )
        .first()
    )

    if not employer_registration:

        return jsonify({
            "success": False,
            "message":
                "The employer registration attached to "
                "this queue could not be found."
        }), 404

    if employer_registration.status != "Approved":

        return jsonify({
            "success": False,
            "message":
                "The employer registration for this queue "
                "is no longer approved."
        }), 403

    if (
        employer_registration.employer_id
        != current_employer.employer_id
    ):

        return jsonify({
            "success": False,
            "message":
                "This queue belongs to another employer."
        }), 403

    if queue.status != "Active":

        return jsonify({
            "success": False,
            "message":
                "This queue entry is not currently active."
        }), 400

    station = get_current_station(registration_ids)
    if not station or queue.assigned_station_id != station.station_id:
        return jsonify({"success": False, "message": "This applicant is not assigned to this interview station."}), 409

    job_fair_registration = (
        JobFairRegistration.query
        .filter(
            JobFairRegistration.registration_id
            == queue.job_fair_registration_id
        )
        .first()
    )

    if not job_fair_registration:

        return jsonify({
            "success": False,
            "message":
                "The job seeker registration attached "
                "to this queue could not be found."
        }), 404

    # ========================================================
    # REQUIRE EVERY JOB TO BE EVALUATED
    # ========================================================

    evaluation_completion = (
        get_queue_evaluation_completion(
            queue
        )
    )

    if not evaluation_completion["complete"]:

        missing_count = len(
            evaluation_completion[
                "missing_job_fair_job_ids"
            ]
        )

        return jsonify({
            "success": False,
            "message":
                "Please evaluate every selected job "
                "before finishing this applicant.",
            "missing_jobs":
                missing_count,
            "evaluated_jobs":
                evaluation_completion[
                    "evaluated_jobs"
                ],
            "total_jobs":
                evaluation_completion[
                    "total_jobs"
                ]
        }), 400

    # ========================================================
    # GET QUEUE JOBS
    # ========================================================

    queue_jobs = get_queue_jobs(queue)

    current_app.logger.info(
        "Queue %s contains %s job(s): %s",
        queue.queue_group_id,
        len(queue_jobs),
        queue_jobs
    )

    try:

        # ====================================================
        # CREATE APPLICATIONS
        # ====================================================

        application_result = (
            create_queue_applications(
                queue
            )
        )

        current_app.logger.info(
            "Queue application result: "
            "applications_created=%s",

            application_result[
                "applications_created"
            ]
        )

        # ====================================================
        # FINISH CURRENT QUEUE
        # ACTIVATE NEXT APPLICANT QUEUE
        # ====================================================

        queue.assigned_station_id = None
        queue.serving_started_at = None
        next_personal_queue = (
            activate_next_dormant_queue(
                queue
            )
        )

        record_audit(
            f"JOB_FAIR_QUEUE_FINISHED queue_group_id={queue.queue_group_id} "
            f"station={station.station_number} {queue_audit_identity(queue)}"
        )

        # ====================================================
        # COMMIT EVERYTHING
        # ====================================================

        db.session.commit()

        current_app.logger.info(
            "Employer queue processed successfully: "
            "queue_group_id=%s",

            queue_group_id
        )

    except Exception:

        db.session.rollback()

        current_app.logger.exception(
            "Failed to process employer queue "
            "queue_group_id=%s",

            queue_group_id
        )

        return jsonify({
            "success": False,
            "message":
                "Unable to process the queue.",
            "queue_group_id":
                queue_group_id
        }), 500

    next_queue_data = None

    if next_personal_queue:

        next_queue_reference = get_queue_reference(
            next_personal_queue
        )

        next_queue_data = {

            "queue_group_id":
                next_personal_queue.queue_group_id,

            "queue_number":
                next_queue_reference,

            "numeric_queue_number":
                next_personal_queue.queue_number,

            "queue_reference":
                next_queue_reference,

            "priority_order":
                next_personal_queue.priority_order,

            "status":
                next_personal_queue.status

        }

    return jsonify({

        "success":
            True,

        "message":
            "Applicant finished successfully.",

        "finished_queue": {

            "queue_group_id":
                queue.queue_group_id,

            "queue_number":
                get_queue_reference(queue),

            "numeric_queue_number":
                queue.queue_number,

            "queue_reference":
                get_queue_reference(queue),

            "priority_order":
                queue.priority_order,

            "status":
                queue.status

        },

        "applications_created":
            len(
                application_result[
                    "applications_created"
                ]
            ),

        "next_personal_queue":
            next_queue_data

    }), 200


# ============================================================
# GET OR CREATE APPLICATION FOR QUEUE JOB
# ============================================================

def get_or_create_queue_application(
    queue,
    job_fair_job
):
    """
    Return the Application belonging to the current
    job seeker and specific JobFairJob.

    If the application does not exist yet, create it.

    This function does NOT commit.
    """

    registration = queue.job_fair_registration

    if not registration:
        raise ValueError(
            "Job fair registration not found."
        )

    job_seeker_id = registration.job_seeker_id

    if not job_seeker_id:
        raise ValueError(
            "Job fair registration has no job seeker."
        )

    if not job_fair_job:
        raise ValueError(
            "Job Fair job not found."
        )

    job = job_fair_job.job

    if not job:
        raise ValueError(
            "Job Fair job has no associated Job."
        )

    application = (
        Application.query
        .filter(
            Application.job_id
            == job.job_id,

            Application.job_seeker_id
            == job_seeker_id
        )
        .first()
    )

    if application:

        current_app.logger.info(
            "Existing application reused: "
            "application_id=%s, "
            "job_id=%s, "
            "job_seeker_id=%s",

            application.application_id,
            job.job_id,
            job_seeker_id
        )

        return application

    application = Application(

        job_id=
            job.job_id,

        job_seeker_id=
            job_seeker_id,

        status=
            "Pending"

    )

    db.session.add(application)

    db.session.flush()

    current_app.logger.info(
        "Created application for queue evaluation: "
        "application_id=%s, "
        "job_id=%s, "
        "job_seeker_id=%s",

        application.application_id,
        job.job_id,
        job_seeker_id
    )

    return application


# ============================================================
# SAVE QUEUE JOB EVALUATION
# ============================================================

@employer.route(
    "/job-fair/queue/<int:queue_group_id>/evaluation",
    methods=["POST"]
)
def save_queue_evaluation(queue_group_id):

    employer_access_required()

    current_employer = get_current_employer()

    if not current_employer:

        return jsonify({
            "success": False,
            "message":
                "Employer profile not found."
        }), 403

    ongoing_job_fair = get_ongoing_job_fair()

    if not ongoing_job_fair:

        return jsonify({
            "success": False,
            "message":
                "There is currently no ongoing job fair."
        }), 400

    queue = (
        JobFairQueue.query
        .filter(
            JobFairQueue.queue_group_id
            == queue_group_id
        )
        .with_for_update()
        .first()
    )

    if not queue:

        return jsonify({
            "success": False,
            "message":
                "Queue entry not found."
        }), 404

    if queue.job_fair_id != ongoing_job_fair.job_fair_id:

        return jsonify({
            "success": False,
            "message":
                "This queue belongs to another Job Fair."
        }), 403

    if queue.status != "Active":

        return jsonify({
            "success": False,
            "message":
                "This queue is not currently active."
        }), 400

    registration_ids = get_employer_registration_ids(
        current_employer.employer_id,
        ongoing_job_fair.job_fair_id
    )

    if (
        queue.employer_registration_id
        not in registration_ids
    ):

        return jsonify({
            "success": False,
            "message":
                "This queue does not belong to your employer."
        }), 403

    employer_registration = (
        JobFairEmployerRegistration.query
        .filter(
            JobFairEmployerRegistration.registration_id
            == queue.employer_registration_id
        )
        .first()
    )

    if not employer_registration:

        return jsonify({
            "success": False,
            "message":
                "Employer registration not found."
        }), 404

    if employer_registration.status != "Approved":

        return jsonify({
            "success": False,
            "message":
                "This employer registration is no longer approved."
        }), 403

    if (
        employer_registration.employer_id
        != current_employer.employer_id
    ):

        return jsonify({
            "success": False,
            "message":
                "This queue belongs to another employer."
        }), 403

    job_fair_registration = (
        JobFairRegistration.query
        .filter(
            JobFairRegistration.registration_id
            == queue.job_fair_registration_id
        )
        .first()
    )

    if not job_fair_registration:

        return jsonify({
            "success": False,
            "message":
                "Job seeker registration not found."
        }), 404

    station = get_current_station(registration_ids)
    if not station or queue.assigned_station_id != station.station_id:
        return jsonify({"success": False, "message": "This applicant is not assigned to this interview station."}), 409

    # Temporary interview stations authenticate through a station session,
    # not Flask-Login. Candidate evaluations require a real users.user_id FK.
    # Attribute them to the verified employer account and retain the actual
    # station identity in the audit trail for accountability.
    evaluator_user_id = current_employer.user_id
    if not evaluator_user_id:
        return jsonify({"success": False, "message": "Employer account is unavailable for evaluation."}), 403

    data = request.get_json(
        silent=True
    ) or {}

    job_fair_job_id = data.get(
        "job_fair_job_id"
    )

    status = (
        data.get("status") or ""
    ).strip()

    remarks = (
        data.get("remarks") or ""
    ).strip()

    allowed_statuses = {
        "Hired",
        "Nearly-hired",
        "Qualified",
        "Mismatch"
    }

    if not job_fair_job_id:

        return jsonify({
            "success": False,
            "message":
                "Please select a job to evaluate."
        }), 400

    try:

        job_fair_job_id = int(
            job_fair_job_id
        )

    except (
        TypeError,
        ValueError
    ):

        return jsonify({
            "success": False,
            "message":
                "Invalid job selection."
        }), 400

    if status not in allowed_statuses:

        return jsonify({
            "success": False,
            "message":
                "Invalid candidate status."
        }), 400

    if not remarks:

        return jsonify({
            "success": False,
            "message":
                "Please enter remarks."
        }), 400

    if len(remarks) > 2000:
        db.session.rollback()
        return jsonify({
            "success": False,
            "message": "Remarks must be 2,000 characters or fewer."
        }), 400

    # ========================================================
    # MAKE SURE THE SELECTED JOB BELONGS TO THIS QUEUE
    # ========================================================

    queue_job = (
        JobFairJobGrouping.query
        .join(
            JobFairJobGrouping.job_fair_job
        )
        .filter(

            JobFairJobGrouping.queue_group_id
            == queue.queue_group_id,

            JobFairJobGrouping.job_fair_job_id
            == job_fair_job_id

        )
        .first()
    )

    # ========================================================
    # IMPORTANT:
    # CHECK queue_job BEFORE ACCESSING job_fair_job
    # ========================================================

    if not queue_job:

        return jsonify({
            "success": False,
            "message":
                "The selected job does not belong "
                "to this applicant's queue."
        }), 400

    job_fair_job = queue_job.job_fair_job

    if not job_fair_job:

        return jsonify({
            "success": False,
            "message":
                "The selected Job Fair job could not be found."
        }), 404

    # ========================================================
    # GET OR CREATE APPLICATION FOR THIS SPECIFIC JOB
    # ========================================================

    try:

        application = get_or_create_queue_application(
            queue,
            job_fair_job
        )

    except ValueError as error:

        return jsonify({
            "success": False,
            "message": str(error)
        }), 400

    if application.job_id != job_fair_job.job_id:

        return jsonify({
            "success": False,
            "message":
                "The application does not belong "
                "to the selected job."
        }), 400

    # Verify evaluation ownership BEFORE changing the hiring state. A
    # mismatched evaluation must not trigger acceptance or vacancy changes.
    existing_evaluation = (
        JobFairCandidateEvaluation.query
        .filter_by(application_id=application.application_id)
        .first()
    )
    if existing_evaluation and (
        existing_evaluation.job_fair_id != queue.job_fair_id
        or existing_evaluation.job_fair_registration_id != queue.job_fair_registration_id
        or existing_evaluation.employer_registration_id != queue.employer_registration_id
        or existing_evaluation.job_fair_job_id != job_fair_job_id
    ):
        db.session.rollback()
        return jsonify({
            "success": False,
            "message": "This application has an evaluation from another Job Fair context."
        }), 409

    # ========================================================
    # JOB FAIR HIRING CONSISTENCY
    #
    # "Hired" is a final employment outcome. It must therefore
    # pass through the same Application Accepted state and
    # vacancy protection used by the normal employer workflow.
    # ========================================================

    if application.status == "Accepted" and status != "Hired":
        return jsonify({
            "success": False,
            "message": (
                "This applicant has already been hired. "
                "The Job Fair evaluation can no longer be "
                "changed to a non-hired result."
            )
        }), 409

    if application.status == "Rejected" and status == "Hired":
        return jsonify({
            "success": False,
            "message": (
                "This application has already been rejected "
                "and cannot be changed to Hired."
            )
        }), 409

    if status == "Hired" and application.status != "Accepted":

        locked_job = lock_job_for_hiring(
            application.job_id
        )

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

            return jsonify({
                "success": False,
                "message": capacity_error,
            }), 409

        accepted, acceptance_error = (
            mark_application_accepted(
                application
            )
        )

        if not accepted:
            db.session.rollback()

            return jsonify({
                "success": False,
                "message": acceptance_error,
            }), 409

        # Close the job when this hire consumes the final
        # available vacancy.
        if (
            accepted_count + 1
            >= int(locked_job.vacancies or 0)
        ):
            locked_job.status = "Closed"

    # ========================================================
    # FIND EXISTING EVALUATION FOR THIS APPLICATION
    # ========================================================

    evaluation = existing_evaluation

    previous_evaluation_status = (
        evaluation.status if evaluation else None
    )

    if evaluation:

        # ====================================================
        # PREVENT OVERWRITING AN EVALUATION FROM ANOTHER
        # JOB FAIR / EMPLOYER / REGISTRATION / JOB
        # ====================================================

        if (
            evaluation.job_fair_id
            != queue.job_fair_id
            or
            evaluation.job_fair_registration_id
            != queue.job_fair_registration_id
            or
            evaluation.employer_registration_id
            != queue.employer_registration_id
            or
            evaluation.job_fair_job_id
            != job_fair_job_id
        ):

            current_app.logger.warning(
                "Application %s already has an evaluation "
                "belonging to another Job Fair context. "
                "Current queue=%s, existing evaluation=%s",

                application.application_id,

                queue.queue_group_id,

                evaluation.evaluation_id
            )

            return jsonify({
                "success": False,
                "message":
                    "This application already has a "
                    "candidate evaluation from another "
                    "Job Fair or evaluation context."
            }), 409

        evaluation.status = status

        evaluation.remarks = remarks

        evaluation.evaluated_by = (
            evaluator_user_id
        )

    else:

        evaluation = JobFairCandidateEvaluation(

            application_id=
                application.application_id,

            job_fair_id=
                ongoing_job_fair.job_fair_id,

            job_fair_registration_id=
                queue.job_fair_registration_id,

            employer_registration_id=
                queue.employer_registration_id,

            job_fair_job_id=
                job_fair_job_id,

            evaluated_by=
                evaluator_user_id,

            status=
                status,

            remarks=
                remarks
        )

        db.session.add(
            evaluation
        )

    # ========================================================
    # JOBSEEKER NOTIFICATION
    #
    # Every meaningful Job Fair evaluation outcome should be
    # visible inside the Jobseeker portal. Do not create a
    # duplicate notification when the employer saves the same
    # status again without changing the evaluation outcome.
    # ========================================================

    if previous_evaluation_status != status:
        record_audit(
            f"JOB_FAIR_EVALUATION application_id={application.application_id} "
            f"job_id={application.job_id} from={previous_evaluation_status or 'None'} to={status}"
        )
        status_messages = {
            "Hired": (
                "Job Fair Evaluation: Hired",
                f"Congratulations! You were marked as Hired for "
                f"{application.job.job_title} during "
                f"{ongoing_job_fair.title}.",
            ),
            "Nearly-hired": (
                "Job Fair Evaluation: Nearly-hired",
                f"Your evaluation for {application.job.job_title} during "
                f"{ongoing_job_fair.title} was updated to Nearly-hired.",
            ),
            "Qualified": (
                "Job Fair Evaluation: Qualified",
                f"You were marked as Qualified for "
                f"{application.job.job_title} during "
                f"{ongoing_job_fair.title}.",
            ),
            "Mismatch": (
                "Job Fair Evaluation: Mismatch",
                f"Your evaluation for {application.job.job_title} during "
                f"{ongoing_job_fair.title} was updated to Mismatch.",
            ),
        }

        notification_title, notification_message = (
            status_messages[status]
        )

        notify_user(
            application.job_seeker.user_id,
            notification_title,
            notification_message,
            "Job Fair",
        )

    try:

        db.session.commit()

    except Exception:

        db.session.rollback()

        current_app.logger.exception(
            "Failed to save candidate evaluation: "
            "queue_group_id=%s, "
            "job_fair_job_id=%s",

            queue_group_id,
            job_fair_job_id
        )

        return jsonify({
            "success": False,
            "message":
                "Unable to save candidate evaluation."
        }), 500

    # ========================================================
    # JOB FAIR HIRED EMAIL
    #
    # Send the hiring email only when the evaluation actually
    # transitions into Hired. The evaluation and in-app
    # notification are already committed above, so an email
    # delivery problem must not undo the hiring result.
    # ========================================================

    if (
        status == "Hired"
        and previous_evaluation_status != "Hired"
    ):
        try:
            jobseeker = application.job_seeker
            job = application.job

            jobseeker_email = (
                jobseeker.user.email
                if jobseeker and jobseeker.user
                else None
            )

            if not jobseeker_email:
                raise ValueError(
                    "The jobseeker does not have an email address."
                )

            company_name = current_employer.company_name
            job_fair_title = ongoing_job_fair.title

            email_body = f"""Hello {jobseeker.first_name},

Congratulations! We are pleased to inform you that you were marked as Hired for {job.job_title} at {company_name} during {job_fair_title}.

Our hiring team will be in touch with you shortly regarding the next onboarding steps.

Thank you.

{company_name}
via PESO Management Information System
"""

            send_email(
                recipient=jobseeker_email,
                subject=f"Job Fair Hiring Result - {job.job_title}",
                body=email_body,
            )

        except Exception:
            current_app.logger.exception(
                "Job Fair hired email delivery failed: "
                "application_id=%s, job_fair_id=%s",
                application.application_id,
                ongoing_job_fair.job_fair_id,
            )

    completion = (
        get_queue_evaluation_completion(
            queue
        )
    )

    return jsonify({

        "success":
            True,

        "message":
            "Candidate evaluation saved successfully.",

        "evaluation": {

            "evaluation_id":
                evaluation.evaluation_id,

            "job_fair_job_id":
                evaluation.job_fair_job_id,

            "status":
                evaluation.status,

            "remarks":
                evaluation.remarks

        },

        "completion": {

            "complete":
                completion["complete"],

            "evaluated_jobs":
                completion["evaluated_jobs"],

            "total_jobs":
                completion["total_jobs"]

        }

    }), 200