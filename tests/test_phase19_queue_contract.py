"""Phase 19 source-level queue integrity checks; no Flask/MySQL required."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
EMPLOYER = ROOT / 'website' / 'employer' / 'queue_management_route.py'
KIOSK = ROOT / 'website' / 'kiosk' / 'kiosk_queue_routes.py'


def function_body(source, name):
    match = re.search(r'^def ' + re.escape(name) + r'\(', source, re.M)
    if not match:
        raise AssertionError(f'Missing function: {name}')
    next_func = re.search(r'^def |^@\w+\.route\(', source[match.end():], re.M)
    return source[match.start(): match.end() + next_func.start()] if next_func else source[match.start():]


class QueueContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.employer = EMPLOYER.read_text(encoding='utf-8')
        cls.kiosk = KIOSK.read_text(encoding='utf-8')

    def test_fcfs_uses_queue_entry_time_and_tiebreaker(self):
        body = function_body(self.employer, 'get_next_callable_queue')
        self.assertIn('JobFairQueue.queued_at.asc()', body)
        self.assertIn('JobFairQueue.queue_group_id.asc()', body)
        self.assertNotIn('registration_method', body)

    def test_call_next_serializes_on_fair_row(self):
        body = function_body(self.employer, 'call_next_queue')
        self.assertIn('JobFair.job_fair_id == ongoing_job_fair.job_fair_id', body)
        self.assertIn('.with_for_update()', body)
        self.assertLess(body.index('.with_for_update()'), body.index('get_next_callable_queue('))

    def test_call_next_checks_applicant_and_station_claims(self):
        body = function_body(self.employer, 'call_next_queue')
        self.assertIn('applicant_active_queues', body)
        self.assertIn('q.assigned_station_id is not None', body)
        self.assertIn('assigned_station_id=station.station_id, status="Active"', body)
        self.assertIn('next_queue.assigned_station_id = station.station_id', body)
        self.assertIn('db.session.commit()', body)

    def test_kiosk_queue_creation_locks_fair_and_rechecks_stale_state(self):
        self.assertIn('locked_fair = (db.session.query(JobFair)', self.kiosk)
        self.assertIn('.with_for_update().first()', self.kiosk)
        self.assertIn('fresh_ids != initial_ids', self.kiosk)
        self.assertIn('fresh_queue_states != existing_queue_states', self.kiosk)

    def test_kiosk_queue_numbers_are_scoped_to_booth(self):
        body = function_body(self.kiosk, 'get_next_queue_number')
        self.assertIn('JobFairQueue.job_fair_id', body)
        self.assertIn('JobFairQueue.employer_registration_id', body)
        self.assertIn('JobFairQueue.queue_number.desc()', body)

    def test_station_capacity_uses_configured_capacity_up_to_15(self):
        self.assertIn('min(reg.simultaneous_interview_capacity or 1, 15)', self.employer)


if __name__ == '__main__':
    unittest.main()
