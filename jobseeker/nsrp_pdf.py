"""Overlay saved jobseeker data on official September 2020 NSRP Form 1.

Dependencies: reportlab>=4,<5 and pypdf>=5,<7.
Place official_nsrp_form.pdf alongside this module.
All overlay coordinates use TOP-LEFT origin in PDF points.
"""
from datetime import date, datetime
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from website.models import (
    JobSeekerLanguage,
    JobSeekerTVET,
    JobSeekerLicense,
    JobSeekerWorkHistory,
)

TEMPLATE = Path(__file__).with_name("official_nsrp_form.pdf")
W, H = 595.32, 841.92


def txt(c, value, x, y, width=100, size=7):
    """Draw a single line at top-left coordinates, shrinking to fit its cell."""
    if value is None:
        return
    if isinstance(value, (date, datetime)):
        value = value.strftime("%m/%d/%Y")
    else:
        value = str(value).strip()
    if not value:
        return
    value = value.encode("latin-1", "replace").decode("latin-1")
    font_name = "Helvetica"
    minimum = 4.5
    while size > minimum and stringWidth(value, font_name, size) > width:
        size = max(minimum, size - 0.25)
    if stringWidth(value, font_name, size) > width:
        original = value
        while value and stringWidth(value + "...", font_name, size) > width:
            value = value[:-1]
        value = value.rstrip()
        if value and value != original:
            value += "..."
    c.setFont(font_name, size)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(x, H - y, value)  # Convert top-left y to ReportLab bottom-left y.


def mark(c, x, top, selected):
    """Place an X in a checkbox; x and top are top-left-based coordinates."""
    if selected:
        c.setLineWidth(1.25)
        c.line(x - 3, H - top - 3, x + 3, H - top + 3)
        c.line(x - 3, H - top + 3, x + 3, H - top - 3)


def val(obj, name):
    return getattr(obj, name, None)


def norm(value):
    return str(value if value is not None else "").strip().lower().replace("_", " ").replace("-", " ")


def choices(value):
    if isinstance(value, (list, tuple, set)):
        return {norm(item) for item in value if item is not None and str(item).strip()}
    return {norm(item) for item in str(value or "").split(",") if item.strip()}


def yes(value):
    return value is True or norm(value) in {"yes", "true", "1"}


def stamp_page(page_no, seeker, languages, tvets, licenses, works):
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=(W, H))
    c.setFillColorRGB(0, 0, 0)

    def t(field, x, y, width=100, size=7):
        txt(c, val(seeker, field), x, y, width, size)

    def m(x, y, condition):
        mark(c, x, y, condition)

    if page_no == 0:
        # All coordinates: original PDF points measured from TOP-LEFT.
        # Name writing area is y=132..165; printed labels begin at y=167.
        for field, x, width in [
            ("last_name", 45, 100), ("first_name", 175, 105),
            ("middle_name", 295, 108), ("suffix", 440, 126),
        ]:
            t(field, x, 157, width, 8)
        t("birthdate", 205, 190, 90)
        sex = norm(seeker.sex)
        m(63, 201, sex == "male"); m(109, 201, sex == "female")
        t("religion", 87, 218, 115, 6.5)
        for field, y in [("street", 218), ("barangay", 232), ("city", 246), ("province", 260)]:
            t(field, 338, y, 215, 6.3)
        civil = norm(seeker.civil_status)
        for y, key in [(227, "single"), (241, "married"), (255, "widowed")]: m(108, y, civil == key)
        t("tin", 63, 274, 130); t("height", 493, 274, 73)
        t("contact_number", 491, 296, 77, 6)
        email = val(val(seeker, "user"), "email")
        txt(c, email if email and not email.endswith("@peso.invalid") else "", 491, 317, 77, 5.5)
        disabilities = choices(seeker.disability)
        for x, y, key in [(92, 287, "visual"), (159, 287, "speech"), (234, 287, "mental"),
                          (92, 305, "hearing"), (159, 305, "physical"), (234, 305, "others")]:
            m(x, y, key in disabilities)
        if "others" in disabilities: t("disability_other", 352, 307, 65, 5.5)
        employment = norm(seeker.employment_status)
        employed, unemployed = employment == "employed", employment == "unemployed"
        m(37, 340, employed); m(282, 340, unemployed)
        et = norm(seeker.employed_type)
        m(57, 358, employed and et in {"wage", "wage employed"})
        self_employed = employed and et in {"self", "self employed"}
        m(57, 371, self_employed)
        sector = norm(seeker.self_sector).replace(" ", "")
        for y, key in [(384,"fisherman/fisherfolk"),(397,"vendor/retailer"),(410,"home based worker"),
                       (425,"transport"),(436,"domestic worker"),(449,"freelancer"),
                       (462,"artisan/craft worker"),(475,"others")]:
            m(72,y,self_employed and sector == key.replace(" ",""))
        if self_employed: t("self_sector_other", 132, 481, 95, 5.5)
        if unemployed:
            t("jobsearch_duration", 518, 358, 36, 6)
            reason = norm(seeker.unemployed_reason).replace(" ", "")
            for x,y,keys in [
                (296,375,{"newentrant","freshgraduate","newentrant/freshgraduate"}),
                (296,397,{"finishedcontract"}),(296,421,{"resigned"}),
                (296,441,{"retired"}),(296,465,{"calamity","laidoffduetocalamity"}),
                (449,375,{"terminatedlocal","laidofflocal"}),
                (449,397,{"laidoffabroad","terminatedabroad"}),
                (449,419,{"otherunemployed","others"})]: m(x,y,reason in keys)
            t("termination_country", 492, 416, 65, 5.5)
            t("unemployed_reason_other", 492, 442, 65, 5.5)
        m(114, 474, yes(seeker.ofw_status)); m(165, 474, not yes(seeker.ofw_status))
        t("ofw_country", 110, 493, 110, 6)
        m(350, 474, yes(seeker.former_ofw)); m(384, 474, not yes(seeker.former_ofw))
        t("former_ofw_country", 390, 493, 125, 6)
        t("former_ofw_return_date", 410, 505, 140, 6)
        m(158, 515, yes(seeker.fourps_beneficiary)); m(197, 515, not yes(seeker.fourps_beneficiary))
        t("fourps_id", 415, 518, 170, 6)
        jobs=choices(seeker.pref_job_type)
        m(36, 567, "part time" in jobs or "parttime" in jobs)
        m(105, 567, "full time" in jobs or "fulltime" in jobs)
        scopes=choices(seeker.location_scope)
        m(194, 567, "local" in scopes); m(357, 565, "overseas" in scopes)
        for index,y in enumerate([596,618,640],1):
            for prefix,x,width in [("pref_occupation_",50,125),("local_location_",209,126),
                                   ("overseas_location_",372,191)]: t(prefix+str(index),x,y,width,6.5)
        language_rows={"english":691,"filipino":709,"mandarin":727,"others":745}
        other_used=False
        for lang in languages:
            key=norm(lang.language_name)
            if key not in language_rows:
                if other_used: continue
                other_used=True; key="others"; txt(c,lang.language_name,75,745,54,5.5)
            y=language_rows[key]
            for x,field in [(197,"can_read"),(300,"can_write"),(401,"can_speak"),(502,"can_understand")]:
                m(x,y,yes(getattr(lang,field,False)))
    else:
        school=norm(seeker.schooling_status)
        m(128,50,school in {"yes","in school","currently in school"})
        m(185,50,school in {"no","not in school","out of school"})
        # The official education table has 4 rows: elementary 91..115,
        # secondary 115..157, tertiary 157..176, graduate 176..205.
        for y,prefix in [(108,"elem"),(149,"sec"),(171,"tertiary"),(195,"grad")]:
            for field,x,width in [("year_grad",352,58),("level_reached",430,49),
                                  ("year_attended",504,55)]: t(prefix+"_"+field,x,y,width,5.7)
        # Form has no separate school-name column; preserve school names on a supplement.
        t("senior_high_strand",270,148,70,5.4)
        t("tertiary_course",123,171,213,5.8);t("grad_course",123,195,213,5.8)
        types=choices(seeker.sec_type)
        m(34,121,"secondary" in types or "non k12" in types)
        m(129,121,"senior high" in types or "k12" in types or "k 12" in types)
        for index,row in enumerate(tvets[:3]):
            y=271+index*14
            for value,x,width in [(row.training_course,46,150),(row.hours,214,40),
                                  (row.institution,268,91),(row.skills_acquired,364,102),
                                  (row.certificate_received,479,84)]: txt(c,value,x,y,width,5.6)
        for index,row in enumerate(licenses[:2]):
            y=354+index*14
            for value,x,width in [(row.eligibility_name,46,120),(row.eligibility_date,176,49),
                                  (row.license_name,268,182),(row.license_validity,485,77)]:txt(c,value,x,y,width,5.7)
        for index,row in enumerate(works[:3]):
            y=423+index*14
            for value,x,width in [(row.company,29,107),(row.company_address,146,100),
                                  (row.position,257,91),(row.number_of_months,363,62),
                                  (row.appointment_status,455,108)]:txt(c,value,x,y,width,5.6)
        skills=choices(seeker.skills)
        for x,ys,labels in [
            (32,[484,494,508,523,536,549],["auto mechanic","beautician","carpentry work","computer literate","domestic chores","driver"]),
            (244,[484,494,508,523,536,549],["electrician","embroidery","gardening","masonry","painter/artist","painting jobs"]),
            (422,[484,494,508,523,536,549],["photography","plumbing","sewing dresses","stenography","tailoring","others"])]:
            for y,key in zip(ys,labels):m(x,y,key in skills)
        t("skills_other",490,550,53,5.5)
        # Signature and PESO-only assessment intentionally left blank.
    c.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def generate_nsrp_pdf(seeker):
    """Return a seeked BytesIO containing the original form with saved data overlaid."""
    if not TEMPLATE.is_file():
        raise FileNotFoundError(f"NSRP background not found: {TEMPLATE}")

    sid = seeker.job_seeker_id
    languages = (JobSeekerLanguage.query.filter_by(job_seeker_id=sid)
                 .order_by(JobSeekerLanguage.language_id).all())
    tvets = (JobSeekerTVET.query.filter_by(job_seeker_id=sid)
             .order_by(JobSeekerTVET.tvet_id).all())
    licenses = (JobSeekerLicense.query.filter_by(job_seeker_id=sid)
                .order_by(JobSeekerLicense.license_id).all())
    works = (JobSeekerWorkHistory.query.filter_by(job_seeker_id=sid)
             .order_by(JobSeekerWorkHistory.work_id.desc()).all())

    original = PdfReader(str(TEMPLATE))
    if len(original.pages) != 2:
        raise ValueError("Expected the original two-page NSRP template")
    writer = PdfWriter()
    for index, page in enumerate(original.pages):
        page.merge_page(stamp_page(index, seeker, languages, tvets, licenses, works))
        writer.add_page(page)

    extras = []
    for label, rows, fields, limit in [
        ("Languages", languages, ["language_name", "can_read", "can_write", "can_speak", "can_understand"], len(languages)),
        ("Technical/Vocational Training", tvets, ["training_course", "hours", "institution", "skills_acquired", "certificate_received"], 3),
        ("Eligibility / Professional License", licenses, ["eligibility_name", "eligibility_date", "license_name", "license_validity"], 2),
        ("Work History", works, ["company", "company_address", "position", "number_of_months", "appointment_status"], 3),
    ]:
        for row in rows[limit:]:
            detail = " | ".join(f"{field}: {getattr(row, field, None) or 'N/A'}" for field in fields)
            extras.append((label, detail))

    # School names and education fields not present in the original template
    # are preserved on a supplementary page rather than placed over labels.
    for label, fields in [
        ("Elementary", ["elem_school", "elem_level_reached", "elem_year_attended"]),
        ("Secondary", ["sec_school", "sec_type", "sec_level_reached", "sec_year_attended"]),
        ("Tertiary", ["tertiary_school", "tertiary_level_reached", "tertiary_year_attended"]),
        ("Graduate", ["grad_school", "grad_level_reached", "grad_year_attended"]),
    ]:
        # School names have no designated column in this official template.
        name_field = fields[0]
        if getattr(seeker, name_field, None):
            extras.append(("Education - " + label, name_field + ": " + str(getattr(seeker, name_field))))
    if extras:
        buffer = BytesIO()
        c = canvas.Canvas(buffer, pagesize=(W, H))
        y = H - 45
        c.setFont("Helvetica-Bold", 12)
        c.drawString(35, y, "NSRP FORM 1 - SUPPLEMENTARY RECORDS")
        y -= 30
        for label, detail in extras:
            if y < 65:
                c.showPage()
                y = H - 45
            c.setFont("Helvetica-Bold", 8)
            c.drawString(35, y, label)
            y -= 13
            c.setFont("Helvetica", 7)
            safe_detail = detail.encode("latin-1", "replace").decode("latin-1")
            for line in simpleSplit(safe_detail, "Helvetica", 7, W - 75):
                if y < 65:
                    c.showPage()
                    y = H - 45
                c.setFont("Helvetica", 7)
                c.drawString(35, y, line)
                y -= 11
            y -= 8
        c.save()
        buffer.seek(0)
        for page in PdfReader(buffer).pages:
            writer.add_page(page)

    output = BytesIO()
    writer.write(output)
    output.seek(0)
    return output
