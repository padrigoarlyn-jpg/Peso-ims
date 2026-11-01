from pathlib import Path
base = Path(__file__).resolve().parents[1]
login = (base / "website/templates/auth/login.html").read_text(encoding="utf-8")
claim = (base / "website/templates/supervised_registration/claim_account.html").read_text(encoding="utf-8")
assert "url_for('supervised_registration.claim_account')" in login
assert "url_for('auth.login')" in claim
print("PASS: claim/login navigation links")
