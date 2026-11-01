"""Static regression tests for Phase 9 claim-page privacy headers."""
from pathlib import Path
import ast

source = Path(__file__).resolve().parents[1] / "website/supervised_registration/routes.py"
code = source.read_text(encoding="utf-8")
ast.parse(code)
for name in ("registration_complete", "claim_account"):
    start = code.index(f"def {name}(")
    end = code.find("\n@", start + 1)
    section = code[start:end if end != -1 else None]
    for header in ("Cache-Control", "Pragma", "Referrer-Policy", "X-Robots-Tag"):
        assert header in section, (name, header)
print("Phase 9 static claim privacy checks: PASS")
