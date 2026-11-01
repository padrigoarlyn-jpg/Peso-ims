from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from website.extensions import db
from website.models import Notification, User
from website.utils.email import send_email
from flask import current_app


ALLOWED_NOTIFICATION_TYPES = {
    "Application",
    "Referral",
    "Job",
    "Job Fair",
    "Announcement",
    "Employer",
    "Employer Registration",
    "System",
}


def _validate_notification_type(notification_type):
    if notification_type not in ALLOWED_NOTIFICATION_TYPES:
        raise ValueError(f"Unsupported notification type: {notification_type}")


def notify_user(user_id, title, message, notification_type):
    _validate_notification_type(notification_type)
    db.session.add(
        Notification(
            user_id=user_id,
            title=title,
            message=message,
            notification_type=notification_type,
        )
    )


def commit_notifications(context="notification delivery"):
    """Commit notifications that were intentionally created after a main action commit.

    Returns True on success. A notification failure never reverses the already
    completed business action.
    """
    try:
        db.session.commit()
        return True
    except Exception as error:
        db.session.rollback()
        current_app.logger.exception(
            "Unable to persist %s: %s",
            context,
            error,
        )
        return False


def notify_role(role, title, message, notification_type):
    for user in User.query.filter_by(
        role=role,
        is_deleted=False,
        status="active",
    ).all():
        notify_user(user.user_id, title, message, notification_type)


def notify_admins(title, message, notification_type):
    """Send operational notifications to every active admin account."""
    notify_role("admin", title, message, notification_type)


def create_upcoming_interview_reminders():
    """Persist local-time interview reminders before sending optional email."""
    from website.models import Application

    # Interview dates and times are stored as Philippine wall-clock values.
    now = datetime.now(ZoneInfo("Asia/Manila")).replace(tzinfo=None)
    reminder_limit = now + timedelta(hours=24)
    pending_emails = []

    applications = Application.query.filter_by(status="Interview").all()

    for application in applications:
        if not application.interview_date or not application.interview_time:
            continue

        interview_at = datetime.combine(
            application.interview_date, application.interview_time
        )
        if not now <= interview_at <= reminder_limit:
            continue

        job_title = application.job.job_title if application.job else "your application"
        interview_label = interview_at.strftime("%B %d, %Y at %I:%M %p")
        message = (
            f"Reminder: the interview for {job_title} is scheduled for "
            f"{interview_label}. Application #{application.application_id}."
        )

        participant_ids = [application.job_seeker.user_id]
        if application.job and application.job.employer:
            participant_ids.append(application.job.employer.user_id)

        for user_id in set(participant_ids):
            already_sent = Notification.query.filter_by(
                user_id=user_id,
                title="Interview Reminder",
                message=message,
            ).first()
            if already_sent:
                continue

            notify_user(user_id, "Interview Reminder", message, "Application")
            user = db.session.get(User, user_id)
            if user and user.email:
                pending_emails.append((user.email, message))

    if not pending_emails and not db.session.new:
        return

    # An email should never be sent for a reminder that failed to persist.
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Unable to persist interview reminders")
        return

    for recipient, message in pending_emails:
        try:
            send_email(recipient, "Interview Reminder - PESO IMS", message)
        except Exception:
            current_app.logger.exception(
                "Unable to queue interview reminder email for %s", recipient
            )
