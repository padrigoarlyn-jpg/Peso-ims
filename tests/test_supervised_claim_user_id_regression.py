"""Run: python tests/test_supervised_claim_user_id_regression.py"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / 'website/supervised_registration/routes.py').read_text(encoding='utf-8')
model_source = (ROOT / 'website/models.py').read_text(encoding='utf-8')
ast.parse(source)
assert 'user_id = db.Column(db.Integer, primary_key=True)' in model_source
assert 'JobSeeker.query.filter_by(user_id=user.user_id).first()' in source
assert 'JobSeeker.query.filter_by(user_id=user.id).first()' not in source
assert 'claim.used_at = datetime.utcnow()' in source
assert 'db.session.commit()' in source
print('PASS: claim-account jobseeker lookup uses User.user_id; source parses')
