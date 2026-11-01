"""Read-only HTTP smoke/security checks for a RUNNING local PESO-IMS server.

Explicit opt-in: PESO_HTTP_TEST_BASE_URL=http://127.0.0.1:5000
No login, no POST, no database writes. Runs with Python stdlib only.
"""
import os
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, HTTPRedirectHandler, Request
from urllib.parse import urlparse


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Phase16HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = os.getenv('PESO_HTTP_TEST_BASE_URL', '').rstrip('/')
        if not base:
            raise unittest.SkipTest('Set PESO_HTTP_TEST_BASE_URL to explicitly enable live HTTP tests')
        parsed = urlparse(base)
        if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1') or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError('Safety restriction: only a local http://localhost or http://127.0.0.1 server is permitted')
        cls.base = base
        cls.client = build_opener(NoRedirect())

    def fetch(self, path):
        req = Request(self.base + path, headers={'User-Agent': 'PESO-IMS-Phase16-QA/1.0'})
        try:
            with self.client.open(req, timeout=8) as response:
                return response.status, response.headers
        except HTTPError as exc:
            return exc.code, exc.headers
        except URLError as exc:
            self.fail(f'Cannot connect to the local Flask server: {exc}')

    def test_unknown_path_returns_404(self):
        status, _ = self.fetch('/__phase16_nonexistent_endpoint_9bd01a')
        self.assertEqual(status, 404)

    def test_security_headers_on_404(self):
        _, headers = self.fetch('/__phase16_nonexistent_endpoint_9bd01a')
        self.assertEqual(headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(headers.get('X-Frame-Options'), 'SAMEORIGIN')
        self.assertTrue(headers.get('Referrer-Policy'))

    def test_admin_dashboard_requires_authentication(self):
        status, _ = self.fetch('/admin/dashboard')
        self.assertIn(status, (302, 303, 401, 403), f'Anonymous admin dashboard returned {status}')

    def test_admin_referrals_require_authentication(self):
        status, _ = self.fetch('/admin/job_referral')
        self.assertIn(status, (302, 303, 401, 403), f'Anonymous referral list returned {status}')

    def test_admin_employers_require_authentication(self):
        status, _ = self.fetch('/admin/employers')
        self.assertIn(status, (302, 303, 401, 403), f'Anonymous employer list returned {status}')

    def test_admin_job_fairs_require_authentication(self):
        status, _ = self.fetch('/admin/job-fairs')
        self.assertIn(status, (302, 303, 401, 403), f'Anonymous job fair list returned {status}')


if __name__ == '__main__':
    unittest.main()
