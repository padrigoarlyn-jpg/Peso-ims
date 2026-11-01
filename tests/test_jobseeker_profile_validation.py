"""Static regression checks for Jobseeker Profile input validation."""
import ast
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = (root / 'website/jobseeker/routes.py').read_text(encoding='utf-8')
ast.parse(source)
checks = {
    'reject_mismatched_work_rows': 'len(set(map(len, row_lists))) != 1',
    'reject_excessive_work_rows': 'len(work_ids) > 50',
    'reject_foreign_work_ids': 'work_id not in existing_rows',
    'reject_duplicate_work_ids': 'work_id in seen_ids',
    'reject_overlong_work_values': 'A work experience field exceeds its character limit.',
    'reject_incomplete_work_values': 'Complete all work experience fields before saving.',
    'reject_invalid_months': 'months.isdecimal()',
    'enforce_months_range': '0 <= number_of_months <= 1200',
    'whitelist_preferred_job_types': 'allowed_job_types = {"Full Time", "Part Time"}',
    'clear_unchecked_job_types': 'dict.fromkeys(job_types)) or None',
}
for name, snippet in checks.items():
    assert snippet in source, name
    print('PASS', name)
print(f'{len(checks)} static checks passed')
