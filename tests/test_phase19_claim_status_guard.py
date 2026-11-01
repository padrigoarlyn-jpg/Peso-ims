"""Static regression guard for independent claiming account status."""
from pathlib import Path
import ast

root = Path(__file__).resolve().parents[1]
source = (root / "website/supervised_registration/routes.py").read_text(encoding="utf-8")
ast.parse(source)
assert "and not user.is_deleted" in source
assert 'and user.status == "pending"' in source
assert 'user.status = "active"' in source
assert "claim.used_at = datetime.utcnow()" in source
print("PASS: claim activation status and deletion guards present")
