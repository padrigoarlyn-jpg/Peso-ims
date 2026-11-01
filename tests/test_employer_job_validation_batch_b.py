import ast, pathlib
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
ROOT = pathlib.Path(__file__).resolve().parents[1]
source = (ROOT / "website/employer/job_fair_routes.py").read_text()
fn = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "parse_job_fair_job_payload")
namespace = {"Decimal": Decimal, "InvalidOperation": InvalidOperation, "VALID_EDUCATION_LEVELS": {"NONE"}, "VALID_EXPERIENCE_MONTHS": {0}, "parse_structured_qualifications": lambda form: [{"code":"x", "importance":"Required"}]}
exec(compile(ast.Module(body=[fn],type_ignores=[]), "<isolated fair payload>", "exec"), namespace)
class Form(dict):
    def getlist(self,key): return self.get(key, [])
def check(override, expected_error=None):
    form=Form({"job_title":"Developer", "category":"IT", "employment_type":"Full Time", "description":"Description", "minimum_education":"NONE", "minimum_experience_months":"0", "salary_min":"12000", "salary_max":"20000", "vacancies":"2", "qualification[]":["Python"], "responsibility[]":["Coding"]})
    form.update(override)
    namespace["request"] = SimpleNamespace(form=form)
    result,error = namespace["parse_job_fair_job_payload"]()
    assert (error is not None) == (expected_error is not None), (override,error)
    if expected_error: assert expected_error in error, (override,error)
    else: assert result is not None
check({})
for value in ("NaN", "Infinity", "-Infinity"):
    check({"salary_min":value}, "finite salary")
check({"vacancies":"100001"}, "100,000")
check({"vacancies":"0"}, "100,000")
check({"job_title":"A"*151}, "150")
check({"category":"A"*101}, "100")
check({"description":"A"*10001}, "10,000")
check({"qualification[]":["A"*501]}, "Qualifications")
check({"responsibility[]":["A"*501]}, "Responsibilities")
regular=(ROOT/"website/employer/joblist_routes.py").read_text()
assert regular.count('minimum_experience_months = 0') == 0
assert regular.count('Please select a valid minimum experience requirement.') >= 4
assert 'requested_status is not None and requested_status not in' in regular
print('PASS: 12 isolated fair payload scenarios and regular job guard assertions')
