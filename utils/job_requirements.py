"""
Single source of truth for structured job requirements.
"""

VALID_EDUCATION_LEVELS = {
    "NONE",
    "JHS",
    "HS",
    "SHS",
    "COLLEGE_UNDERGRAD",
    "ASSOCIATE",
    "BACHELOR",
    "RELEVANT_BACHELOR",
}

VALID_EXPERIENCE_MONTHS = {
    0,
    6,
    12,
    24,
    36,
    60,
}

STANDARD_JOB_QUALIFICATIONS = {
    "SKILL_COMPUTER",
    "SKILL_MS_OFFICE",
    "SKILL_DATA_ENTRY",
    "SKILL_COMMUNICATION",
    "SKILL_CUSTOMER_SERVICE",
    "SKILL_SALES",
    "SKILL_POS_CASHIER",
    "SKILL_ADMIN",
    "CERT_TESDA",
    "CERT_PRO_LICENSE",
    "CERT_DRIVER",
    "CERT_CSE",
}

VALID_QUALIFICATION_IMPORTANCE = {
    "Required",
    "Preferred",
}

EDUCATION_LABELS = {
    "NONE": "No Recognized Minimum Education",
    "JHS": "Junior High School",
    "HS": "High School Graduate / Equivalent",
    "SHS": "Senior High School Graduate",
    "COLLEGE_UNDERGRAD": "College Undergraduate",
    "ASSOCIATE": "Associate / 2-Year College Graduate",
    "BACHELOR": "Bachelor's Degree Graduate",
    "RELEVANT_BACHELOR": "Relevant Bachelor's Degree",
}

EXPERIENCE_LABELS = {
    0: "No Experience Required",
    6: "6 Months",
    12: "1 Year",
    24: "2 Years",
    36: "3 Years",
    60: "5 Years",
}

QUALIFICATION_LABELS = {
    "SKILL_COMPUTER": "Computer Literate",
    "SKILL_MS_OFFICE": "Microsoft Office Proficiency",
    "SKILL_DATA_ENTRY": "Data Entry / Typing Skills",
    "SKILL_COMMUNICATION": "Communication Skills",
    "SKILL_CUSTOMER_SERVICE": "Customer Service Skills",
    "SKILL_SALES": "Sales Skills",
    "SKILL_POS_CASHIER": "Cash Handling / POS Skills",
    "SKILL_ADMIN": "Administrative / Clerical Skills",
    "CERT_TESDA": "TESDA / TVET Certification",
    "CERT_PRO_LICENSE": "Professional License",
    "CERT_DRIVER": "Driver's License",
    "CERT_CSE": "Civil Service Eligibility",
}


def parse_structured_qualifications(form):
    selected_codes = form.getlist("standard_qualification[]")
    parsed = []
    seen_codes = set()

    for raw_code in selected_codes:
        code = (raw_code or "").strip()

        if code not in STANDARD_JOB_QUALIFICATIONS:
            continue

        if code in seen_codes:
            continue

        seen_codes.add(code)

        importance = (
            form.get(f"importance_{code}", "Required")
            or "Required"
        ).strip()

        if importance not in VALID_QUALIFICATION_IMPORTANCE:
            importance = "Required"

        parsed.append({
            "code": code,
            "importance": importance,
        })

    return parsed


def parse_minimum_education(
    form,
    field_name="minimum_education",
    default="NONE",
):
    value = (
        form.get(field_name, default)
        or default
    ).strip()

    if value not in VALID_EDUCATION_LEVELS:
        return None, (
            "Please select a valid minimum "
            "education requirement."
        )

    return value, None


def parse_minimum_experience_months(
    form,
    field_name="minimum_experience_months",
    default=0,
):
    raw_value = form.get(field_name, default)

    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None, (
            "Please select a valid minimum "
            "experience requirement."
        )

    if value not in VALID_EXPERIENCE_MONTHS:
        return None, (
            "Please select a valid minimum "
            "experience requirement."
        )

    return value, None


def validate_structured_requirements(
    form,
    *,
    require_common_qualification=True,
):
    minimum_education, error = (
        parse_minimum_education(form)
    )

    if error:
        return None, error

    minimum_experience_months, error = (
        parse_minimum_experience_months(form)
    )

    if error:
        return None, error

    structured_qualifications = (
        parse_structured_qualifications(form)
    )

    if (
        require_common_qualification
        and not structured_qualifications
    ):
        return None, (
            "Please select at least one "
            "common qualification."
        )

    return {
        "minimum_education":
            minimum_education,
        "minimum_experience_months":
            minimum_experience_months,
        "structured_qualifications":
            structured_qualifications,
    }, None
