from zoneinfo import ZoneInfo
from datetime import datetime, timezone, date
import re
import json
import hashlib
import uuid

from flask import (
    abort,
    current_app,
    render_template,
    redirect,
    url_for,
    flash,
    jsonify,
    request,
    url_for
)

from sqlalchemy import or_, and_

from flask_login import (
    login_required,
    current_user
)

from . import kiosk

from website.extensions import db
from website.utils.job_fair_lifecycle import is_queue_entry_open

from website.models import (
    JobFair,
    JobFairQueue,
    JobFairJob,
    JobFairJobGrouping,
    JobFairRegistration,
    JobFairJobPreselection,
    Application,
    JobFairInterviewStation,
    JobFairTicketPrintAttempt,
    AuditLog
)


# ============================================================
# KIOSK ACCESS
# ============================================================

def kiosk_access_required():
    """
    Make sure the currently logged-in account
    is an active kiosk account.
    """

    if not current_user.is_authenticated:
        abort(403)

    if current_user.role != "kiosk":
        abort(403)

    if not current_user.kiosk:
        abort(403)

    if current_user.kiosk.status != "active":
        abort(403)


# ============================================================
# GET CURRENT ONGOING JOB FAIR
# ============================================================

def get_ongoing_job_fair():
    """Return the active Job Fair assigned to this authenticated kiosk.

    Other kiosk pages use kiosk.job_fair_id. Selecting the first globally
    ongoing fair here could send an applicant to a different event when
    multiple fairs overlap.
    """
    if (
        not current_user.is_authenticated
        or current_user.role != "kiosk"
        or not current_user.kiosk
        or current_user.kiosk.mode != "job_fair"
        or not current_user.kiosk.job_fair_id
    ):
        return None

    now = datetime.now(ZoneInfo("Asia/Manila")).replace(tzinfo=None)
    return (
        JobFair.query
        .filter(
            JobFair.job_fair_id == current_user.kiosk.job_fair_id,
            JobFair.status == "Ongoing",
            JobFair.start_datetime <= now,
            JobFair.end_datetime > now,
        )
        .first()
    )


# ============================================================
# CHECK IF JOB IS EXPIRED
# ============================================================

def is_job_expired(job):
    """
    Return True when the job deadline has already passed.
    """

    if not job:
        return True

    if job.deadline is None:
        return False

    return job.deadline < date.today()


# ============================================================
# GET JOB FAIR JOB DISPLAY STATUS
# ============================================================

def get_job_fair_job_display_status(job):
    """
    Return the status that should be used for displaying a job.
    """

    if is_job_expired(job):
        return "Job Fair-Closed"

    if job.status == "Job Fair-Closed":
        return "Job Fair-Closed"

    return job.status


# ============================================================
# CHECK IF JOB IS CLOSED
# ============================================================

def is_job_fair_job_closed(job):
    """
    Return True when a Job Fair job should no longer be available.
    """

    if not job:
        return True

    if is_job_expired(job):
        return True

    if job.status == "Job Fair-Closed":
        return True

    return False


# ============================================================
# GET PRESELECTED JOB FAIR JOB IDS
# ============================================================

def selected_job_fair_job_ids_for_registration(
    registration_id
):
    """
    Return the applicant's selected JobFairJob IDs.

    The original preselection order is preserved.
    """

    records = (
        JobFairJobPreselection.query
        .filter(
            JobFairJobPreselection.job_fair_registration_id
            == registration_id
        )
        .order_by(
            JobFairJobPreselection.preselected_at.asc(),
            JobFairJobPreselection.preselection_id.asc()
        )
        .all()
    )

    return [
        record.job_fair_job_id
        for record in records
    ]


# ============================================================
# GET APPLIED JOB IDS
# ============================================================

def get_applied_job_ids_for_registration(
    registration
):
    """
    Return Job IDs that the applicant has already applied to.

    These jobs remain visible in the kiosk Job Fair job list,
    but their Select Job button will be disabled.
    """

    if not registration:
        return set()

    job_seeker = registration.job_seeker

    if not job_seeker:
        return set()

    job_seeker_id = getattr(
        job_seeker,
        "job_seeker_id",
        None
    )

    if job_seeker_id is None:
        return set()

    applied_job_ids = (
        db.session.query(
            Application.job_id
        )
        .filter(
            Application.job_seeker_id
            == job_seeker_id,

            Application.job_id.isnot(None)
        )
        .all()
    )

    return {
        job_id
        for (job_id,) in applied_job_ids
    }


# ============================================================
# GET VALID PRESELECTED JOBS
# ============================================================

def get_valid_preselected_jobs(
    registration,
    ongoing_job_fair
):
    """
    Return the registration's currently valid preselected
    JobFairJob records.

    The applicant's preselection order is preserved.
    """

    if not registration or not ongoing_job_fair:
        return []

    applied_job_ids = (
        get_applied_job_ids_for_registration(
            registration
        )
    )

    preselection_records = (
        JobFairJobPreselection.query
        .filter(
            JobFairJobPreselection.job_fair_registration_id
            == registration.registration_id
        )
        .order_by(
            JobFairJobPreselection.preselected_at.asc(),
            JobFairJobPreselection.preselection_id.asc()
        )
        .all()
    )

    valid_jobs = []

    for preselection in preselection_records:

        job_fair_job = preselection.job_fair_job

        if not job_fair_job:
            continue

        employer_registration = (
            job_fair_job.registration
        )

        if not employer_registration:
            continue

        if (
            employer_registration.job_fair_id
            != ongoing_job_fair.job_fair_id
        ):
            continue

        if (
            employer_registration.status
            != "Approved"
        ):
            continue

        job = job_fair_job.job

        if not job:
            continue

        if (
            job.approval_status
            != "Approved"
        ):
            continue

        if is_job_fair_job_closed(job):
            continue

        if job.job_id in applied_job_ids:
            continue

        valid_jobs.append({
            "preselection": preselection,
            "job_fair_job": job_fair_job,
            "job": job
        })

    return valid_jobs


# ============================================================
# GET AVAILABLE JOB FAIR JOBS
# ============================================================

def get_available_job_fair_jobs(
    ongoing_job_fair
):
    """
    Return only Job Fair jobs that are currently available.
    """

    if not ongoing_job_fair:
        return []

    job_fair_jobs = (
        JobFairJob.query
        .join(
            JobFairJob.registration
        )
        .filter(
            JobFairJob.registration.has(
                job_fair_id=ongoing_job_fair.job_fair_id
            ),

            JobFairJob.registration.has(
                status="Approved"
            )
        )
        .all()
    )

    available_jobs = []

    for job_fair_job in job_fair_jobs:

        job = job_fair_job.job

        if not job:
            continue

        if job.approval_status != "Approved":
            continue

        if is_job_expired(job):
            continue

        if job.status == "Job Fair-Closed":
            continue

        if job.status != "Job Fair":
            continue

        available_jobs.append(
            job_fair_job
        )

    return available_jobs


# ============================================================
# BUILD QUEUE PREFIX
# ============================================================

def get_queue_prefix(
    employer_registration
):
    """
    Build the user-facing queue prefix.

    Priority:

        1. Booth number
        2. Company name
        3. QUEUE

    Examples:

        A01
        Booth 12 -> BOOTH-12
        ABC Corporation -> ABC-CORPORATION
    """

    if not employer_registration:
        return "QUEUE"

    booth_number = (
        employer_registration.booth_number
    )

    if booth_number:

        booth_number = str(
            booth_number
        ).strip()

        if booth_number:

            booth_prefix = re.sub(
                r"[^A-Za-z0-9]+",
                "-",
                booth_number
            ).strip("-").upper()

            if booth_prefix:
                return booth_prefix

    employer = (
        employer_registration.employer
    )

    if employer and employer.company_name:

        company_name = str(
            employer.company_name
        ).strip()

        company_prefix = re.sub(
            r"[^A-Za-z0-9]+",
            "-",
            company_name
        ).strip("-").upper()

        if company_prefix:

            if len(company_prefix) > 20:
                company_prefix = (
                    company_prefix[:20]
                    .rstrip("-")
                )

            return company_prefix

    return "QUEUE"


# ============================================================
# BUILD USER-FACING QUEUE REFERENCE
# ============================================================

def get_queue_reference(
    queue
):
    """
    Convert the internal numeric queue number into
    the user-facing booth-aware queue reference.

    Database:

        queue_number = 1

    Display:

        A01-001
    """

    if not queue:
        return "QUEUE-000"

    prefix = get_queue_prefix(
        queue.employer_registration
    )

    try:
        number = int(
            queue.queue_number
        )
    except (
        TypeError,
        ValueError
    ):
        number = 0

    return f"{prefix}-{number:03d}"


# ============================================================
# QUEUING NUMBER PAGE
# ============================================================

@kiosk.route(
    "/queuing-number/<reference_id>"
)
@login_required
def queuing_number(reference_id):

    kiosk_access_required()

    reference_id = reference_id.strip().upper()

    registration = (
        JobFairRegistration.query
        .filter(
            JobFairRegistration.reference_id
            == reference_id
        )
        .first()
    )

    if not registration:

        flash(
            "Invalid or expired reference number.",
            "error"
        )

        return redirect(
            url_for(
                "kiosk.registration"
            )
        )

    ongoing_job_fair = get_ongoing_job_fair()

    if not ongoing_job_fair:

        flash(
            "There is currently no ongoing job fair.",
            "error"
        )

        return redirect(
            url_for(
                "kiosk.personal_information",
                reference_id=reference_id
            )
        )

    if (
        registration.job_fair_id
        != ongoing_job_fair.job_fair_id
    ):

        flash(
            "Your registration is not for the current job fair.",
            "error"
        )

        return redirect(
            url_for(
                "kiosk.registration"
            )
        )

    # A valid reference entered at the authenticated physical kiosk
    # is the attendance/check-in event. This happens before job
    # selection so "Attended, Did Not Apply" can be represented.
    if registration.attendance_status != "Attended":
        registration.attendance_status = "Attended"

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "Unable to record Job Fair check-in for registration %s.",
                registration.registration_id,
            )
            flash(
                "We could not record your Job Fair check-in. Please try again.",
                "error",
            )
            return redirect(url_for("kiosk.registration"))

    valid_preselected_jobs = get_valid_preselected_jobs(
        registration,
        ongoing_job_fair
    )

    selected_job_fair_jobs = [
        item["job_fair_job"]
        for item in valid_preselected_jobs
    ]

    selected_job_fair_job_ids = [
        item["job_fair_job"].job_fair_job_id
        for item in valid_preselected_jobs
    ]

    existing_queues = (
        JobFairQueue.query
        .filter(
            JobFairQueue.job_fair_id
            == ongoing_job_fair.job_fair_id,

            JobFairQueue.job_fair_registration_id
            == registration.registration_id
        )
        .order_by(
            JobFairQueue.priority_order.asc()
        )
        .all()
    )

    existing_queue_data = build_queue_response(
        existing_queues
    )

    queued_job_fair_job_ids = {
        grouping.job_fair_job_id
        for queue in existing_queues
        if queue.status != "Cancelled"
        for grouping in queue.job_groupings
    }

    # Returning applicants may append only jobs that are not already tied
    # to an existing employer queue. Existing queue records/order stay intact.
    if existing_queues:
        selected_job_fair_jobs = [
            job_fair_job
            for job_fair_job in selected_job_fair_jobs
            if job_fair_job.job_fair_job_id not in queued_job_fair_job_ids
        ]
        selected_job_fair_job_ids = [
            job_fair_job.job_fair_job_id
            for job_fair_job in selected_job_fair_jobs
        ]

    queue_entry_open = is_queue_entry_open(ongoing_job_fair)
    if not queue_entry_open:
        selected_job_fair_jobs = []
        selected_job_fair_job_ids = []

    return render_template(
        "kiosk/QueuingNumberKiosk.html",

        registration=registration,

        job_seeker=registration.job_seeker,

        ongoing_job_fair=ongoing_job_fair,

        selected_job_fair_jobs=
            selected_job_fair_jobs,

        selected_job_fair_job_ids=
            selected_job_fair_job_ids,

        existing_queues=
            existing_queues,

        existing_queue_data=
            existing_queue_data,

        queue_entry_open=
            queue_entry_open
    )


# ============================================================
# CREATE QUEUE NUMBERS
# ============================================================

@kiosk.route("/queuing-number/<reference_id>/create", methods=["POST"])
@login_required
def create_queue(reference_id):
    kiosk_access_required()
    reference_id = reference_id.strip().upper()

    registration = JobFairRegistration.query.filter_by(reference_id=reference_id).first()
    if not registration:
        return jsonify({"success": False, "message": "Invalid registration."}), 404

    ongoing_job_fair = get_ongoing_job_fair()
    if not ongoing_job_fair:
        return jsonify({"success": False, "message": "There is currently no ongoing job fair."}), 400

    if registration.job_fair_id != ongoing_job_fair.job_fair_id:
        return jsonify({"success": False, "message": "Registration does not belong to this job fair."}), 403

    if registration.attendance_status != "Attended":
        registration.attendance_status = "Attended"

    existing_queues = (
        JobFairQueue.query
        .filter(
            JobFairQueue.job_fair_id == ongoing_job_fair.job_fair_id,
            JobFairQueue.job_fair_registration_id == registration.registration_id,
        )
        .order_by(JobFairQueue.priority_order.asc())
        .all()
    )

    if not is_queue_entry_open(ongoing_job_fair):
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return jsonify({
            "success": False,
            "queue_entry_closed": True,
            "message": "Queue entry for this Job Fair has closed. Existing queues may continue processing.",
            "queues": build_queue_response(existing_queues),
        }), 403

    request_data = request.get_json(silent=True)
    if not isinstance(request_data, dict):
        return jsonify({"success": False, "message": "Invalid queue request."}), 400
    raw_ids = request_data.get("selected_job_fair_job_ids", [])
    if not isinstance(raw_ids, list):
        return jsonify({"success": False, "message": "Invalid job selection."}), 400

    normalized_ids = []
    for value in raw_ids:
        if isinstance(value, bool):
            return jsonify({"success": False, "message": "Invalid job selection."}), 400
        if isinstance(value, int):
            job_fair_job_id = value
        elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
            job_fair_job_id = int(value)
        else:
            return jsonify({"success": False, "message": "Invalid job selection."}), 400
        if job_fair_job_id <= 0:
            return jsonify({"success": False, "message": "Invalid job selection."}), 400
        if job_fair_job_id not in normalized_ids:
            normalized_ids.append(job_fair_job_id)

    if not normalized_ids:
        return jsonify({"success": False, "message": "Please select at least one job."}), 400

    valid_preselected_jobs = get_valid_preselected_jobs(registration, ongoing_job_fair)
    valid_by_id = {
        item["job_fair_job"].job_fair_job_id: item
        for item in valid_preselected_jobs
    }

    selected_items = []
    for job_fair_job_id in normalized_ids:
        item = valid_by_id.get(job_fair_job_id)
        if not item:
            return jsonify({
                "success": False,
                "message": "One or more selected jobs are no longer valid or are not selected for this Job Fair.",
            }), 400
        selected_items.append(item)

    existing_by_employer = {
        q.employer_registration_id: q
        for q in existing_queues
        if q.status != "Cancelled"
    }
    existing_grouping_job_ids = {
        grouping.job_fair_job_id
        for queue in existing_queues
        if queue.status != "Cancelled"
        for grouping in queue.job_groupings
    }
    existing_queue_states = {
        q.queue_group_id: (q.status, q.assigned_station_id, q.serving_started_at)
        for q in existing_queues
    }

    employer_groups = {}
    employer_order = []
    for item in selected_items:
        job_fair_job = item["job_fair_job"]
        if job_fair_job.job_fair_job_id in existing_grouping_job_ids:
            continue

        employer_registration = job_fair_job.registration
        if (not employer_registration
                or employer_registration.job_fair_id != ongoing_job_fair.job_fair_id
                or employer_registration.status != "Approved"):
            continue

        employer_registration_id = employer_registration.registration_id
        if employer_registration_id not in employer_groups:
            employer_groups[employer_registration_id] = {
                "employer_registration": employer_registration,
                "jobs": [],
            }
            employer_order.append(employer_registration_id)
        employer_groups[employer_registration_id]["jobs"].append(job_fair_job)

    new_job_ids_by_employer = {
        employer_id: {job.job_fair_job_id for job in group["jobs"]}
        for employer_id, group in employer_groups.items()
    }

    if not employer_groups:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return jsonify({
            "success": True,
            "already_queued": True,
            "message": "These jobs are already in your queue. No new ticket was created or authorized for printing.",
            "no_changes": True,
            "print_queues": [],
            "queues": build_queue_response(existing_queues),
        }), 200

    closed_employers = []
    for employer_registration_id in employer_order:
        queue = existing_by_employer.get(employer_registration_id)
        if queue and queue.status in {"Finished", "Cancelled"}:
            employer = queue.employer_registration.employer if queue.employer_registration else None
            closed_employers.append(employer.company_name if employer else "Employer")

    if closed_employers:
        return jsonify({
            "success": False,
            "message": "You cannot add a new job to an employer visit that is already finished or cancelled.",
            "employers": closed_employers,
        }), 409

    now = datetime.now(timezone.utc)
    changed_queues = []
    newly_created_queue_ids = []
    try:
        locked_fair = (db.session.query(JobFair)
            .filter(JobFair.job_fair_id == ongoing_job_fair.job_fair_id)
            .with_for_update().first())
        if locked_fair is None or locked_fair.status != "Ongoing":
            db.session.rollback()
            return jsonify({"success": False, "message": "The Job Fair is no longer ongoing."}), 409
        if not is_queue_entry_open(locked_fair):
            db.session.rollback()
            return jsonify({"success": False, "queue_entry_closed": True,
                            "message": "Queue entry has closed. Please refresh."}), 409

        fresh_queues = (
            JobFairQueue.query.filter(
                JobFairQueue.job_fair_id == ongoing_job_fair.job_fair_id,
                JobFairQueue.job_fair_registration_id == registration.registration_id,
            ).order_by(JobFairQueue.priority_order.asc())
            .with_for_update().all()
        )
        fresh_ids = {q.queue_group_id for q in fresh_queues}
        initial_ids = {q.queue_group_id for q in existing_queues}
        fresh_grouping_ids = {
            grouping.job_fair_job_id
            for queue in fresh_queues
            if queue.status != "Cancelled"
            for grouping in queue.job_groupings
        }
        fresh_queue_states = {
            q.queue_group_id: (q.status, q.assigned_station_id, q.serving_started_at)
            for q in fresh_queues
        }
        if (fresh_ids != initial_ids
                or fresh_grouping_ids != existing_grouping_job_ids
                or fresh_queue_states != existing_queue_states):
            db.session.rollback()
            return jsonify({
                "success": False,
                "message": "Your queue was updated by another request. Please refresh and try again.",
            }), 409

        max_priority = max((q.priority_order for q in fresh_queues), default=0)
        has_open_progression = any(q.status in {"Active", "Dormant"} for q in fresh_queues)

        for employer_registration_id in employer_order:
            group_data = employer_groups[employer_registration_id]
            selected_jobs = group_data["jobs"]
            queue = existing_by_employer.get(employer_registration_id)

            if queue:
                for job_fair_job in selected_jobs:
                    db.session.add(JobFairJobGrouping(
                        queue_group_id=queue.queue_group_id,
                        job_fair_job_id=job_fair_job.job_fair_job_id,
                    ))
                changed_queues.append(queue)
                continue

            max_priority += 1
            employer_registration = group_data["employer_registration"]
            should_activate = not has_open_progression
            queue = JobFairQueue(
                job_fair_id=ongoing_job_fair.job_fair_id,
                job_fair_registration_id=registration.registration_id,
                employer_registration_id=employer_registration.registration_id,
                priority_order=max_priority,
                queue_number=get_next_queue_number(
                    ongoing_job_fair.job_fair_id,
                    employer_registration.registration_id,
                ),
                status="Active" if should_activate else "Dormant",
                queued_at=now,
                activated_at=now if should_activate else None,
            )
            db.session.add(queue)
            db.session.flush()
            newly_created_queue_ids.append(queue.queue_group_id)

            for job_fair_job in selected_jobs:
                db.session.add(JobFairJobGrouping(
                    queue_group_id=queue.queue_group_id,
                    job_fair_job_id=job_fair_job.job_fair_job_id,
                ))

            existing_by_employer[employer_registration_id] = queue
            changed_queues.append(queue)
            has_open_progression = True

        for queue in changed_queues:
            if queue.status != "Active" or queue.assigned_station_id is not None:
                continue
            station = (JobFairInterviewStation.query.filter_by(
                employer_registration_id=queue.employer_registration_id,
                station_number=1, is_revoked=False
            ).with_for_update().first())
            if station is None:
                station = JobFairInterviewStation(
                    job_fair_id=queue.job_fair_id,
                    employer_registration_id=queue.employer_registration_id,
                    station_number=1, is_revoked=False
                )
                db.session.add(station)
                db.session.flush()
            previously_served = JobFairQueue.query.filter(
                JobFairQueue.job_fair_id == queue.job_fair_id,
                JobFairQueue.employer_registration_id == queue.employer_registration_id,
                JobFairQueue.serving_started_at.isnot(None)
            ).first()
            station_busy = JobFairQueue.query.filter_by(
                assigned_station_id=station.station_id, status="Active"
            ).first()
            applicant_busy = JobFairQueue.query.filter(
                JobFairQueue.job_fair_id == queue.job_fair_id,
                JobFairQueue.job_fair_registration_id == queue.job_fair_registration_id,
                JobFairQueue.status == "Active",
                JobFairQueue.assigned_station_id.isnot(None)
            ).first()
            if previously_served or station_busy or applicant_busy:
                continue
            older_waiting = JobFairQueue.query.filter(
                JobFairQueue.job_fair_id == queue.job_fair_id,
                JobFairQueue.employer_registration_id == queue.employer_registration_id,
                JobFairQueue.status == "Active",
                JobFairQueue.assigned_station_id.is_(None),
                JobFairQueue.queue_group_id != queue.queue_group_id,
                or_(
                    JobFairQueue.queued_at < queue.queued_at,
                    and_(
                        JobFairQueue.queued_at == queue.queued_at,
                        JobFairQueue.queue_group_id < queue.queue_group_id,
                    ),
                ),
            ).first()
            if older_waiting:
                continue
            queue.assigned_station_id = station.station_id
            queue.serving_started_at = datetime.utcnow()

        db.session.commit()

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Failed to create or append kiosk queue.")
        return jsonify({"success": False, "message": "Unable to update your queue route."}), 500

    all_queues = (
        JobFairQueue.query
        .filter(
            JobFairQueue.job_fair_id == ongoing_job_fair.job_fair_id,
            JobFairQueue.job_fair_registration_id == registration.registration_id,
        )
        .order_by(JobFairQueue.priority_order.asc())
        .all()
    )

    print_queues = []
    for queue in all_queues:
        new_job_ids = new_job_ids_by_employer.get(queue.employer_registration_id)
        if not new_job_ids:
            continue
        queue_payload = build_queue_response([queue])[0]
        if queue.queue_group_id not in newly_created_queue_ids:
            queue_payload["jobs"] = [
                job for job in queue_payload.get("jobs", [])
                if job.get("job_fair_job_id") in new_job_ids
            ]
            if not queue_payload["jobs"]:
                continue
        print_queues.append(queue_payload)

    try:
        authorized_tickets = []
        for payload in print_queues:
            queue_id = int(payload["queue_group_id"])
            job_ids = sorted({int(job["job_fair_job_id"]) for job in payload.get("jobs", [])})
            if not job_ids:
                continue
            fingerprint = json.dumps([queue_id, job_ids], separators=(",", ":"))
            request_key = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
            existing_attempt = JobFairTicketPrintAttempt.query.filter_by(request_key=request_key).first()
            if existing_attempt is not None:
                current_app.logger.warning("Suppressed previously reserved kiosk ticket: queue=%s", queue_id)
                continue
            attempt = JobFairTicketPrintAttempt(
                request_key=request_key,
                queue_group_id=queue_id,
                job_ids_json=json.dumps(job_ids),
                status="Reserved",
                note="Authorized by kiosk; physical print not confirmed",
            )
            db.session.add(attempt)
            db.session.flush()
            payload["print_attempt_id"] = attempt.attempt_id
            payload["print_result_url"] = url_for("kiosk.record_ticket_print_result", attempt_id=attempt.attempt_id)
            authorized_tickets.append(payload)
        db.session.commit()
        print_queues = authorized_tickets
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Queue saved, but ticket print reservation failed")
        return jsonify({"success": False, "message": "Queue saved, but printing authorization could not be confirmed. Ask PESO staff; do not retry."}), 503

    return jsonify({
        "success": True,
        "already_queued": bool(existing_queues),
        "appended": bool(existing_queues),
        "no_changes": False,
        "message": "Your queue route has been updated." if existing_queues else "Your queue numbers have been created.",
        "queues": build_queue_response(all_queues),
        "print_queues": print_queues,
    }), 200 if existing_queues else 201


# ============================================================
# END REMAINING QUEUE PARTICIPATION
# ============================================================

@kiosk.route(
    "/queuing-number/<reference_id>/end-participation",
    methods=["POST"]
)
@login_required
def end_queue_participation(reference_id):
    """Cancel only the applicant's unfinished queues; preserve fair history."""

    kiosk_access_required()
    reference_id = reference_id.strip().upper()
    registration = JobFairRegistration.query.filter_by(reference_id=reference_id).first()
    if not registration:
        return jsonify({"success": False, "message": "Invalid registration."}), 404

    ongoing_job_fair = get_ongoing_job_fair()
    if not ongoing_job_fair or registration.job_fair_id != ongoing_job_fair.job_fair_id:
        return jsonify({"success": False, "message": "This registration is not for the current Job Fair."}), 403

    # Serialize kiosk cancellation with employer Call Next and kiosk auto-serving.
    # A station claim cannot occur between identifying and cancelling waiting queues.
    locked_fair = (db.session.query(JobFair)
        .filter(JobFair.job_fair_id == ongoing_job_fair.job_fair_id)
        .with_for_update().first())
    if locked_fair is None or locked_fair.status != "Ongoing":
        db.session.rollback()
        return jsonify({"success": False, "message": "The Job Fair is no longer ongoing."}), 409

    # Lock unfinished queues so withdrawal cannot race an employer action.
    # A live interview must be resolved by its station first; cancelling it
    # from the kiosk would otherwise leave the employer UI holding a queue
    # that has already become Cancelled.
    remaining = (
        JobFairQueue.query
        .filter(
            JobFairQueue.job_fair_id == ongoing_job_fair.job_fair_id,
            JobFairQueue.job_fair_registration_id == registration.registration_id,
            JobFairQueue.status.in_(["Active", "Dormant"]),
        )
        .order_by(JobFairQueue.priority_order.asc(), JobFairQueue.queue_group_id.asc())
        .with_for_update()
        .all()
    )

    # Optional single-employer cancellation. Existing requests without an ID
    # retain the original cancel-all-waiting behavior.
    payload = request.get_json(silent=True)
    if payload is not None and not isinstance(payload, dict):
        db.session.rollback()
        return jsonify({"success": False, "message": "Invalid cancellation request."}), 400
    selected_id = (payload or {}).get("queue_group_id")
    if selected_id is not None:
        if isinstance(selected_id, bool) or not isinstance(selected_id, int) or selected_id < 1:
            db.session.rollback()
            return jsonify({"success": False, "message": "Invalid employer queue selection."}), 400
        if not any(queue.queue_group_id == selected_id for queue in remaining):
            db.session.rollback()
            return jsonify({"success": False, "message": "Selected queue is not available for cancellation."}), 409

    # An applicant may withdraw from waiting/dormant employer visits while an
    # interview is in progress. Never cancel the queue owned by a station.
    cancellable = [
        queue for queue in remaining
        if queue.assigned_station_id is None and queue.serving_started_at is None
        and (selected_id is None or queue.queue_group_id == selected_id)
    ]

    if not cancellable:
        db.session.rollback()
        return jsonify({
            "success": False,
            "message": "This queue cannot be cancelled while being served." if selected_id is not None else "There are no waiting queues to cancel. Any ongoing interview remains active.",
            "queues": build_queue_response(
                JobFairQueue.query.filter_by(
                    job_fair_id=ongoing_job_fair.job_fair_id,
                    job_fair_registration_id=registration.registration_id,
                ).order_by(JobFairQueue.priority_order.asc()).all()
            ),
        }), 409

    try:
        for queue in cancellable:
            queue.status = "Cancelled"
            queue.cancellation_reason = "Applicant Withdrawn"
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Failed to end queue participation for registration %s.",
            registration.registration_id,
        )
        return jsonify({"success": False, "message": "Unable to end queue participation."}), 500

    all_queues = (
        JobFairQueue.query
        .filter_by(
            job_fair_id=ongoing_job_fair.job_fair_id,
            job_fair_registration_id=registration.registration_id,
        )
        .order_by(JobFairQueue.priority_order.asc())
        .all()
    )
    return jsonify({
        "success": True,
        "message": "Selected employer queue cancelled." if selected_id is not None else "Waiting queues cancelled. Ongoing interviews and completed visits were preserved.",
        "queues": build_queue_response(all_queues),
    }), 200


# ============================================================
# QUEUE CONFIRMATION PAGE
# ============================================================


# Kiosk-only, authenticated acknowledgement. Never claims physical printing.
@kiosk.route("/ticket-print-attempt/<int:attempt_id>/result", methods=["POST"])
@login_required
def record_ticket_print_result(attempt_id):
    if current_user.role == "kiosk":
        kiosk_access_required()
    elif current_user.role != "admin" or not getattr(current_user, "admin", None):
        abort(403)
    # Lock before checking status: simultaneous callbacks must not overwrite a
    # previously recorded outcome.
    attempt = (JobFairTicketPrintAttempt.query
               .filter_by(attempt_id=attempt_id).with_for_update().first())
    if attempt is None:
        return jsonify({"success": False, "message": "Print attempt not found."}), 404

    # A kiosk may acknowledge only tickets from its currently assigned fair.
    # A valid attempt ID alone must not permit cross-event status changes.
    queue = JobFairQueue.query.filter_by(queue_group_id=attempt.queue_group_id).first()
    is_staff_reprint = (attempt.note or "").startswith("Staff reprint authorized by admin user ")
    if current_user.role == "kiosk":
        # Staff-authorized reprint outcomes must come from a staff session,
        # even if this kiosk belongs to the same job fair.
        if is_staff_reprint:
            return jsonify({"success": False, "message": "Staff reprint results require an administrator."}), 403
        if (not queue or current_user.kiosk.job_fair_id is None
                or queue.job_fair_id != current_user.kiosk.job_fair_id):
            return jsonify({"success": False, "message": "Print attempt is not assigned to this kiosk."}), 403
    else:
        # Administrators may report outcomes only for staff reprints.
        if not queue or not is_staff_reprint:
            abort(403)

    data = request.get_json(silent=True) or {}
    requested_status = data.get("status")
    if requested_status not in {"Acknowledged", "Uncertain", "Failed"}:
        return jsonify({"success": False, "message": "Invalid print status."}), 400
    if attempt.status != "Reserved":
        return jsonify({"success": True, "status": attempt.status, "already_recorded": True}), 200
    attempt.status = requested_status
    outcome_note = {
        "Acknowledged": "QZ Tray accepted the dispatch; physical print not verified",
        "Uncertain": "Printer response uncertain; manual staff review required",
        "Failed": "Client reported failure; staff review required before retry",
    }[requested_status]
    # Staff reprints carry the approving admin and reason in note. Never erase
    # that audit trail when the printer callback reports its outcome.
    if (attempt.note or "").startswith("Staff reprint authorized by admin user "):
        attempt.note = (attempt.note + " | " + outcome_note)[:255]
    else:
        attempt.note = outcome_note
    db.session.commit()
    return jsonify({"success": True, "status": attempt.status}), 200



# Staff-only manual reprint authorization. This endpoint does NOT dispatch to a
# printer: the staff workstation must explicitly send the returned ticket to QZ.
@kiosk.route("/staff/ticket-print-attempt/<int:attempt_id>/authorize-reprint", methods=["POST"])
@login_required
def authorize_ticket_reprint(attempt_id):
    if current_user.role != "admin" or not getattr(current_user, "admin", None):
        abort(403)

    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason") or "").strip()
    if len(reason) < 10 or len(reason) > 200:
        return jsonify({"success": False, "message": "A reprint reason of 10–200 characters is required."}), 400

    # Lock the parent queue row to serialize competing staff authorizations.
    # The same lock is acquired by every request to this endpoint before
    # inspecting outstanding print attempts.
    original = JobFairTicketPrintAttempt.query.filter_by(attempt_id=attempt_id).first()
    if original is None:
        return jsonify({"success": False, "message": "Print attempt not found."}), 404

    queue = (JobFairQueue.query
             .filter_by(queue_group_id=original.queue_group_id)
             .with_for_update().first())
    if queue is None:
        return jsonify({"success": False, "message": "Queue no longer exists."}), 404
    if original.status in ("Reserved", "Uncertain"):
        db.session.rollback()
        return jsonify({"success": False, "message": "Original attempt is unresolved; investigate before reprinting."}), 409

    try:
        job_ids = json.loads(original.job_ids_json)
        job_ids = sorted({int(job_id) for job_id in job_ids})
    except (ValueError, TypeError):
        return jsonify({"success": False, "message": "Original ticket data is invalid."}), 409
    if not job_ids:
        return jsonify({"success": False, "message": "Original ticket has no jobs."}), 409

    # Check the complete history for this exact ticket, not just the selected
    # attempt. A prior replacement may already be awaiting investigation.
    for other in JobFairTicketPrintAttempt.query.filter_by(queue_group_id=queue.queue_group_id).all():
        if other.status not in ("Reserved", "Uncertain"):
            continue
        try:
            other_jobs = {int(value) for value in json.loads(other.job_ids_json)}
        except (TypeError, ValueError):
            db.session.rollback()
            return jsonify({"success": False, "message": "An unresolved ticket has invalid job data; staff review required."}), 409
        if other_jobs == set(job_ids):
            db.session.rollback()
            return jsonify({"success": False, "message": "An outstanding print attempt already exists for this ticket. Review it before authorizing another."}), 409

    # Rebuild from current queue, but restrict lines to those on the original
    # ticket. Never add later jobs to an old ticket reprint.
    payload = build_queue_response([queue])[0]
    payload["jobs"] = [j for j in payload.get("jobs", []) if int(j["job_fair_job_id"]) in job_ids]
    if len({int(j["job_fair_job_id"]) for j in payload["jobs"]}) != len(job_ids):
        return jsonify({"success": False, "message": "Some original ticket jobs are no longer available."}), 409

    # Random request key allows a *new* explicitly authorized attempt while
    # preserving the unique fingerprint of the original kiosk reservation.
    new_attempt = JobFairTicketPrintAttempt(
        request_key=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
        queue_group_id=queue.queue_group_id,
        job_ids_json=json.dumps(job_ids),
        status="Reserved",
        note=f"Staff reprint authorized by admin user {current_user.user_id}; reason: {reason}"[:255],
    )
    db.session.add(new_attempt)
    db.session.flush()
    db.session.add(AuditLog(
        user_id=current_user.user_id,
        action=f"JOB_FAIR_TICKET_REPRINT_AUTHORIZED original={original.attempt_id} new={new_attempt.attempt_id} queue={queue.queue_group_id}"[:255],
        ip_address=(request.remote_addr or "")[:45],
    ))
    db.session.commit()
    payload["print_attempt_id"] = new_attempt.attempt_id
    payload["reference_id"] = queue.job_fair_registration.reference_id
    payload["print_result_url"] = url_for("kiosk.record_ticket_print_result", attempt_id=new_attempt.attempt_id)
    return jsonify({"success": True, "ticket": payload, "requires_staff_print_dispatch": True}), 201

@kiosk.route(
    "/queuing-confirmation/<reference_id>"
)
@login_required
def queuing_confirmation(reference_id):

    kiosk_access_required()

    reference_id = reference_id.strip().upper()

    registration = (
        JobFairRegistration.query
        .filter_by(
            reference_id=reference_id
        )
        .first()
    )

    if not registration:

        flash(
            "Invalid or expired reference number.",
            "error"
        )

        return redirect(
            url_for(
                "kiosk.registration"
            )
        )

    ongoing_job_fair = get_ongoing_job_fair()

    if not ongoing_job_fair:

        flash(
            "There is currently no ongoing job fair.",
            "error"
        )

        return redirect(
            url_for(
                "kiosk.registration"
            )
        )

    if (
        registration.job_fair_id
        != ongoing_job_fair.job_fair_id
    ):

        flash(
            "Your registration is not for the current job fair.",
            "error"
        )

        return redirect(
            url_for(
                "kiosk.registration"
            )
        )

    queues = (
        JobFairQueue.query
        .filter(
            JobFairQueue.job_fair_id
            == ongoing_job_fair.job_fair_id,

            JobFairQueue.job_fair_registration_id
            == registration.registration_id
        )
        .order_by(
            JobFairQueue.priority_order.asc()
        )
        .all()
    )

    if not queues:

        flash(
            "No queue numbers were found for this registration.",
            "error"
        )

        return redirect(
            url_for(
                "kiosk.queuing_number",
                reference_id=reference_id
            )
        )

    queue_data = build_queue_response(
        queues
    )

    return render_template(
        "kiosk/QueueConfirmationKiosk.html",

        existing_queue_view=(request.args.get("existing_queue") == "1"),

        registration=registration,

        job_seeker=registration.job_seeker,

        ongoing_job_fair=ongoing_job_fair,

        queues=queues,

        queue_data=queue_data
    )


# ============================================================
# BUILD QUEUE RESPONSE
# ============================================================

def build_queue_response(queues):
    """
    Convert JobFairQueue records into JSON-safe data.

    IMPORTANT:

    The database continues storing:

        queue_number = 1

    But the API now exposes:

        numeric_queue_number = 1
        queue_number = "A01-001"
        queue_reference = "A01-001"

    This means existing frontend code using:

        queue.queue_number

    will now display:

        A01-001

    instead of:

        1
    """

    queue_data = []

    for queue in queues:

        employer_registration = (
            queue.employer_registration
        )

        employer = None

        if employer_registration:
            employer = (
                employer_registration.employer
            )

        booth_number = None

        if employer_registration:
            booth_number = (
                employer_registration.booth_number
            )

        queue_reference = get_queue_reference(
            queue
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

        jobs = []

        for grouping in groupings:

            job_fair_job = (
                grouping.job_fair_job
            )

            if not job_fair_job:
                continue

            job = job_fair_job.job

            if not job:
                continue

            jobs.append({

                "grouping_id":
                    grouping.grouping_id,

                "job_fair_job_id":
                    job_fair_job.job_fair_job_id,

                "job_title":
                    job.job_title

            })

        queue_data.append({

            "queue_group_id":
                queue.queue_group_id,

            # =================================================
            # USER-FACING QUEUE NUMBER
            # =================================================
            #
            # IMPORTANT:
            # This is intentionally the formatted value.
            #
            # Existing frontend code can safely use:
            #
            # queue.queue_number
            #
            # and receive:
            #
            # A01-001
            # =================================================

            "queue_number":
                queue_reference,

            # =================================================
            # INTERNAL NUMERIC NUMBER
            # =================================================

            "numeric_queue_number":
                queue.queue_number,

            # =================================================
            # EXPLICIT DISPLAY REFERENCE
            # =================================================

            "queue_reference":
                queue_reference,

            # =================================================
            # BOOTH
            # =================================================

            "booth_number":
                booth_number,

            # =================================================
            # APPLICANT VISIT ORDER
            # =================================================

            "priority_order":
                queue.priority_order,

            # =================================================
            # STATUS
            # =================================================

            "group_status":
                queue.status,

            "cancellation_reason":
                queue.cancellation_reason,

            # =================================================
            # COMPANY
            # =================================================

            "company":
                employer.company_name
                if employer
                else "Unknown Employer",

            # =================================================
            # EMPLOYER REGISTRATION
            # =================================================

            "employer_registration_id":
                queue.employer_registration_id,

            # =================================================
            # JOBS
            # =================================================

            "jobs":
                jobs

        })

    queue_data.sort(
        key=lambda item: (
            item["priority_order"]
            if item["priority_order"] is not None
            else 999999
        )
    )

    return queue_data


# ============================================================
# GET NEXT QUEUE NUMBER
# ============================================================

def get_next_queue_number(
    job_fair_id,
    employer_registration_id
):
    """
    Get the next numeric queue number for an employer/booth
    within a specific job fair.

    IMPORTANT:

    This function intentionally returns an INTEGER because
    JobFairQueue.queue_number is an integer database column.

    Example:

        A01:
            1
            2
            3

        A02:
            1
            2

    The user-facing reference is created separately:

        A01-001
        A01-002
        A01-003

        A02-001
        A02-002
    """

    last_queue = (
        JobFairQueue.query
        .filter(
            JobFairQueue.job_fair_id
            == job_fair_id,

            JobFairQueue.employer_registration_id
            == employer_registration_id
        )
        .order_by(
            JobFairQueue.queue_number.desc()
        )
        .first()
    )

    if not last_queue:
        return 1

    try:
        return int(
            last_queue.queue_number
        ) + 1

    except (
        TypeError,
        ValueError
    ):
        return 1

# Administrator-reviewed resolution for an interrupted kiosk/staff print attempt.
# This records a verified outcome; it never dispatches or creates a ticket.
@kiosk.route("/staff/ticket-print-attempt/<int:attempt_id>/resolve", methods=["POST"])
@login_required
def resolve_ticket_print_attempt(attempt_id):
    if current_user.role != "admin" or not getattr(current_user, "admin", None):
        abort(403)
    data = request.get_json(silent=True) or {}
    outcome = str(data.get("outcome") or "").strip()
    reason = str(data.get("reason") or "").strip()
    if outcome not in ("Acknowledged", "Failed"):
        return jsonify({"success": False, "message": "Select a verified printing outcome."}), 400
    if not isinstance(data.get("verified"), bool) or not data["verified"]:
        return jsonify({"success": False, "message": "Confirm that staff investigated the physical ticket outcome."}), 400
    if not 20 <= len(reason) <= 200:
        return jsonify({"success": False, "message": "Provide a review explanation of 20–200 characters."}), 400

    attempt = (JobFairTicketPrintAttempt.query
               .filter_by(attempt_id=attempt_id).with_for_update().first())
    if attempt is None:
        return jsonify({"success": False, "message": "Print attempt not found."}), 404
    if attempt.status not in ("Reserved", "Uncertain"):
        db.session.rollback()
        return jsonify({"success": False, "message": "This attempt has already been resolved. Refresh the page."}), 409
    queue = JobFairQueue.query.filter_by(queue_group_id=attempt.queue_group_id).first()
    if queue is None:
        db.session.rollback()
        return jsonify({"success": False, "message": "Associated queue not found."}), 409

    # Keep the original authorization/reason for staff reprints, if any.
    old_note = attempt.note or ""
    resolution = (f"Staff review admin={current_user.user_id} outcome={outcome}: {reason}")
    attempt.note = (old_note + " | " + resolution if old_note else resolution)[:255]
    attempt.status = outcome
    db.session.add(AuditLog(
        user_id=current_user.user_id,
        action=(f"JOB_FAIR_PRINT_ATTEMPT_REVIEWED attempt={attempt_id} "
                f"queue={queue.queue_group_id} outcome={outcome} reason={reason}")[:255],
        ip_address=(request.remote_addr or "")[:45],
    ))
    db.session.commit()
    return jsonify({"success": True, "status": outcome, "message": "Review recorded. Refresh to authorize a replacement if appropriate."}), 200


@kiosk.route("/staff/ticket-reprints", methods=["GET"])
@login_required
def staff_ticket_reprints():
    if current_user.role != "admin" or not getattr(current_user, "admin", None):
        abort(403)
    attempts = (JobFairTicketPrintAttempt.query
                .order_by(JobFairTicketPrintAttempt.attempt_id.desc())
                .limit(100).all())
    rows = []
    for attempt in attempts:
        queue = JobFairQueue.query.filter_by(queue_group_id=attempt.queue_group_id).first()
        if not queue:
            continue
        rows.append({"attempt": attempt, "queue": queue,
                     "reference": queue.job_fair_registration.reference_id if queue.job_fair_registration else "—"})
    return render_template("admin/staff_ticket_reprints.html", rows=rows)


@kiosk.route("/staff/ticket-print-attempt/<int:attempt_id>/begin", methods=["POST"])
@login_required
def begin_staff_ticket_reprint(attempt_id):
    if current_user.role != "admin" or not getattr(current_user, "admin", None):
        abort(403)
    attempt = JobFairTicketPrintAttempt.query.filter_by(attempt_id=attempt_id).with_for_update().first()
    if attempt is None:
        abort(404)
    if not (attempt.note or "").startswith("Staff reprint authorized by admin user "):
        abort(403)
    if attempt.status != "Reserved":
        return jsonify({"success": False, "message": "Ticket dispatch already started or resolved."}), 409
    attempt.status = "Uncertain"
    db.session.commit()
    return jsonify({"success": True}), 200


@kiosk.route("/staff/ticket-print-attempt/<int:attempt_id>/finalize", methods=["POST"])
@login_required
def finalize_staff_ticket_reprint(attempt_id):
    if current_user.role != "admin" or not getattr(current_user, "admin", None):
        abort(403)
    attempt = JobFairTicketPrintAttempt.query.filter_by(attempt_id=attempt_id).with_for_update().first()
    if attempt is None:
        abort(404)
    if not (attempt.note or "").startswith("Staff reprint authorized by admin user "):
        abort(403)
    if attempt.status != "Uncertain":
        return jsonify({"success": False, "message": "Print attempt is not pending resolution."}), 409
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in ("Acknowledged", "Failed"):
        return jsonify({"success": False, "message": "Invalid result."}), 400
    attempt.status = status
    # Preserve staff authorization metadata in attempt.note.
    db.session.commit()
    return jsonify({"success": True, "status": status}), 200
