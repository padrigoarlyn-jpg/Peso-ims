# Run from PESO-IMS project root (with a working Python environment):
# python tests/run_employer_batch_e.py
from pathlib import Path
import subprocess,sys,ast
root=Path(__file__).resolve().parents[1]
errors=[]
for path in (root/'website').rglob('*.py'):
    try: ast.parse(path.read_text(encoding='utf-8-sig'))
    except Exception as exc: errors.append((str(path),str(exc)))
print(f'Python syntax errors: {len(errors)}')
for path, err in errors: print(path,err)
failed=[]
for path in sorted((root/'tests').glob('test_*.py')):
    result=subprocess.run([sys.executable,str(path)],cwd=root, capture_output=True,text=True)
    print(('PASS' if result.returncode==0 else 'FAIL'),path.name)
    if result.returncode: failed.append(path.name);print((result.stdout+result.stderr)[-1000:])
print(f'Failed scripts: {len(failed)}')
raise SystemExit(1 if failed or errors else 0)
