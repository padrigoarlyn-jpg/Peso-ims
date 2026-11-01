"""
Shared lifecycle helpers for Job Fair-exclusive jobs.

These helpers do not commit transactions. Routes remain
responsible for db.session.commit()/rollback().
"""


def get_job_fair_entry_for_job(job):
    """
    Return the first JobFairJob link for a Job Fair-exclusive
    job, or None when the job is not linked to a registration.
    """
    entries = getattr(job, "job_fair_entries", None) or []
    return entries[0] if entries else None


def get_job_fair_registration_for_job(job):
    entry = get_job_fair_entry_for_job(job)
    if entry is None:
        return None
    return getattr(entry, "registration", None)


def get_job_fair_for_job(job):
    registration = get_job_fair_registration_for_job(job)
    if registration is None:
        return None
    return getattr(registration, "job_fair", None)


def iter_exclusive_jobs_for_registration(registration):
    for entry in getattr(registration, "selected_jobs", None) or []:
        job = getattr(entry, "job", None)

        if (
            job is not None
            and getattr(job, "job_type", None) == "Job Fair"
        ):
            yield job


def iter_exclusive_jobs_for_fair(job_fair):
    seen_job_ids = set()

    for registration in (
        getattr(job_fair, "employer_registrations", None)
        or []
    ):
        for job in iter_exclusive_jobs_for_registration(
            registration
        ):
            job_id = getattr(job, "job_id", None)

            if job_id in seen_job_ids:
                continue

            seen_job_ids.add(job_id)
            yield job


def sync_exclusive_job_deadlines(job_fair):
    """
    Keep all Job Fair-exclusive deadlines aligned with the
    event end date.
    """
    if not getattr(job_fair, "end_datetime", None):
        return 0

    deadline = job_fair.end_datetime.date()
    count = 0

    for job in iter_exclusive_jobs_for_fair(job_fair):
        job.deadline = deadline
        count += 1

    return count


def close_exclusive_jobs_for_registration(
    registration,
    reason=None,
):
    """
    Close all exclusive jobs belonging to one employer
    registration. Approval status is intentionally preserved
    for audit/history.
    """
    count = 0

    for job in iter_exclusive_jobs_for_registration(
        registration
    ):
        job.status = "Closed"

        if reason and not getattr(job, "remarks", None):
            job.remarks = reason

        count += 1

    return count


def close_exclusive_jobs_for_fair(
    job_fair,
    reason=None,
):
    """
    Close every exclusive job attached to a finished Job Fair.
    Approval status is preserved.
    """
    count = 0

    for job in iter_exclusive_jobs_for_fair(job_fair):
        job.status = "Closed"

        if reason and not getattr(job, "remarks", None):
            job.remarks = reason

        count += 1

    return count


# ============================================================
# PHASE 1D: REGISTRATION / QUEUE-ENTRY AVAILABILITY
# ============================================================

def _local_now_naive():
    """Return Philippine local wall-clock time as a naive datetime.

    Job Fair schedule columns in the current PESO-IMS schema are stored as
    naive local datetimes, so comparisons must use the same representation.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Manila")).replace(tzinfo=None)


def is_past_scheduled_end(job_fair, now=None):
    now = now or _local_now_naive()
    end_datetime = getattr(job_fair, "end_datetime", None)
    return bool(end_datetime and now >= end_datetime)


def is_registration_open(job_fair, now=None):
    now = now or _local_now_naive()
    if getattr(job_fair, "status", None) == "Finished":
        return False
    if getattr(job_fair, "registration_manually_closed", False):
        return False
    deadline = getattr(job_fair, "registration_deadline", None)
    if deadline and now > deadline:
        return False
    if is_past_scheduled_end(job_fair, now):
        return False
    return getattr(job_fair, "status", None) in {"Upcoming", "Ongoing"}


def is_queue_entry_open(job_fair, now=None):
    now = now or _local_now_naive()
    if getattr(job_fair, "status", None) != "Ongoing":
        return False
    if getattr(job_fair, "queue_entry_manually_closed", False):
        return False
    deadline = getattr(job_fair, "queue_entry_deadline", None)
    if deadline and now > deadline:
        return False
    if is_past_scheduled_end(job_fair, now):
        return False
    return True
