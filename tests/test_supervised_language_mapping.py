"""Regression checks for supervised JSRF language mapping. No database required."""
from pathlib import Path
from html.parser import HTMLParser

root = Path(__file__).resolve().parents[1]
html = (root / 'website/templates/supervised_registration/jsrf_preview.html').read_text(encoding='utf-8')

class Inputs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}
    def handle_starttag(self, tag, attrs):
        if tag == 'input':
            values = dict(attrs)
            name = values.get('name', '')
            if name.startswith('language_'):
                assert name not in self.fields, f'Duplicate field: {name}'
                self.fields[name] = values

parser = Inputs()
parser.feed(html)
for index, language in enumerate(('English', 'Filipino')):
    name = f'language_{index}_language_name'
    assert parser.fields[name].get('type') == 'hidden'
    assert parser.fields[name].get('value') == language
    for field in ('can_read', 'can_write', 'can_speak', 'can_understand'):
        assert parser.fields[f'language_{index}_{field}'].get('type') == 'checkbox'
js = (root / 'website/static/javascript/supervised_registration/supervised_parity.js').read_text(encoding='utf-8')
assert 'name="language_${index}_language_name"' in js
assert '`language_${index}_${field}`' in js
assert 'table.rows.length >= 10' in js
print('PASS: English and Filipino names and four proficiency fields mapped correctly')
print('PASS: New language rows use indexed backend field names')
