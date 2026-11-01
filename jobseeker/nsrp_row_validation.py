"""Shared server-side validation for NSRP/JSRF repeating sections.

Call from both online and supervised submit endpoints before writing any
jobseeker child rows. Never trust browser-only validation.
"""
from datetime import date

GROUPS = {
    "Language proficiency": {"lang_name[]": 50},
    "Technical/vocational training": {
        "tvet_course[]": 150, "tvet_hours[]": 12,
        "tvet_institution[]": 150, "tvet_skills[]": 255,
        "tvet_certificate[]": 100,
    },
    "Eligibility and professional licenses": {
        "eligibility_name[]": 150, "eligibility_date[]": 10,
        "license_name[]": 150, "license_validity[]": 10,
    },
    "Work experience": {
        "company[]": 150, "company_address[]": 150,
        "position[]": 150, "number_of_months[]": 12,
        "appointment_status[]": 50,
    },
}


def validate_nsrp_rows(formdata):
    """Raise ValueError on oversized, malformed or incomplete submitted rows.

    formdata must expose getlist(name), as Werkzeug MultiDict does.
    Empty placeholder rows are allowed; fields are not silently discarded.
    """
    # Language checkboxes are keyed by the entered language name. Duplicate
    # names would make one checkbox selection apply to multiple saved rows.
    language_names = [str(name or "").strip() for name in formdata.getlist("lang_name[]")]
    nonempty_names = [name for name in language_names if name]
    normalized_names = [name.casefold() for name in nonempty_names]
    if len(normalized_names) != len(set(normalized_names)):
        raise ValueError("Language proficiency: each language may be entered only once.")

    # Reject unexpected checkbox values, including manipulated submissions.
    # Unchecked checkboxes are absent; checked HTML checkboxes normally send 'on'.
    for name in nonempty_names:
        for proficiency in ("read", "write", "speak", "understand"):
            key = f"lang_{proficiency}_{name}"
            values = formdata.getlist(key)
            if len(values) > 1 or any(value != "on" for value in values):
                raise ValueError("Language proficiency: invalid checkbox value.")

    for group_name, fields in GROUPS.items():
        values = {key: formdata.getlist(key) for key in fields}
        if any(len(rows) > 50 for rows in values.values()):
            raise ValueError(f"{group_name}: maximum of 50 rows allowed.")
        for key, entries in values.items():
            for value in entries:
                if len(str(value or '').strip()) > fields[key]:
                    raise ValueError(f"{group_name}: {key} exceeds {fields[key]} characters.")
        count = max((len(rows) for rows in values.values()), default=0)
        for index in range(count):
            row = {key: (entries[index] if index < len(entries) else '').strip()
                   for key, entries in values.items()}
            if not any(row.values()):
                continue
            if group_name == 'Technical/vocational training':
                if not row['tvet_course[]']:
                    raise ValueError(f"Training row {index + 1}: course is required when details are entered.")
                hours = row['tvet_hours[]']
                if hours and (not hours.isascii() or not hours.isdecimal() or int(hours) > 100000):
                    raise ValueError(f"Training row {index + 1}: hours must be 0–100000.")
            elif group_name == 'Eligibility and professional licenses':
                if not row['eligibility_name[]'] and not row['license_name[]']:
                    raise ValueError(f"Eligibility row {index + 1}: enter an eligibility or license name.")
                for field in ('eligibility_date[]', 'license_validity[]'):
                    value = row[field]
                    if value:
                        try:
                            date.fromisoformat(value)
                        except ValueError as exc:
                            raise ValueError(f"Eligibility row {index + 1}: invalid date.") from exc
            elif group_name == 'Work experience':
                # Fresh-graduate bypass is already handled by the existing route.
                if not row['company[]']:
                    raise ValueError(f"Work experience row {index + 1}: company is required.")
                months = row['number_of_months[]']
                if months and (not months.isascii() or not months.isdecimal() or int(months) > 1200):
                    raise ValueError(f"Work experience row {index + 1}: months must be 0–1200.")
    return True
