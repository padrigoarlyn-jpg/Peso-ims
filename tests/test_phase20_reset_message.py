"""Static regression check for role-specific password reset feedback."""
from pathlib import Path
import ast

source = Path(__file__).resolve().parents[1] / "website" / "auth" / "routes.py"
tree = ast.parse(source.read_text(encoding="utf-8"))
text = source.read_text(encoding="utf-8")
assert 'if user.role == "admin":\n            flash("Your password has been updated. Please sign in and complete administrator MFA."' in text
assert 'flash("Your password has been updated. Please sign in with your new password.", "success")' in text
assert 'user.auth_version = (user.auth_version or 0) + 1' in text
print("PASS: role-specific reset feedback and auth_version preservation")
