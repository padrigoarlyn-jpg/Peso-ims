from datetime import datetime, timezone

from flask import current_app

from website.models import JobFairQueue


# ============================================================
# ACTIVATE NEXT DORMANT QUEUE
# ============================================================

def activate_next_dormant_queue(current_queue):
    """
    Finish the current active queue and activate the next
    dormant queue belonging to the SAME APPLICANT in the
    SAME JOB FAIR.

    The next queue is determined by priority_order.

    IMPORTANT:
    The next queue may belong to a DIFFERENT employer.

    Example:

        Employer A -> Employer B -> Employer C

    This helper allows:

        A queue -> B queue -> C queue

    while preserving the applicant's original priority.

    This function does NOT commit.
    The caller is responsible for committing.
    """

    if not current_queue:
        return None

    if current_queue.status != "Active":
        return None

    now = datetime.now(timezone.utc)

    # ========================================================
    # FIND NEXT DORMANT QUEUE
    # ========================================================
    #
    # Same:
    #
    #   Job Fair
    #   Applicant
    #
    # Different:
    #
    #   Employer is allowed to change
    #
    # Higher priority:
    #
    #   current = 1
    #   next    = 2
    #
    # ========================================================

    next_queue = (
        JobFairQueue.query
        .filter(

            JobFairQueue.job_fair_id
            == current_queue.job_fair_id,

            JobFairQueue.job_fair_registration_id
            == current_queue.job_fair_registration_id,

            JobFairQueue.priority_order
            > current_queue.priority_order,

            JobFairQueue.status
            == "Dormant"

        )
        .order_by(

            JobFairQueue.priority_order.asc(),

            JobFairQueue.queued_at.asc(),

            JobFairQueue.queue_group_id.asc()

        )
        .first()
    )

    # ========================================================
    # FINISH CURRENT QUEUE
    # ========================================================

    current_queue.status = "Finished"

    current_queue.finished_at = now

    # ========================================================
    # ACTIVATE NEXT QUEUE
    # ========================================================

    if next_queue:

        next_queue.status = "Active"

        next_queue.activated_at = now

    # ========================================================
    # LOG RESULT
    # ========================================================

    if next_queue:

        current_app.logger.info(
            "Activated next applicant queue: "
            "finished_queue=%s, "
            "next_queue=%s, "
            "job_fair_registration_id=%s, "
            "current_employer_registration_id=%s, "
            "next_employer_registration_id=%s, "
            "current_priority=%s, "
            "next_priority=%s",

            current_queue.queue_group_id,

            next_queue.queue_group_id,

            current_queue.job_fair_registration_id,

            current_queue.employer_registration_id,

            next_queue.employer_registration_id,

            current_queue.priority_order,

            next_queue.priority_order
        )

    else:

        current_app.logger.info(
            "No next dormant queue found for applicant: "
            "finished_queue=%s, "
            "job_fair_registration_id=%s, "
            "priority=%s",

            current_queue.queue_group_id,

            current_queue.job_fair_registration_id,

            current_queue.priority_order
        )

    return next_queue

def activate_next_after_cancelled_queue(current_queue):
    """Activate the next Dormant queue after the current visit was cancelled.

    Unlike ``activate_next_dormant_queue``, this helper deliberately preserves
    the current queue's Cancelled status and cancellation reason.  It does not
    commit; the caller owns the transaction.
    """
    if not current_queue or current_queue.status != "Cancelled":
        return None

    now = datetime.now(timezone.utc)
    next_queue = (
        JobFairQueue.query
        .filter(
            JobFairQueue.job_fair_id == current_queue.job_fair_id,
            JobFairQueue.job_fair_registration_id == current_queue.job_fair_registration_id,
            JobFairQueue.priority_order > current_queue.priority_order,
            JobFairQueue.status == "Dormant",
        )
        .order_by(
            JobFairQueue.priority_order.asc(),
            JobFairQueue.queued_at.asc(),
            JobFairQueue.queue_group_id.asc(),
        )
        .first()
    )

    if next_queue:
        next_queue.status = "Active"
        next_queue.activated_at = now
        current_app.logger.info(
            "Activated next applicant queue after cancellation: cancelled_queue=%s, next_queue=%s, reason=%s",
            current_queue.queue_group_id,
            next_queue.queue_group_id,
            current_queue.cancellation_reason,
        )

    return next_queue

