from pathlib import Path
import ast
root = Path(__file__).resolve().parents[1]
forms = (root / "website/jobseeker/forms.py").read_text(encoding="utf-8")
routes = (root / "website/jobseeker/routes.py").read_text(encoding="utf-8")
ast.parse(forms); ast.parse(routes)
assert "def validate_contact_number(self, field):" in forms
assert 'r"09[0-9]{9}"' in forms
assert "def validate_former_ofw_return_date(self, field):" in forms
assert 'r"(0[1-9]|1[0-2])/[0-9]{4}"' in forms
assert '"unemployed_reason_other", "Specify the reason for unemployment."' in forms
assert 'raise ValueError("Please specify the reason for unemployment.")' in routes
assert 'raise ValueError("Country of termination is required when laid off abroad.")' in routes
print("Batch C static regression: passed")
