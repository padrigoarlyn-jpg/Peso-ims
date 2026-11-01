"""Source-level regression checks. Run with python tests/test_kiosk_validation_batch_a.py."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROUTES = (ROOT / 'website/kiosk/routes.py').read_text(encoding='utf-8')
QUEUES = (ROOT / 'website/kiosk/kiosk_queue_routes.py').read_text(encoding='utf-8')

for text in (ROUTES, QUEUES):
    ast.parse(text)

assert 'isinstance(job_fair_job_id, bool)' in ROUTES
assert 're.fullmatch(r"[0-9]+", job_fair_job_id)' in ROUTES
assert 'job_fair_job_id <= 0' in ROUTES
assert 'if not isinstance(password, str):' in ROUTES
assert '"Invalid password format."' in ROUTES
assert 'if not isinstance(request_data, dict):' in QUEUES
assert '"Invalid queue request."' in QUEUES
assert 'if isinstance(value, bool):' in QUEUES
print('PASS: 8 kiosk validation regression assertions')
