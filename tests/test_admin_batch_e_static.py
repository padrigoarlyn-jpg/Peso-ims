"""Run: python tests/test_admin_batch_e_static.py (inside project root)."""
import ast
from pathlib import Path
root=Path(__file__).resolve().parents[1]
admin=(root/'website/admin/routes.py').read_text(encoding='utf-8')
claim=(root/'website/supervised_registration/routes.py').read_text(encoding='utf-8')
model=(root/'website/models.py').read_text(encoding='utf-8')
for src in (admin,claim,model): ast.parse(src)
assert admin.count('Queue entry cutoff cannot be before the Job Fair starts.') >= 2
assert 'request.get_json(silent=True)' in admin
assert 'if not hasattr(data, "get"):' in admin
assert '@super_admin_required' in admin
assert 'DELETE AUDIT LOGS' in admin
assert 'timedelta(days=30)' in admin
assert 'JobSeeker.query.filter_by(user_id=user.user_id).first()' in claim
assert 'JobSeeker.query.filter_by(user_id=user.id).first()' not in claim
print('PASS: Batch E consolidated static regression checks')
