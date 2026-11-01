"""Dependency-free regression checks: python tests/test_supervised_patch_static.py"""
from pathlib import Path
import ast
import re

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ROOT / 'website/supervised_registration/routes.py'
MODELS = ROOT / 'website/models.py'
HTML = ROOT / 'website/templates/supervised_registration/jsrf_preview.html'
AUTH = ROOT / 'website/auth/routes.py'


def run():
    for path in (ROUTES, MODELS, HTML, AUTH):
        assert path.is_file(), f'Missing required file: {path}'
    source = ROUTES.read_text(encoding='utf-8')
    ast.parse(source)
    model = MODELS.read_text(encoding='utf-8')
    auth = AUTH.read_text(encoding='utf-8')
    html = HTML.read_text(encoding='utf-8')
    checks = {
        'one-hour applicant session': 'APPLICANT_SECONDS = 60 * 60' in source,
        'transaction rollback': 'db.session.rollback()' in source,
        'unclaimed account creation': 'is_unclaimed=True' in source,
        'repeatable field allowlist': '_REPEAT_GROUPS' in source and 'Duplicate repeatable-row field' in source,
        'repeatable row completeness': 'Incomplete repeatable row' in source,
        'account model flag': 'is_unclaimed = db.Column' in model,
        'auth unclaimed gate': 'is_unclaimed' in auth,
        'CSRF token': 'form.csrf_token' in html,
        'dynamic rows': 'addNewLanguageRow()' in html and 'supervised_parity.js' in html,
        'applicant expiry countdown': 'expires_at|tojson' in html,
    }
    for name, passed in checks.items():
        print(('PASS' if passed else 'FAIL') + ': ' + name)
    assert all(checks.values()), 'Static integration checks failed'
    print('PASS: 10 static integration checks')


if __name__ == '__main__':
    run()
