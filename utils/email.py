from concurrent.futures import ThreadPoolExecutor
import smtplib
import socket
import time

from flask import current_app
from flask_mail import Message

from website.extensions import mail
from website.utils.email_branding import apply_peso_branding


# Keep the pool deliberately small so bulk operations do not create hundreds
# of simultaneous SMTP connections. Requests only enqueue work and can return
# immediately; these workers perform the actual network I/O in the background.
_email_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="peso-email",
)




def is_deliverable_email(address):
    """Reject internal non-routable account placeholders before SMTP.

    This is a conservative internal-address safeguard, not email verification.
    """
    if not isinstance(address, str):
        return False
    value = address.strip().lower()
    if not value or value.count("@") != 1 or any(c.isspace() for c in value):
        return False
    local, domain = value.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        return False
    return not (domain == "peso.invalid" or domain.endswith(".invalid"))


def _normalize_recipients(recipient):
    if not recipient:
        raise ValueError("Email recipient is missing.")

    if isinstance(recipient, str):
        recipients = [recipient.strip()]
    else:
        recipients = [
            str(email).strip()
            for email in recipient
            if email and str(email).strip()
        ]

    recipients = [email for email in recipients if is_deliverable_email(email)]

    if not recipients:
        raise ValueError("No valid email recipients were provided.")

    return recipients


def _send_email_worker(
    app,
    recipients,
    subject,
    body,
    attachment_pdf=None,
    filename="Referral_Letter.pdf",
    raise_errors=False,
):
    """Perform the actual SMTP work outside the HTTP request."""

    with app.app_context():
        msg = Message(
            subject=subject,
            recipients=recipients,
            body=body,
        )
        try:
            apply_peso_branding(msg, body, app)
            if not msg.html or not any(
                attachment.headers.get("Content-ID") == "<peso-email-header>"
                for attachment in msg.attachments
            ):
                raise RuntimeError("PESO email branding missing before SMTP delivery")

            if attachment_pdf:
                msg.attach(
                    filename=filename,
                    content_type="application/pdf",
                    data=attachment_pdf,
                )

            # Retry only transient transport disconnects/timeouts. Authentication
            # rejection and all other errors are surfaced without retry.
            for attempt in range(2):
                try:
                    mail.send(msg)
                    break
                except (smtplib.SMTPServerDisconnected, socket.timeout, TimeoutError, ConnectionResetError):
                    if attempt:
                        raise
                    app.logger.warning("SMTP connection interrupted; retrying once | Subject: %s", subject)
                    time.sleep(1)

            app.logger.info(
                "Background email sent | Recipients: %s | Subject: %s",
                ", ".join(recipients),
                subject,
            )

        except Exception as error:
            # SMTP failures happen after the page has already responded, so log
            # them instead of blocking the route that queued the message.
            app.logger.exception(
                "Background email failed | Recipients: %s | Subject: %s | Error: %s",
                ", ".join(recipients),
                subject,
                error,
            )
            if raise_errors:
                raise

def send_email(
    recipient,
    subject,
    body,
    attachment_pdf=None,
    filename="Referral_Letter.pdf",
):
    if isinstance(recipient, str) and recipient.strip().lower().endswith("@peso.invalid"):
        current_app.logger.info("Email skipped: recipient has no verified delivery address | Subject: %s", subject)
        return False
    recipients = _normalize_recipients(recipient)

    app = current_app._get_current_object()
    pdf_copy = bytes(attachment_pdf) if attachment_pdf else None

    if subject in {
        "PESO Admin Login Verification Code",
        "PESO Admin Password Reset Verification Code",
        "PESO Admin Email Change Verification Code",
        "PESO Password Reset",
    }:
        started = time.monotonic()
        try:
            _send_email_worker(app, recipients, str(subject), str(body), pdf_copy, filename, raise_errors=True)
        finally:
            app.logger.info(
                "Authentication/recovery SMTP attempt completed | Subject: %s | Duration: %.2fs",
                subject,
                time.monotonic() - started,
            )
        return True

    from website.utils.durable_email_queue import enqueue_email_job, deliver_email_job
    if enqueue_email_job(deliver_email_job, recipients, str(subject), str(body), pdf_copy, filename):
        app.logger.info("Email queued durably | Subject: %s", subject)
        return True

    _email_executor.submit(
        _send_email_worker,
        app,
        recipients,
        str(subject),
        str(body),
        pdf_copy,
        filename,
    )

    app.logger.info(
        "Email queued | Recipients: %s | Subject: %s",
        ", ".join(recipients),
        subject,
    )

    return True
