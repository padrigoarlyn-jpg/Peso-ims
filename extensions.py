import os

from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_wtf import CSRFProtect
from flask_mail import Mail
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

login_manager = LoginManager()
csrf = CSRFProtect()
mail = Mail()

# Initialize Rate Limiter using client IP
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["60 per minute"],
    storage_uri=os.getenv("RATELIMIT_STORAGE_URI", "memory://"),
)

login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"


@login_manager.user_loader
def load_user(user_id):
    from website.models import User
    # Reject legacy session IDs and any session issued before password recovery.
    try:
        account_id, version = str(user_id).split(":", 1)
        account_id, version = int(account_id), int(version)
        if account_id <= 0 or version < 0:
            return None
    except (TypeError, ValueError):
        return None
    user = db.session.get(User, account_id)
    if not user or user.is_deleted or user.status != "active":
        return None
    return user if (user.auth_version or 0) == version else None


db = SQLAlchemy(
    engine_options={
        "pool_pre_ping": True,  # Tests the connection before using it
        "pool_recycle": 300,    # Recycles connections every 5 minutes (300 seconds)
    }
)