from pathlib import Path
import re
s=Path(__file__).resolve().parents[1].joinpath("website/employer/job_fair_routes.py").read_text(encoding="utf-8")
assert s.count('_validated_optional_positive_int("representatives")') == 2
assert s.count('_validated_optional_positive_int("simultaneous_interview_capacity")') == 2
assert 'not raw_value.isascii() or not raw_value.isdecimal()' in s
assert 'if raw_value is None:' in s
compile(s, "job_fair_routes.py", "exec")
print("Employer Job Fair integer validation static regression: PASS")
