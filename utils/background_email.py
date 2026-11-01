"""Small background email helper for non-critical bulk notifications.

This keeps bulk SMTP delivery out of the request that published the Job Fair.
For a larger production deployment, replace this in-process executor with a
durable job queue such as RQ or Celery.
"""

from concurrent.futures import ThreadPoolExecutor

from flask_mail import Message

from website.extensions import mail
from website.utils.email_branding import apply_peso_branding
from website.utils.email import is_deliverable_email


# Keep the worker count deliberately small so bulk mail does not overwhelm the
# local SMTP connection or compete heavily with web requests.
_email_executor = ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="peso-email",
)


def _send_bulk_email(app, recipients, subject, body, raise_errors=False):
    """Send a batch using one reusable Flask-Mail connection."""

    with app.app_context():
        try:
            failed_recipients = []
            with mail.connect() as connection:
                for recipient in recipients:
                    try:
                        message = Message(
                            subject=subject,
                            recipients=[recipient],
                            body=body,
                        )
                        apply_peso_branding(message, body, app)
                        # Diagnostics: verify the outgoing message includes the
                        # HTML alternative and the inline PESO header before SMTP.
                        if not message.html or not any(
                            attachment.headers.get("Content-ID") == "<peso-email-header>"
                            for attachment in message.attachments
                        ):
                            raise RuntimeError("PESO email branding missing before SMTP delivery")
                        connection.send(message)
                    except Exception as error:
                        failed_recipients.append(recipient)
                        app.logger.exception(
                            "Bulk email failed for %s: %s",
                            recipient,
                            error,
                        )
            # RQ must see partial failures as failures, rather than marking a
            # partially delivered batch successful. Do not retry here: a
            # transport failure may have happened after SMTP accepted mail.
            if failed_recipients and raise_errors:
                raise RuntimeError(
                    f"Bulk email failed for {len(failed_recipients)} recipient(s)"
                )
        except Exception as error:
            app.logger.exception("Unable to open bulk email connection: %s", error)
            if raise_errors:
                raise


def send_bulk_email_async(app, recipients, subject, body):
    """Queue a bulk-email batch and return to the caller immediately."""

    unique_recipients = list(
        dict.fromkeys(
            email.strip()
            for email in recipients
            if is_deliverable_email(email)
        )
    )

    if not unique_recipients:
        return None

    from website.utils.durable_email_queue import enqueue_email_job, deliver_bulk_email_job
    if enqueue_email_job(deliver_bulk_email_job, unique_recipients, str(subject), str(body)):
        return True

    return _email_executor.submit(
        _send_bulk_email,
        app,
        unique_recipients,
        subject,
        body,
    )
