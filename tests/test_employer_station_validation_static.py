from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "website/employer/queue_management_route.py"
s = SOURCE.read_text(encoding="utf-8")
assert '@limiter.limit("5 per minute; 20 per hour")\n@login_required\ndef create_station_access' in s
assert 'len(token) > 128' in s
assert 'len(code) != 6 or not code.isascii() or not code.isdecimal()' in s
assert 'secrets.compare_digest(station.short_code_hash, _hash_station_secret(code))' in s
assert 'with_for_update().first_or_404()' in s
print("5 station validation checks passed")
