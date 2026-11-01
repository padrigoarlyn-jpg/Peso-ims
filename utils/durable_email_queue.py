"""Optional Redis/RQ delivery for ordinary notifications.

Development remains compatible with existing background threads. Production
operators should set PESO_EMAIL_QUEUE_URL and run email_worker.py separately.
"""
import os


def enqueue_email_job(function, *args):
    """Return True if enqueued, False if local thread fallback is permitted.

    A configured Redis queue must never silently fall back to volatile threads.
    """
    url = os.environ.get('PESO_EMAIL_QUEUE_URL', '').strip()
    require = os.environ.get('PESO_REQUIRE_DURABLE_EMAIL', '').strip().lower() in ('1', 'true', 'yes', 'on')
    if not url:
        if require:
            raise RuntimeError('PESO_REQUIRE_DURABLE_EMAIL is enabled but PESO_EMAIL_QUEUE_URL is missing')
        return False
    try:
        from redis import Redis
        from rq import Queue
        queue = Queue('peso-emails', connection=Redis.from_url(url), default_timeout=600)
        queue.enqueue(function, *args, job_timeout=600, result_ttl=86400, failure_ttl=604800)
    except Exception:
        # When durable delivery is configured, failing visibly is safer than
        # returning success for an email that could be lost on process restart.
        raise
    return True


def deliver_email_job(recipients, subject, body, attachment_pdf=None, filename='Referral_Letter.pdf'):
    """RQ entrypoint; instantiate app only inside worker, never serialize it."""
    from website import create_app
    from website.utils.email import _send_email_worker
    app = create_app()
    _send_email_worker(app, recipients, subject, body, attachment_pdf, filename, raise_errors=True)


def deliver_bulk_email_job(recipients, subject, body):
    from website import create_app
    from website.utils.background_email import _send_bulk_email
    app = create_app()
    _send_bulk_email(app, recipients, subject, body, raise_errors=True)
