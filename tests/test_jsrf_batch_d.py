import importlib.util
from pathlib import Path
class MultiDict:
    def __init__(self, pairs):
        self.pairs = pairs
    def getlist(self, key):
        return [value for name, value in self.pairs if name == key]

source = Path(__file__).resolve().parent.parent / 'website/jobseeker/nsrp_row_validation.py'
spec = importlib.util.spec_from_file_location('nsrp_row_validation', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

cases = [
    ('normal', [('lang_name[]','English'),('lang_name[]','Filipino'),('lang_read_English','on')], True),
    ('duplicate case', [('lang_name[]','English'),('lang_name[]','english')], False),
    ('duplicate spacing', [('lang_name[]',' Filipino '),('lang_name[]','Filipino')], False),
    ('bad checkbox', [('lang_name[]','English'),('lang_read_English','yes')], False),
    ('duplicate checkbox', [('lang_name[]','English'),('lang_read_English','on'),('lang_read_English','on')], False),
    ('blank placeholder', [('lang_name[]','')], True),
    ('normal TVET', [('tvet_course[]','Programming'),('tvet_hours[]','20')], True),
    ('missing TVET course', [('tvet_hours[]','20')], False),
]
for name, fields, expected in cases:
    try:
        module.validate_nsrp_rows(MultiDict(fields))
        passed=True
    except ValueError:
        passed=False
    assert passed == expected, f'{name}: expected {expected}, got {passed}'
    print('PASS',name)
print(f'{len(cases)} scenarios passed')
