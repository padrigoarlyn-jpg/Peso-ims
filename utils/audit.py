"""Small, safe audit-log helper for security and high-impact workflow events."""

from flask import current_app, request
from flask_login import current_user

from website.extensions import db
from website.models import AuditLog


def _client_ip():
    # request.remote_addr is intentionally preferred over untrusted forwarding
    # headers unless the application is later placed behind ProxyFix.
    return (request.remote_addr or "")[:45] or None


def record_audit(action, *, user_id=None, commit=False):
    """Queue an audit row, optionally committing it immediately.

    Never pass passwords, OTPs, reset tokens, or other secrets in ``action``.
    For normal business workflows, leave commit=False so the audit row shares
    the same transaction as the state change it describes.
    """
    clean_action = " ".join(str(action or "").split())[:255]
    if not clean_action:
        return None

    actor_id = user_id
    if actor_id is None and getattr(current_user, "is_authenticated", False):
        actor_id = current_user.user_id

    entry = AuditLog(
        user_id=actor_id,
        action=clean_action,
        ip_address=_client_ip(),
    )
    db.session.add(entry)

    if commit:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Failed to persist audit event: %s", clean_action)
            return None

    return entry
