import re
from datetime import date, datetime

from website.models import JobSeekerLicense, JobSeekerTVET
from website.utils.job_requirements import (
    EDUCATION_LABELS,
    QUALIFICATION_LABELS,
)

EDUCATION_RANK = {
    "NONE": 0, "JHS": 1, "HS": 2, "SHS": 3,
    "COLLEGE_UNDERGRAD": 4, "ASSOCIATE": 5, "BACHELOR": 6,
}



QUALIFICATION_ALIASES = {
    "SKILL_COMPUTER": ["computer literate", "computer literacy", "computer skills", "basic computer", "computer proficient", "computer proficiency"],
    "SKILL_MS_OFFICE": ["microsoft office", "ms office", "microsoft word", "ms word", "microsoft excel", "ms excel", "excel", "microsoft powerpoint", "ms powerpoint", "powerpoint"],
    "SKILL_DATA_ENTRY": ["data entry", "data encoding", "data encoder", "encoder", "typing", "typing skills"],
    "SKILL_COMMUNICATION": ["communication", "communication skills", "verbal communication", "written communication", "interpersonal communication"],
    "SKILL_CUSTOMER_SERVICE": ["customer service", "customer support", "customer relations", "customer assistance", "customer care", "service crew", "front desk"],
    "SKILL_SALES": ["sales", "selling", "sales associate", "sales representative", "salesperson", "retail sales", "merchandiser"],
    "SKILL_POS_CASHIER": ["cashier", "cash handling", "cash handling skills", "point of sale", "pos", "pos system"],
    "SKILL_ADMIN": ["administrative", "administrative assistant", "admin assistant", "clerical", "office clerk", "office assistant", "office administration", "filing", "record keeping", "records management"],
}

ROLE_FAMILIES = {
    "IT_SOFTWARE": {
        "job": ["software", "developer", "programmer", "web developer", "frontend", "front end", "backend", "back end", "full stack", "information technology", "it staff", "computer", "systems", "technical support", "help desk"],
        "education": ["information technology", "computer science", "information systems", "computer engineering", "software engineering", "computer technology"],
        "training": ["programming", "web development", "software development", "computer systems servicing", "css nc ii", "computer programming", "networking"],
        "license": [],
        "work": ["developer", "programmer", "it staff", "technical support", "help desk", "web developer", "software", "system administrator"],
    },
    "ADMIN_OFFICE": {
        "job": ["administrative", "admin assistant", "office assistant", "office clerk", "clerk", "secretary", "encoder", "data entry", "records", "receptionist"],
        "education": ["office administration", "business administration", "business management", "information management", "secretarial"],
        "training": ["office productivity", "bookkeeping", "computer operations", "microsoft office", "records management"],
        "license": ["civil service"],
        "work": ["administrative", "admin assistant", "office assistant", "office clerk", "clerk", "secretary", "encoder", "data entry", "records", "receptionist"],
    },
    "ACCOUNTING_FINANCE": {
        "job": ["accountant", "accounting", "bookkeeper", "finance", "auditor", "payroll", "billing"],
        "education": ["accountancy", "accounting", "financial management", "finance"],
        "training": ["bookkeeping", "accounting", "payroll"],
        "license": ["certified public accountant", "cpa"],
        "work": ["accountant", "accounting", "bookkeeper", "finance", "auditor", "payroll", "billing"],
    },
    "SALES_RETAIL": {
        "job": ["sales", "sales associate", "sales representative", "retail", "merchandiser", "cashier", "store staff"],
        "education": ["marketing", "business administration", "business management", "entrepreneurship"],
        "training": ["retail", "sales", "customer service"],
        "license": [],
        "work": ["sales", "sales associate", "sales representative", "retail", "merchandiser", "cashier", "store staff"],
    },
    "CUSTOMER_SERVICE": {
        "job": ["customer service", "customer support", "call center", "contact center", "front desk", "receptionist"],
        "education": ["business administration", "communication", "hospitality", "tourism"],
        "training": ["customer service", "contact center services", "call center"],
        "license": [],
        "work": ["customer service", "customer support", "call center", "contact center", "front desk", "receptionist"],
    },
    "FOOD_SERVICE": {
        "job": ["barista", "kitchen staff", "cook", "chef", "food service", "service crew", "waiter", "waitress", "restaurant", "baker", "pastry"],
        "education": ["hospitality management", "hotel and restaurant management", "culinary", "food service"],
        "training": ["cookery", "bread and pastry", "food and beverage services", "commercial cooking", "barista"],
        "license": [],
        "work": ["barista", "kitchen staff", "cook", "chef", "food service", "service crew", "waiter", "waitress", "restaurant", "baker", "pastry"],
    },
    "ELECTRICAL": {
        "job": ["electrician", "electrical technician", "electrical", "lineman"],
        "education": ["electrical engineering", "electrical technology", "industrial electricity"],
        "training": ["electrical installation and maintenance", "eim nc ii", "electrical installation"],
        "license": ["registered electrical engineer", "ree", "registered master electrician", "rme"],
        "work": ["electrician", "electrical technician", "electrical", "lineman"],
    },
    "DRIVING_LOGISTICS": {
        "job": ["driver", "delivery driver", "truck driver", "courier", "logistics", "warehouse"],
        "education": [],
        "training": ["driving", "driving nc ii", "forklift"],
        "license": ["driver", "drivers license", "driver license", "professional driver", "non professional driver", "lto"],
        "work": ["driver", "delivery driver", "truck driver", "courier", "logistics", "warehouse"],
    },
    "HEALTHCARE": {
        "job": ["nurse", "nursing", "caregiver", "healthcare", "medical", "midwife"],
        "education": ["nursing", "midwifery", "caregiving", "medical technology", "physical therapy"],
        "training": ["caregiving", "health care services", "emergency medical services"],
        "license": ["registered nurse", "nurse", "midwife"],
        "work": ["nurse", "caregiver", "healthcare", "medical", "midwife"],
    },
    "ENGINEERING": {
        "job": ["engineer", "engineering", "civil engineer", "mechanical engineer", "electronics engineer"],
        "education": ["civil engineering", "mechanical engineering", "electronics engineering", "industrial engineering", "engineering"],
        "training": ["cad", "autocad", "drafting"],
        "license": ["engineer", "professional engineer"],
        "work": ["engineer", "engineering", "cad operator", "draftsman"],
    },
}

RELEVANCE_STOPWORDS = {"and", "or", "the", "a", "an", "of", "for", "with", "staff", "worker", "employee", "assistant", "associate", "specialist", "officer", "full", "time", "part", "position", "job"}


def normalize_text(value):
    if value is None:
        return ""
    text = str(value).casefold().strip()
    text = re.sub(r"[^a-z0-9+#.\s/-]", " ", text)
    return re.sub(r"\s+", " ", text)


def contains_alias(text, aliases):
    normalized = normalize_text(text)
    return bool(normalized) and any(normalize_text(alias) in normalized for alias in aliases if alias)


def extract_relevance_keywords(value):
    return {w for w in re.findall(r"[a-z0-9+#]+", normalize_text(value)) if len(w) >= 3 and w not in RELEVANCE_STOPWORDS}


def build_target_job_text(job):
    return normalize_text(" ".join([getattr(job, "job_title", "") or "", getattr(job, "category", "") or ""]))


def detect_job_families(job):
    target = build_target_job_text(job)
    return {name for name, family in ROLE_FAMILIES.items() if contains_alias(target, family.get("job", []))}


def evidence_matches_job_family(job, evidence_text, evidence_type):
    evidence = normalize_text(evidence_text)
    if not evidence:
        return False
    families = detect_job_families(job)
    if families:
        return any(contains_alias(evidence, ROLE_FAMILIES[name].get(evidence_type, [])) for name in families)
    job_keywords = extract_relevance_keywords(build_target_job_text(job))
    evidence_keywords = extract_relevance_keywords(evidence)
    return bool(job_keywords and evidence_keywords and job_keywords.intersection(evidence_keywords))


def determine_jobseeker_education_level(jobseeker):
    """Normalize highest education. schooling_status is NOT an education level."""
    grad_course = normalize_text(getattr(jobseeker, "grad_course", ""))
    grad_level = normalize_text(getattr(jobseeker, "grad_level_reached", ""))
    grad_year = getattr(jobseeker, "grad_year_grad", None)

    if grad_year or "graduated" in grad_level or (
        grad_course and grad_level not in {"not applicable", "n/a"}
    ):
        return "BACHELOR"

    tertiary_course = normalize_text(getattr(jobseeker, "tertiary_course", ""))
    tertiary_level = normalize_text(getattr(jobseeker, "tertiary_level_reached", ""))
    tertiary_year = getattr(jobseeker, "tertiary_year_grad", None)

    if tertiary_year or "graduated" in tertiary_level:
        text = f"{tertiary_course} {tertiary_level}"
        if any(term in text for term in {
            "associate", "two year", "2 year", "2-year", "diploma"
        }):
            return "ASSOCIATE"
        return "BACHELOR"

    if tertiary_course or (
        tertiary_level and tertiary_level not in {"not applicable", "n/a"}
    ):
        return "COLLEGE_UNDERGRAD"

    sec_type = normalize_text(getattr(jobseeker, "sec_type", ""))
    sec_level = normalize_text(getattr(jobseeker, "sec_level_reached", ""))
    strand = normalize_text(getattr(jobseeker, "senior_high_strand", ""))
    sec_year = getattr(jobseeker, "sec_year_grad", None)
    sec_text = f"{sec_type} {sec_level} {strand}"

    if strand or "senior high" in sec_text or "grade 12" in sec_text:
        return "SHS"
    if sec_year:
        return "HS"
    if any(term in sec_text for term in [
        "junior high", "grade 10", "grade 9", "grade 8", "grade 7"
    ]):
        return "JHS"

    if getattr(jobseeker, "elem_year_grad", None) or normalize_text(
        getattr(jobseeker, "elem_level_reached", "")
    ):
        return "JHS"

    return "NONE"


def get_jobseeker_education_label(jobseeker):
    grad_year = getattr(jobseeker, "grad_year_grad", None)
    grad_level = normalize_text(getattr(jobseeker, "grad_level_reached", ""))
    if grad_year or "graduated" in grad_level:
        return "Graduate / Postgraduate Degree"

    if getattr(jobseeker, "grad_course", None) and grad_level not in {
        "not applicable", "n/a"
    }:
        return "Graduate Studies / Postgraduate"

    level = determine_jobseeker_education_level(jobseeker)
    if level == "BACHELOR":
        return "Bachelor's Degree"

    if level == "ASSOCIATE":
        return "Associate / 2-Year College Graduate"

    return EDUCATION_LABELS.get(level, level)


def format_experience_months(months):
    try:
        months = max(int(months or 0), 0)
    except (TypeError, ValueError):
        months = 0

    if months <= 0:
        return "No work experience recorded"

    years, remaining = divmod(months, 12)
    parts = []
    if years:
        parts.append(f"{years} year{'s' if years != 1 else ''}")
    if remaining:
        parts.append(f"{remaining} month{'s' if remaining != 1 else ''}")
    return " ".join(parts)


def get_most_recent_work_display(jobseeker):
    history = list(getattr(jobseeker, "work_history", None) or [])
    if not history:
        legacy = (getattr(jobseeker, "experience", None) or "").strip()
        return legacy or "No work experience recorded"

    def sort_key(item):
        end = parse_work_date(getattr(item, "work_to", None), use_current_date=True)
        start = parse_work_date(getattr(item, "work_from", None))
        return (end or date.min, start or date.min)

    history.sort(key=sort_key, reverse=True)
    latest = history[0]
    position = getattr(latest, "position", None) or "Position not specified"
    company = getattr(latest, "company", None) or "Company not specified"
    return f"{position} at {company}"


def has_completed_tertiary_degree(jobseeker):
    """Return True only when the JSRF establishes tertiary degree completion."""
    tertiary_year = getattr(jobseeker, "tertiary_year_grad", None)
    tertiary_level = normalize_text(
        getattr(jobseeker, "tertiary_level_reached", "")
    )
    return bool(tertiary_year or "graduated" in tertiary_level)


def get_relevant_bachelor_evidence(jobseeker, job):
    """
    Match RELEVANT_BACHELOR conservatively.

    A relevant bachelor's requirement needs BOTH:
    1. evidence in the JSRF that the tertiary degree was completed; and
    2. a recorded tertiary course that matches the job family/title.

    Graduate-study fields are intentionally not used as a substitute for the
    applicant's bachelor's course because they describe a different degree.
    """
    if not has_completed_tertiary_degree(jobseeker):
        return []

    course = (getattr(jobseeker, "tertiary_course", "") or "").strip()
    if not course:
        return []

    if evidence_matches_job_family(job, course, "education"):
        return [f"Completed tertiary course: {course}"]

    return []


def get_relevant_bachelor_applicant_value(jobseeker):
    course = (getattr(jobseeker, "tertiary_course", "") or "").strip()
    if not course:
        return "Bachelor's course not recorded"

    if has_completed_tertiary_degree(jobseeker):
        return f"Bachelor's Degree — {course}"

    return f"Tertiary course — {course} (completion not established)"


def education_meets_requirement(jobseeker, required_level, job=None):
    required_level = (required_level or "NONE").strip()
    if required_level == "NONE":
        return True
    if required_level == "RELEVANT_BACHELOR":
        return bool(job is not None and get_relevant_bachelor_evidence(jobseeker, job))
    required_rank = EDUCATION_RANK.get(required_level)
    applicant_rank = EDUCATION_RANK.get(determine_jobseeker_education_level(jobseeker), 0)
    return required_rank is not None and applicant_rank >= required_rank


def parse_work_date(value, use_current_date=False):
    if value in (None, ""):
        return date.today() if use_current_date else None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return date.today() if use_current_date else None
    if normalize_text(text) in {"present", "current", "ongoing", "now", "currently employed"}:
        return date.today()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%m/%d/%Y", "%d/%m/%Y", "%m/%Y", "%B %Y", "%b %Y", "%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    year_match = re.search(r"\b(19|20)\d{2}\b", text)
    if year_match:
        try:
            return date(int(year_match.group()), 1, 1)
        except ValueError:
            pass
    return date.today() if use_current_date else None


def month_index(value):
    return value.year * 12 + value.month


def merge_month_intervals(intervals):
    cleaned = sorted((min(start, end), max(start, end)) for start, end in intervals)
    if not cleaned:
        return []
    merged = [list(cleaned[0])]
    for start, end in cleaned[1:]:
        last = merged[-1]
        if start <= last[1] + 1:
            last[1] = max(last[1], end)
        else:
            merged.append([start, end])
    return [tuple(x) for x in merged]


def calculate_merged_months(intervals):
    return sum((end - start) + 1 for start, end in merge_month_intervals(intervals))


def is_work_history_relevant(history, job):
    text = " ".join([getattr(history, "position", "") or "", getattr(history, "company", "") or ""])
    return evidence_matches_job_family(job, text, "work")


def get_work_history_intervals(jobseeker, job=None, relevant_only=False):
    intervals, evidence = [], []
    for history in (getattr(jobseeker, "work_history", None) or []):
        if relevant_only and job is not None and not is_work_history_relevant(history, job):
            continue
        start = parse_work_date(getattr(history, "work_from", None))
        end = parse_work_date(getattr(history, "work_to", None), use_current_date=True)
        if not start or not end or end < start:
            continue
        intervals.append((month_index(start), month_index(end)))
        evidence.append({
            "position": getattr(history, "position", "") or "Position not specified",
            "company": getattr(history, "company", "") or "Company not specified",
            "work_from": getattr(history, "work_from", None),
            "work_to": getattr(history, "work_to", None) or "Present",
        })
    return intervals, evidence


def _history_months(history):
    direct = getattr(history, "number_of_months", None)
    if direct is not None:
        try:
            return max(0, int(direct))
        except (TypeError, ValueError):
            return 0
    start = parse_work_date(getattr(history, "work_from", None))
    end = parse_work_date(getattr(history, "work_to", None), use_current_date=True)
    if not start or not end or end < start:
        return 0
    return max(0, (end.year - start.year) * 12 + end.month - start.month + 1)


def calculate_total_experience_months(jobseeker):
    return sum(_history_months(h) for h in (getattr(jobseeker, "work_history", None) or []))


def calculate_relevant_experience_months(jobseeker, job):
    return sum(_history_months(h) for h in (getattr(jobseeker, "work_history", None) or []) if is_work_history_relevant(h, job))


def get_relevant_work_evidence(jobseeker, job):
    evidence = []
    for h in (getattr(jobseeker, "work_history", None) or []):
        if not is_work_history_relevant(h, job):
            continue
        position = getattr(h, "position", None) or "Position not specified"
        company = getattr(h, "company", None) or "Company not specified"
        months = _history_months(h)
        evidence.append(f"Work history: {position} at {company} ({months} month{'s' if months != 1 else ''})")
    return evidence


def experience_meets_requirement(jobseeker, job):
    required = getattr(job, "minimum_experience_months", 0) or 0
    return required <= 0 or calculate_relevant_experience_months(jobseeker, job) >= required


def get_jobseeker_tvet_records(jobseeker):
    job_seeker_id = getattr(jobseeker, "job_seeker_id", None)
    return [] if not job_seeker_id else JobSeekerTVET.query.filter_by(job_seeker_id=job_seeker_id).all()


def tvet_record_has_certificate(record):
    return bool((getattr(record, "certificate_received", "") or "").strip())


def jobseeker_has_tvet(jobseeker):
    return any(tvet_record_has_certificate(r) for r in get_jobseeker_tvet_records(jobseeker))


def get_relevant_tvet_evidence(jobseeker, job):
    evidence = []
    for record in get_jobseeker_tvet_records(jobseeker):
        if not tvet_record_has_certificate(record):
            continue
        text = " ".join([getattr(record, "training_course", "") or "", getattr(record, "skills_acquired", "") or "", getattr(record, "certificate_received", "") or ""])
        if evidence_matches_job_family(job, text, "training"):
            course = getattr(record, "training_course", "") or "TVET training"
            cert = getattr(record, "certificate_received", "") or "Certificate recorded"
            evidence.append(f"TVET: {course} — {cert}")
    return evidence


def jobseeker_has_relevant_tvet(jobseeker, job):
    return bool(get_relevant_tvet_evidence(jobseeker, job))


def get_jobseeker_license_records(jobseeker):
    job_seeker_id = getattr(jobseeker, "job_seeker_id", None)
    return [] if not job_seeker_id else JobSeekerLicense.query.filter_by(job_seeker_id=job_seeker_id).all()


def license_is_current(record):
    validity = getattr(record, "license_validity", None)
    if not validity:
        return True
    if isinstance(validity, datetime):
        validity = validity.date()
    return validity >= date.today() if isinstance(validity, date) else True


def get_driver_license_evidence(jobseeker):
    aliases = ["driver", "drivers license", "driver license", "professional driver", "non professional driver", "non-professional driver", "lto"]
    out = []
    for record in get_jobseeker_license_records(jobseeker):
        name = getattr(record, "license_name", "") or ""
        if license_is_current(record) and contains_alias(name, aliases):
            out.append(f"License: {name}")
    return out


def jobseeker_has_driver_license(jobseeker):
    return bool(get_driver_license_evidence(jobseeker))


def get_civil_service_evidence(jobseeker):
    aliases = ["civil service", "career service", "professional eligibility", "subprofessional eligibility", "cse"]
    out = []
    for record in get_jobseeker_license_records(jobseeker):
        name = getattr(record, "eligibility_name", "") or ""
        if contains_alias(name, aliases):
            out.append(f"Eligibility: {name}")
    return out


def jobseeker_has_civil_service_eligibility(jobseeker):
    return bool(get_civil_service_evidence(jobseeker))


def get_relevant_professional_license_evidence(jobseeker, job):
    driver_aliases = ["driver", "drivers license", "driver license", "professional driver", "non professional driver", "non-professional driver", "lto"]
    out = []
    for record in get_jobseeker_license_records(jobseeker):
        if not license_is_current(record):
            continue
        name = (getattr(record, "license_name", "") or "").strip()
        if not name or contains_alias(name, driver_aliases):
            continue
        if evidence_matches_job_family(job, name, "license"):
            out.append(f"Professional license: {name}")
    return out


def jobseeker_has_professional_license(jobseeker, job=None):
    if job is not None:
        return bool(get_relevant_professional_license_evidence(jobseeker, job))
    driver_aliases = ["driver", "drivers license", "driver license", "professional driver", "non professional driver", "non-professional driver", "lto"]
    for record in get_jobseeker_license_records(jobseeker):
        name = (getattr(record, "license_name", "") or "").strip()
        if name and license_is_current(record) and not contains_alias(name, driver_aliases):
            return True
    return False


def build_jobseeker_skill_text(jobseeker):
    parts = [getattr(jobseeker, "skills", "") or "", getattr(jobseeker, "experience", "") or ""]
    for tvet in get_jobseeker_tvet_records(jobseeker):
        parts += [getattr(tvet, "training_course", "") or "", getattr(tvet, "skills_acquired", "") or ""]
    for work in (getattr(jobseeker, "work_history", None) or []):
        parts += [getattr(work, "position", "") or "", getattr(work, "appointment_status", "") or ""]
    return normalize_text(" ".join(parts))


def detect_common_skills(jobseeker):
    text = build_jobseeker_skill_text(jobseeker)
    return {code for code, aliases in QUALIFICATION_ALIASES.items() if contains_alias(text, aliases)}


def get_skill_evidence(jobseeker, qualification_code):
    aliases = QUALIFICATION_ALIASES.get(qualification_code, [])
    evidence = []
    skills = getattr(jobseeker, "skills", "") or ""
    if contains_alias(skills, aliases):
        evidence.append(f"JSRF skills: {skills}")
    exp = getattr(jobseeker, "experience", "") or ""
    if contains_alias(exp, aliases):
        evidence.append(f"JSRF experience: {exp}")
    for work in (getattr(jobseeker, "work_history", None) or []):
        position = getattr(work, "position", "") or ""
        if contains_alias(position, aliases):
            evidence.append(f"Work history position: {position}")
    for tvet in get_jobseeker_tvet_records(jobseeker):
        text = " ".join([getattr(tvet, "training_course", "") or "", getattr(tvet, "skills_acquired", "") or ""])
        if contains_alias(text, aliases):
            evidence.append(f"TVET skill evidence: {getattr(tvet, 'training_course', '') or 'TVET record'}")
    return evidence


def evaluate_standard_qualification(jobseeker, job, code):
    if code in QUALIFICATION_ALIASES:
        evidence = get_skill_evidence(jobseeker, code)
    elif code == "CERT_TESDA":
        evidence = get_relevant_tvet_evidence(jobseeker, job)
    elif code == "CERT_PRO_LICENSE":
        evidence = get_relevant_professional_license_evidence(jobseeker, job)
    elif code == "CERT_DRIVER":
        evidence = get_driver_license_evidence(jobseeker)
    elif code == "CERT_CSE":
        evidence = get_civil_service_evidence(jobseeker)
    else:
        evidence = []
    return {"matched": bool(evidence), "evidence": evidence}


def get_jobseeker_standard_qualifications(jobseeker, job=None):
    qualifications = set(detect_common_skills(jobseeker))
    if job is not None:
        if jobseeker_has_relevant_tvet(jobseeker, job): qualifications.add("CERT_TESDA")
        if jobseeker_has_professional_license(jobseeker, job): qualifications.add("CERT_PRO_LICENSE")
    else:
        if jobseeker_has_tvet(jobseeker): qualifications.add("CERT_TESDA")
        if jobseeker_has_professional_license(jobseeker): qualifications.add("CERT_PRO_LICENSE")
    if jobseeker_has_driver_license(jobseeker): qualifications.add("CERT_DRIVER")
    if jobseeker_has_civil_service_eligibility(jobseeker): qualifications.add("CERT_CSE")
    return qualifications


def build_normalized_jobseeker_profile(jobseeker, job=None):
    education_level = determine_jobseeker_education_level(jobseeker)
    total_experience = calculate_total_experience_months(jobseeker)
    relevant_experience = calculate_relevant_experience_months(jobseeker, job) if job is not None else 0
    qualifications = get_jobseeker_standard_qualifications(jobseeker, job=job)
    return {
        "education_code": education_level,
        "education_label": get_jobseeker_education_label(jobseeker),
        "total_experience_months": total_experience,
        "relevant_experience_months": relevant_experience,
        "standard_qualifications": qualifications,
        "has_tvet": "CERT_TESDA" in qualifications,
        "has_driver_license": "CERT_DRIVER" in qualifications,
        "has_professional_license": "CERT_PRO_LICENSE" in qualifications,
        "has_civil_service_eligibility": "CERT_CSE" in qualifications,
    }


def calculate_job_match(job, jobseeker):
    profile = build_normalized_jobseeker_profile(jobseeker, job)
    details = []
    REQUIRED_WEIGHT, PREFERRED_WEIGHT = 2, 1
    earned_points = total_points = 0

    required_education = getattr(job, "minimum_education", "NONE") or "NONE"
    if required_education != "NONE":
        matched = education_meets_requirement(jobseeker, required_education, job=job)
        total_points += REQUIRED_WEIGHT
        if matched: earned_points += REQUIRED_WEIGHT
        evidence = get_relevant_bachelor_evidence(jobseeker, job) if required_education == "RELEVANT_BACHELOR" else [f"Normalized education: {profile['education_label']}"]
        details.append({
            "type": "education", "code": required_education, "label": "Minimum Education",
            "required_value": EDUCATION_LABELS.get(required_education, required_education),
            "applicant_value": (
                get_relevant_bachelor_applicant_value(jobseeker)
                if required_education == "RELEVANT_BACHELOR"
                else profile["education_label"]
            ),
            "importance": "Required",
            "matched": matched, "points_possible": REQUIRED_WEIGHT,
            "points_earned": REQUIRED_WEIGHT if matched else 0, "evidence": evidence,
        })

    required_experience = getattr(job, "minimum_experience_months", 0) or 0
    if required_experience > 0:
        applicant_months = profile["relevant_experience_months"] or 0
        matched = applicant_months >= required_experience
        total_points += REQUIRED_WEIGHT
        if matched: earned_points += REQUIRED_WEIGHT
        details.append({
            "type": "experience", "code": "EXPERIENCE", "label": "Relevant Work Experience",
            "required_value": required_experience, "applicant_value": applicant_months,
            "importance": "Required", "matched": matched,
            "points_possible": REQUIRED_WEIGHT,
            "points_earned": REQUIRED_WEIGHT if matched else 0,
            "evidence": get_relevant_work_evidence(jobseeker, job),
        })

    for qualification in (getattr(job, "structured_qualifications", None) or []):
        code = (getattr(qualification, "qualification_code", "") or "").strip()
        importance = getattr(qualification, "importance", "Required") or "Required"
        weight = PREFERRED_WEIGHT if importance == "Preferred" else REQUIRED_WEIGHT
        evaluation = evaluate_standard_qualification(jobseeker, job, code)
        matched = evaluation["matched"]
        total_points += weight
        if matched: earned_points += weight
        details.append({
            "type": "qualification", "code": code,
            "label": QUALIFICATION_LABELS.get(code, code),
            "required_value": True, "applicant_value": matched,
            "importance": importance, "matched": matched,
            "points_possible": weight, "points_earned": weight if matched else 0,
            "evidence": evaluation["evidence"],
        })

    match_percentage = round((earned_points / total_points) * 100, 2) if total_points > 0 else 0.0
    required_results = [x for x in details if x["importance"] == "Required"]
    preferred_results = [x for x in details if x["importance"] == "Preferred"]
    required_matched = sum(1 for x in required_results if x["matched"])
    preferred_matched = sum(1 for x in preferred_results if x["matched"])
    all_required_met = all(x["matched"] for x in required_results) if required_results else True

    if total_points <= 0:
        ranking_status = "Not Ranked"
        ranking_reason = "No structured requirements are available for this job."
    elif match_percentage <= 0:
        ranking_status = "Not Ranked"
        ranking_reason = "No structured job criteria matched the applicant profile."
    else:
        ranking_status = "Ranked"
        ranking_reason = None

    education_detail = next((x for x in details if x["type"] == "education"), None)
    experience_detail = next((x for x in details if x["type"] == "experience"), None)

    return {
        "match_percentage": match_percentage,
        "earned_points": earned_points,
        "total_points": total_points,
        "criteria_total": len(details),
        "criteria_matched": sum(1 for detail in details if detail["matched"]),
        "all_required_met": all_required_met,
        "required_total": len(required_results),
        "required_matched": required_matched,
        "preferred_total": len(preferred_results),
        "preferred_matched": preferred_matched,
        "education_code": profile["education_code"],
        "education_label": profile["education_label"],
        "total_experience_months": profile["total_experience_months"],
        "relevant_experience_months": profile["relevant_experience_months"],
        "total_experience_display": format_experience_months(
            profile["total_experience_months"]
        ),
        "relevant_experience_display": format_experience_months(
            profile["relevant_experience_months"]
        ),
        "most_recent_work": get_most_recent_work_display(jobseeker),
        "standard_qualifications": profile["standard_qualifications"],
        "details": details,
        "ranking_status": ranking_status,
        "ranking_reason": ranking_reason,
        "required": {"matched": required_matched, "total": len(required_results)},
        "preferred": {"matched": preferred_matched, "total": len(preferred_results)},
        "education": {
            "required": required_education,
            "applicant": profile["education_code"],
            "matched": education_detail["matched"] if education_detail else True,
            "evidence": education_detail["evidence"] if education_detail else [],
        },
        "experience": {
            "required_months": required_experience,
            "total_months": profile["total_experience_months"],
            "relevant_months": profile["relevant_experience_months"],
            "matched": experience_detail["matched"] if experience_detail else True,
            "evidence": experience_detail["evidence"] if experience_detail else [],
        },
        "qualifications": [x for x in details if x["type"] == "qualification"],
    }
