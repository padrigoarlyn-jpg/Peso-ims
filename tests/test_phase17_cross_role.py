"""Phase 17: opt-in, read-only authenticated cross-role HTTP checks.

Requires PESO_HTTP_TEST_BASE_URL and one or more PESO_TEST_<ROLE>_COOKIE
values. Run only with disposable test accounts. No cookies are printed.
"""
import os
import unittest
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse

BASE = os.getenv('PESO_HTTP_TEST_BASE_URL', '').rstrip('/')

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

OPENER = build_opener(NoRedirect)


def fetch(path, cookie):
    req = Request(urljoin(BASE + '/', path.lstrip('/')), headers={
        'Cookie': cookie,
        'User-Agent': 'PESO-IMS-Phase17-QA/1.0',
    })
    try:
        with OPENER.open(req, timeout=12) as resp:
            return resp.status, resp.headers.get('Location', '')
    except HTTPError as exc:
        return exc.code, exc.headers.get('Location', '')


class CrossRoleSecurity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BASE:
            raise unittest.SkipTest('Set PESO_HTTP_TEST_BASE_URL to run live HTTP tests')
        parsed = urlparse(BASE)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            raise ValueError('PESO_HTTP_TEST_BASE_URL must be an http(s) URL')

    def check_access(self, role, path, allowed):
        cookie = os.getenv('PESO_TEST_' + role.upper() + '_COOKIE', '').strip()
        if not cookie:
            self.skipTest('Set PESO_TEST_' + role.upper() + '_COOKIE to enable this check')
        try:
            status, location = fetch(path, cookie)
        except URLError as exc:
            self.fail(f'Server unreachable: {exc.reason}')
        if allowed:
            self.assertEqual(status, 200, f'{role} should access {path}; received {status} redirect={location!r}')
        else:
            self.assertIn(status, (302, 303, 401, 403, 404), f'{role} unexpectedly accessed {path}: HTTP {status}')
            if status in (302, 303):
                target = urlparse(urljoin(BASE + '/', location))
                self.assertEqual(target.netloc, urlparse(BASE).netloc, 'Unexpected external redirect')
                self.assertNotEqual(target.path.rstrip('/'), path.rstrip('/'), 'Redirect loops to protected path')

    def test_admin_own_dashboard(self):
        self.check_access('admin', '/admin/dashboard', True)

    def test_employer_own_dashboard(self):
        self.check_access('employer', '/employer/dashboard', True)

    def test_jobseeker_own_dashboard(self):
        self.check_access('jobseeker', '/jobseeker/dashboard', True)

    def test_employer_blocked_from_admin_referrals(self):
        self.check_access('employer', '/admin/job_referral', False)

    def test_jobseeker_blocked_from_admin_employers(self):
        self.check_access('jobseeker', '/admin/employers', False)

    def test_jobseeker_blocked_from_employer_dashboard(self):
        self.check_access('jobseeker', '/employer/dashboard', False)

    def test_employer_blocked_from_jobseeker_dashboard(self):
        self.check_access('employer', '/jobseeker/dashboard', False)

    def test_admin_blocked_from_employer_dashboard(self):
        self.check_access('admin', '/employer/dashboard', False)

    def test_admin_blocked_from_jobseeker_dashboard(self):
        self.check_access('admin', '/jobseeker/dashboard', False)

    def test_regular_admin_blocked_from_create_admin(self):
        self.check_access('admin', '/admin/create-admin', False)


if __name__ == '__main__':
    unittest.main()
