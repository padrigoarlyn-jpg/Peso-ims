"""Phase 18: read-only object-level authorization tests for local PESO-IMS.

Supply two different employer accounts, two different jobseeker accounts,
and existing IDs owned by account A. Missing configuration SKIPS tests.
"""
import os
import unittest
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

BASE = os.environ.get('PESO_HTTP_TEST_BASE_URL', 'http://127.0.0.1:5000').rstrip('/')

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

OPENER = build_opener(NoRedirect)

def fetch(path, cookie):
    req = Request(BASE + path, headers={'Cookie': cookie, 'User-Agent': 'PESO-IMS-Phase18-Local-QA'})
    try:
        with OPENER.open(req, timeout=8) as response:
            return response.status, dict(response.headers), response.read(4096)
    except HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read(4096)

def deny_or_safe_redirect(test, result):
    status, headers, _ = result
    if status in (301, 302, 303, 307, 308):
        location = headers.get('Location', '')
        target = urlparse(location)
        base = urlparse(BASE)
        test.assertTrue(not target.netloc or target.netloc == base.netloc, 'Unexpected external redirect')
        # Any redirect is only a preliminary signal: review Location manually.
        test.assertTrue(location, 'Redirect missing Location')
    else:
        test.assertIn(status, (401, 403, 404), f'Unexpected access response: {status}')

class Phase18RecordIsolation(unittest.TestCase):
    def require(self, *names):
        values = [os.environ.get(n, '').strip() for n in names]
        if not all(values):
            self.skipTest('Missing environment variable(s): ' + ', '.join(n for n, v in zip(names, values) if not v))
        return values

    def test_employer_b_cannot_view_employer_a_application_resume(self):
        cookie_a, cookie_b, application_id = self.require('PESO_TEST_EMPLOYER_A_COOKIE', 'PESO_TEST_EMPLOYER_B_COOKIE', 'PESO_TEST_EMPLOYER_A_APPLICATION_ID')
        self.assertNotEqual(cookie_a, cookie_b, 'Must use distinct sessions')
        self.assertTrue(application_id.isdecimal())
        deny_or_safe_redirect(self, fetch(f'/employer/application/{application_id}/resume', cookie_b))

    def test_jobseeker_b_cannot_view_jobseeker_a_fair_confirmation(self):
        cookie_a, cookie_b, registration_id = self.require('PESO_TEST_JOBSEEKER_A_COOKIE', 'PESO_TEST_JOBSEEKER_B_COOKIE', 'PESO_TEST_JOBSEEKER_A_REGISTRATION_ID')
        self.assertNotEqual(cookie_a, cookie_b, 'Must use distinct sessions')
        self.assertTrue(registration_id.isdecimal())
        deny_or_safe_redirect(self, fetch(f'/jobseeker/job-fair/confirmation/{registration_id}', cookie_b))

    def test_jobseeker_a_can_view_own_fair_confirmation(self):
        cookie_a, registration_id = self.require('PESO_TEST_JOBSEEKER_A_COOKIE', 'PESO_TEST_JOBSEEKER_A_REGISTRATION_ID')
        self.assertTrue(registration_id.isdecimal())
        status, _, _ = fetch(f'/jobseeker/job-fair/confirmation/{registration_id}', cookie_a)
        self.assertEqual(status, 200, 'Owner should access own existing registration; verify URL prefix and account')

if __name__ == '__main__':
    unittest.main()
