from functools import wraps
from flask import abort, redirect, url_for
from flask_login import current_user

def role_required(*allowed_roles):
    """
    Restricts route access to users who are authenticated and match one of the allowed roles.
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # 1. Check if user is logged in
            if not current_user.is_authenticated:
                return redirect(url_for("auth.login"))
            
            # 2. Check if user's role matches any of the allowed roles
            if current_user.role not in allowed_roles:
                abort(403)  # Forbidden access
                
            return f(*args, **kwargs)
        return decorated_function
    return decorator
