"""Static regression guards for Employer applicant decisions (no DB required)."""
from pathlib import Path
import ast

source = Path(__file__).resolve().parents[1] / 'website/employer/routes.py'
tree = ast.parse(source.read_text(encoding='utf-8'))
funcs = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
for name in ('reject_applicant', 'hire_applicant', 'schedule_interview'):
    assert name in funcs, name
    text = ast.get_source_segment(source.read_text(encoding='utf-8'), funcs[name])
    assert 'current_app.logger.exception' in text, name
    assert 'str(e)' not in text, name
for name in ('reject_applicant', 'hire_applicant'):
    text = ast.get_source_segment(source.read_text(encoding='utf-8'), funcs[name])
    assert 'db.session.rollback()' in text, name
    assert 'db.session.commit()' in text, name
    assert 'Unable to ' in text, name
assert 'APPLICATION_REJECTED application_id=' in source.read_text(encoding='utf-8')
print('PASS: 3 applicant/interview error-message guards, 2 transaction guards, 1 audit guard')
