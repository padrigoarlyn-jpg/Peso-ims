"""
Central Application status rules for PESO-IMS.

Application status is separate from Referral status and from
Job Fair evaluation/queue status.

Application workflow:
    Pending -> Reviewed
    Pending/Reviewed -> Interview
    Pending/Reviewed/Interview -> Rejected
    Interview -> Accepted           (PESO-referred)
    Pending/Reviewed/Interview -> Accepted
                                      (Job Fair-evaluated direct)

Accepted and Rejected are terminal states.
"""

from datetime import date, datetime


STATUS_PENDING = "Pending"
STATUS_REVIEWED = "Reviewed"
STATUS_INTERVIEW = "Interview"
STATUS_ACCEPTED = "Accepted"
STATUS_REJECTED = "Rejected"

VALID_APPLICATION_STATUSES = {
    STATUS_PENDING,
    STATUS_REVIEWED,
    STATUS_INTERVIEW,
    STATUS_ACCEPTED,
    STATUS_REJECTED,
}

TERMINAL_APPLICATION_STATUSES = {
    STATUS_ACCEPTED,
    STATUS_REJECTED,
}

# General transition rules.
ALLOWED_TRANSITIONS = {
    STATUS_PENDING: {
        STATUS_REVIEWED,
        STATUS_INTERVIEW,
        STATUS_REJECTED,
    },
    STATUS_REVIEWED: {
        STATUS_INTERVIEW,
        STATUS_REJECTED,
    },
    STATUS_INTERVIEW: {
        STATUS_ACCEPTED,
        STATUS_REJECTED,
    },
    STATUS_ACCEPTED: set(),
    STATUS_REJECTED: set(),
}


def normalize_application_status(value):
    value = (value or STATUS_PENDING).strip()

    if value not in VALID_APPLICATION_STATUSES:
        return STATUS_PENDING

    return value


def validate_application_transition(
    application,
    target_status,
    *,
    allow_same=False,
):
    """
    Validate a generic Application status transition.

    This function does not modify the Application.
    """
    if application is None:
        return False, "Application not found."

    current_status = normalize_application_status(
        application.status
    )

    if target_status not in VALID_APPLICATION_STATUSES:
        return False, "Invalid application status."

    if current_status == target_status:
        if allow_same:
            return True, None

        return (
            False,
            f"This application is already {target_status.lower()}.",
        )

    if current_status in TERMINAL_APPLICATION_STATUSES:
        return (
            False,
            (
                f"This application is already "
                f"{current_status.lower()} and can no longer "
                "change status."
            ),
        )

    allowed = ALLOWED_TRANSITIONS.get(
        current_status,
        set(),
    )

    if target_status not in allowed:
        return (
            False,
            (
                f"Application status cannot move from "
                f"{current_status} to {target_status}."
            ),
        )

    return True, None


def mark_application_reviewed(application):
    """
    Used when PESO officially issues a referral.

    Pending -> Reviewed.
    Reviewed/Interview/terminal states are left untouched rather
    than moved backward.
    """
    if application is None:
        return False, "Application not found."

    current_status = normalize_application_status(
        application.status
    )

    if current_status == STATUS_PENDING:
        application.status = STATUS_REVIEWED
        return True, None

    return True, None


def mark_application_interview(application):
    """
    Schedule or reschedule an interview.

    Pending -> Interview
    Reviewed -> Interview
    Interview -> Interview (reschedule)
    """
    valid, reason = validate_application_transition(
        application,
        STATUS_INTERVIEW,
        allow_same=True,
    )

    if not valid:
        return False, reason

    application.status = STATUS_INTERVIEW

    # Any previous terminal timestamps should not exist for
    # a valid nonterminal transition, but clear them defensively.
    application.hired_at = None
    application.rejected_at = None

    return True, None


def mark_application_rejected(
    application,
    *,
    rejected_at=None,
):
    """
    Pending/Reviewed/Interview -> Rejected.
    Accepted and Rejected are terminal.
    """
    valid, reason = validate_application_transition(
        application,
        STATUS_REJECTED,
    )

    if not valid:
        return False, reason

    application.status = STATUS_REJECTED
    application.rejected_at = (
        rejected_at or datetime.utcnow()
    )
    application.hired_at = None

    return True, None


def validate_hire_transition(
    application,
    *,
    is_referred=False,
    is_job_fair_direct=False,
):
    """
    Validate whether employer hiring may proceed.

    PESO-referred:
        must already be Interview and the scheduled interview
        date cannot still be in the future.

    Job Fair direct:
        may be accepted from Pending, Reviewed, or Interview
        because the Job Fair candidate evaluation is the
        employer-side screening event.
    """
    if application is None:
        return False, "Application not found."

    current_status = normalize_application_status(
        application.status
    )

    if current_status == STATUS_ACCEPTED:
        return False, "This applicant has already been hired."

    if current_status == STATUS_REJECTED:
        return False, "This applicant has already been rejected."

    if is_referred:
        if current_status != STATUS_INTERVIEW:
            return (
                False,
                "Referred applicants must be interviewed first.",
            )

        interview_date = getattr(
            application,
            "interview_date",
            None,
        )

        if (
            interview_date is not None
            and interview_date > date.today()
        ):
            return (
                False,
                "The interview has not happened yet.",
            )

        return True, None

    if is_job_fair_direct:
        if current_status not in {
            STATUS_PENDING,
            STATUS_REVIEWED,
            STATUS_INTERVIEW,
        }:
            return (
                False,
                "This Job Fair application cannot be hired from its current status.",
            )

        return True, None

    return (
        False,
        (
            "This applicant is not eligible for employer hiring "
            "because there is no official PESO referral or "
            "Job Fair evaluation."
        ),
    )


def mark_application_accepted(
    application,
    *,
    hired_at=None,
):
    """
    Final accepted/hired state. Caller must perform source,
    interview, authorization, and vacancy checks first.
    """
    if application is None:
        return False, "Application not found."

    current_status = normalize_application_status(
        application.status
    )

    if current_status == STATUS_ACCEPTED:
        return False, "This applicant has already been hired."

    if current_status == STATUS_REJECTED:
        return False, "This applicant has already been rejected."

    application.status = STATUS_ACCEPTED
    application.hired_at = (
        hired_at or datetime.utcnow()
    )
    application.rejected_at = None

    # A confirmed hire must also be reflected in the job seeker's
    # current employment status.  This function is shared by the
    # regular/referral and Job Fair hiring workflows, so keeping the
    # update here prevents the profile from becoming stale.
    if getattr(application, "job_seeker", None) is not None:
        application.job_seeker.employment_status = "employed"

    return True, None


def sync_jobseeker_employment_status(job_seeker):
    """
    Repair legacy/stale profiles that were hired before employment
    status synchronization was introduced.

    This helper promotes a seeker to employed only when an active accepted
    application exists. It deliberately does not infer unemployment from the
    absence of an active PESO-IMS hire: outside employment may exist.

    Returns True when the JobSeeker row was changed.
    """
    if job_seeker is None:
        return False

    # Local import avoids coupling model imports to this state module at
    # application import time.
    from website.models import Application

    has_confirmed_hire = (
        Application.query
        .filter(
            Application.job_seeker_id == job_seeker.job_seeker_id,
            Application.status == STATUS_ACCEPTED,
            Application.employment_ended_at.is_(None),
        )
        .first()
        is not None
    )

    if (
        has_confirmed_hire
        and (job_seeker.employment_status or "").strip().lower()
        != "employed"
    ):
        job_seeker.employment_status = "employed"
        return True

    return False
