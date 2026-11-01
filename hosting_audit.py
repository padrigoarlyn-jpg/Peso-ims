"""Read-only PESO-IMS deployment preflight. Never prints secret values."""
from pathlib import Path
import importlib.util
import os
import sys

ROOT = Path(__file__).resolve().parent
PRODUCTION = os.getenv('APP_ENV', '').lower() == 'production'
failures = 0
warnings = 0

def check(label, ok, *, critical=True, note=''):
    global failures, warnings
    if not ok:
        if critical: failures += 1
        else: warnings += 1
    status = 'PASS' if ok else ('FAIL' if critical else 'WARN')
    print(f'[{status}] {label}' + (f' — {note}' if note and not ok else ''))

print('PESO-IMS Hosting Preflight (read-only; no credentials displayed)')
print('Mode:', 'production' if PRODUCTION else 'development/staging')
for file in ('production_server.py','scheduler_worker.py','email_worker.py',
             'website/utils/durable_email_queue.py'):
    check(f'File {file}', (ROOT/file).is_file(), critical=PRODUCTION,
          note='Missing or phase not integrated')
for module in ('flask','waitress','pymysql','flask_limiter','redis','rq'):
    check(f'Dependency {module}', importlib.util.find_spec(module) is not None,
          critical=PRODUCTION, note='Install requirements.txt and requirements-hosting.txt')
if PRODUCTION:
    key = os.getenv('SECRET_KEY', '')
    check('Non-default SECRET_KEY', len(key) >= 32 and key != 'dev-only-change-this-secret-key',
          note='Set a long random value in private environment')
    check('HTTPS public URL', os.getenv('PUBLIC_BASE_URL','').startswith('https://'))
    check('Secure session cookies', os.getenv('SESSION_COOKIE_SECURE','').lower() == 'true')
    check('Trusted hosts configured', bool(os.getenv('TRUSTED_HOSTS','').strip()))
    check('External rate limit storage', os.getenv('RATELIMIT_STORAGE_URI','').startswith(('redis://','rediss://')))
    check('Database URL configured', bool(os.getenv('DATABASE_URL','').strip()))
    check('Scheduler disabled in web process', os.getenv('ENABLE_SCHEDULER','').lower() == 'false',
          note='Use a dedicated scheduler process')
    check('Debug not enabled', os.getenv('FLASK_DEBUG','').lower() not in ('1','true','yes'))
    check('Durable mail Redis configured', bool(os.getenv('EMAIL_QUEUE_REDIS_URL','') or os.getenv('REDIS_URL','')),
          critical=False, note='Verify the actual variable expected by durable_email_queue.py')
else:
    print('[INFO] Set APP_ENV=production to enable production environment checks.')
check('Project .env present', (ROOT/'.env').exists(), critical=False,
      note='Expected for local development only; never commit or publish')
check('Git ignore rules present', (ROOT/'.gitignore').exists(), critical=False)
print(f'\nResult: {failures} failure(s), {warnings} warning(s).')
sys.exit(1 if failures else 0)
