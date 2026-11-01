"""Static integration checks for supervised Phase 5B. Run with python tests/test_supervised_phase5b.py"""
from pathlib import Path
import ast
root = Path(__file__).resolve().parents[1]
route = (root / "website/supervised_registration/routes.py").read_text(encoding="utf-8")
model = (root / "website/models.py").read_text(encoding="utf-8")
checks = {
    "syntax": lambda: ast.parse(route) is not None,
    "CSRF form validation": lambda: "form.validate_on_submit()" in route,
    "transaction commit": lambda: "db.session.commit()" in route,
    "transaction rollback": lambda: "db.session.rollback()" in route,
    "station revocation": lambda: '"revoked_at": datetime.utcnow()' in route,
    "capacity lock": lambda: "with_for_update()" in route,
    "on-site classification": lambda: 'registration_source="On-site"' in route,
    "reference generation": lambda: "secrets.token_hex(8).upper()" in route,
    "account unclaimed": lambda: "is_unclaimed=True" in route and "is_unclaimed" in model,
    "work history dates": lambda: '"work_from", "work_to"' in route,
    "payload limit": lambda: "256 * 1024" in route,
    "expiry recheck": lambda: "if not _applicant_station():" in route,
}
for name, check in checks.items():
    assert check(), f"FAIL: {name}"
    print("PASS:", name)
print("PASS: all static checks; live MySQL testing is still required")
