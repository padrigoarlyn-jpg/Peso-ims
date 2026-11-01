from pathlib import Path
import ast
root = Path(__file__).resolve().parents[1]
joblist = (root / "website/jobseeker/joblist_routes.py").read_text()
routes = (root / "website/jobseeker/routes.py").read_text()
for name, source in (("joblist",joblist),("routes",routes)):
    ast.parse(source)
assert "except IntegrityError:" in joblist
assert "concurrent_selection = JobFairJobPreselection.query.filter_by(" in joblist
assert '"already_preselected": True' in joblist
assert '"preselection_id": concurrent_selection.preselection_id' in joblist
assert "except IntegrityError:" in routes
assert "existing_application = Application.query.filter_by(" in routes
assert '"You have already submitted a request for this job."' in routes
print("PASS: 7 Batch E syntax/guard assertions")
