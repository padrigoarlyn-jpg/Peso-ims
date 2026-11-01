import io
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

def generate_jsrf_excel(jobseeker):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "NSRP Form 1 - JSRF"
    ws.views.sheetView[0].showGridLines = True

    HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    WHITE_BOLD = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    BOLD = Font(name="Calibri", size=10, bold=True)
    NORMAL = Font(name="Calibri", size=10)

    THIN_BORDER = Border(
        left=Side(style="thin", color="D3D3D3"),
        right=Side(style="thin", color="D3D3D3"),
        top=Side(style="thin", color="D3D3D3"),
        bottom=Side(style="thin", color="D3D3D3"),
    )

    ws.merge_cells("A1:F1")
    ws["A1"] = "NATIONAL SKILLS REGISTRATION PROGRAM (NSRP) - JOB SEEKER REGISTRATION FORM (JSRF)"
    ws["A1"].font = Font(name="Calibri", size=13, bold=True, color="1F4E78")
    ws["A1"].alignment = Alignment(horizontal="center", vertical="center")

    curr_row = 3

    def add_section_header(title):
        nonlocal curr_row
        ws.merge_cells(start_row=curr_row, start_column=1, end_row=curr_row, end_column=6)
        cell = ws.cell(row=curr_row, column=1, value=title)
        cell.font = WHITE_BOLD
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="left", vertical="center", indent=1)
        curr_row += 1

    def add_data_row(label1, val1, label2="", val2="", label3="", val3=""):
        nonlocal curr_row
        ws.cell(row=curr_row, column=1, value=label1).font = BOLD
        ws.cell(row=curr_row, column=2, value=str(val1 if val1 is not None else "N/A")).font = NORMAL
        if label2:
            ws.cell(row=curr_row, column=3, value=label2).font = BOLD
            ws.cell(row=curr_row, column=4, value=str(val2 if val2 is not None else "N/A")).font = NORMAL
        if label3:
            ws.cell(row=curr_row, column=5, value=label3).font = BOLD
            ws.cell(row=curr_row, column=6, value=str(val3 if val3 is not None else "N/A")).font = NORMAL

        for col in range(1, 7):
            ws.cell(row=curr_row, column=col).border = THIN_BORDER
        curr_row += 1

    add_section_header("I. PERSONAL INFORMATION")
    add_data_row(
        "Job Seeker ID:",
        f"JS-{jobseeker.job_seeker_id:06d}",
        "Full Name:",
        getattr(jobseeker, "full_name", f"{jobseeker.first_name} {jobseeker.last_name}"),
        "Sex:",
        jobseeker.sex,
    )
    add_data_row(
        "Date of Birth:",
        jobseeker.birthdate.strftime("%Y-%m-%d") if getattr(jobseeker, "birthdate", None) else "N/A",
        "Civil Status:",
        jobseeker.civil_status,
        "Religion:",
        jobseeker.religion,
    )
    add_data_row(
        "TIN:",
        jobseeker.tin,
        "Height:",
        jobseeker.height if jobseeker.height else "N/A",
        "4Ps Beneficiary:",
        f"Yes (ID: {jobseeker.masked_fourps_id})" if getattr(jobseeker, "fourps_beneficiary", False) else "No",
    )
    add_data_row(
        "Email:",
        jobseeker.user.email if getattr(jobseeker, "user", None) else "N/A",
        "Contact No:",
        jobseeker.contact_number,
    )

    ws.cell(row=curr_row, column=1, value="Address:").font = BOLD
    full_addr = f"{jobseeker.street or ''}, {jobseeker.barangay or ''}, {jobseeker.city or ''}, {jobseeker.province or ''}".strip(", ")
    ws.merge_cells(start_row=curr_row, start_column=2, end_row=curr_row, end_column=6)
    ws.cell(row=curr_row, column=2, value=full_addr or "N/A").font = NORMAL
    for col in range(1, 7):
        ws.cell(row=curr_row, column=col).border = THIN_BORDER
    curr_row += 2

    add_section_header("II. EMPLOYMENT STATUS")
    add_data_row(
        "Current Status:",
        jobseeker.employment_status,
        "Employed Type:",
        getattr(jobseeker, "employed_type", "N/A"),
    )
    add_data_row(
        "Unemployed Reason:",
        getattr(jobseeker, "unemployed_reason_other", None) or getattr(jobseeker, "unemployed_reason", "N/A"),
        "Job Search Duration:",
        getattr(jobseeker, "jobsearch_duration", "N/A"),
    )
    add_data_row(
        "OFW Status:",
        getattr(jobseeker, "ofw_status", "N/A"),
        "Former OFW:",
        getattr(jobseeker, "former_ofw", "N/A"),
        "Disability:",
        getattr(jobseeker, "disability_other", None) or getattr(jobseeker, "disability", "N/A"),
    )
    curr_row += 1

    add_section_header("III. JOB PREFERENCES")
    add_data_row("Preferred Job Type:", getattr(jobseeker, "pref_job_type", "N/A"))
    add_data_row(
        "1st Preference:",
        getattr(jobseeker, "pref_occupation_1", "N/A"),
        "2nd Preference:",
        getattr(jobseeker, "pref_occupation_2", "N/A"),
        "3rd Preference:",
        getattr(jobseeker, "pref_occupation_3", "N/A"),
    )
    add_data_row(
        "Local Locations:",
        ", ".join(
            filter(
                None,
                [
                    getattr(jobseeker, "local_location_1", None),
                    getattr(jobseeker, "local_location_2", None),
                    getattr(jobseeker, "local_location_3", None),
                ],
            )
        )
        or "N/A",
    )
    add_data_row(
        "Overseas Locations:",
        ", ".join(
            filter(
                None,
                [
                    getattr(jobseeker, "overseas_location_1", None),
                    getattr(jobseeker, "overseas_location_2", None),
                    getattr(jobseeker, "overseas_location_3", None),
                ],
            )
        )
        or "N/A",
    )
    curr_row += 1

    add_section_header("IV. EDUCATIONAL BACKGROUND")
    add_data_row("Schooling Status:", getattr(jobseeker, "schooling_status", "N/A"))
    add_data_row(
        "Elementary:",
        getattr(jobseeker, "elem_school", "N/A"),
        "Year Graduated:",
        getattr(jobseeker, "elem_year_grad", "N/A"),
    )

    sec_grad = f" ({jobseeker.sec_year_grad})" if getattr(jobseeker, "sec_year_grad", None) else ""
    add_data_row(
        "Secondary:",
        getattr(jobseeker, "sec_school", "N/A"),
        "Strand / Year Grad:",
        f"{getattr(jobseeker, 'senior_high_strand', '')}{sec_grad}".strip() or "N/A",
    )

    tert_grad = f" ({jobseeker.tertiary_year_grad})" if getattr(jobseeker, "tertiary_year_grad", None) else ""
    add_data_row(
        "Tertiary:",
        getattr(jobseeker, "tertiary_school", "N/A"),
        "Course / Year Grad:",
        f"{getattr(jobseeker, 'tertiary_course', '')}{tert_grad}".strip() or "N/A",
    )

    grad_year = f" ({jobseeker.grad_year_grad})" if getattr(jobseeker, "grad_year_grad", None) else ""
    add_data_row(
        "Graduate Studies:",
        getattr(jobseeker, "grad_school", "N/A"),
        "Course / Year Grad:",
        f"{getattr(jobseeker, 'grad_course', '')}{grad_year}".strip() or "N/A",
    )
    curr_row += 1

    add_section_header("V. WORK EXPERIENCES")
    work_histories = getattr(jobseeker, "work_history", [])
    if work_histories:
        ws.cell(row=curr_row, column=1, value="Company Name").font = BOLD
        ws.cell(row=curr_row, column=2, value="Address (City/Municipality)").font = BOLD
        ws.cell(row=curr_row, column=3, value="Position").font = BOLD
        ws.cell(row=curr_row, column=4, value="Number of Months").font = BOLD
        ws.cell(row=curr_row, column=6, value="Status").font = BOLD
        curr_row += 1
        for work in work_histories:
            ws.cell(row=curr_row, column=1, value=getattr(work, "company", None) or "N/A").font = NORMAL
            ws.cell(row=curr_row, column=2, value=getattr(work, "company_address", None) or "N/A").font = NORMAL
            ws.cell(row=curr_row, column=3, value=getattr(work, "position", None) or "N/A").font = NORMAL
            ws.cell(
                row=curr_row,
                column=4,
                value=getattr(work, "number_of_months", None) if getattr(work, "number_of_months", None) is not None else "N/A",
            ).font = NORMAL
            ws.cell(row=curr_row, column=6, value=getattr(work, "appointment_status", None) or "N/A").font = NORMAL
            curr_row += 1
    else:
        add_data_row("Work History:", "No work experience recorded.")
    curr_row += 1

    add_section_header("VI. LICENSES & PROFESSIONAL ELIGIBILITY")
    from website.models import JobSeekerLicense

    licenses = JobSeekerLicense.query.filter_by(
        job_seeker_id=jobseeker.job_seeker_id
    ).all()

    if licenses:
        for lic in licenses:
            if getattr(lic, "eligibility_name", None):
                add_data_row(
                    "Eligibility:",
                    lic.eligibility_name,
                    "Date:",
                    lic.eligibility_date or "N/A",
                )
            if getattr(lic, "license_name", None):
                add_data_row(
                    "License:",
                    lic.license_name,
                    "Validity:",
                    lic.license_validity or "N/A",
                )
    else:
        add_data_row("Licenses/Eligibility:", "None listed.")

    curr_row += 1
    add_section_header("VII. ELECTRONIC CERTIFICATION")
    add_data_row(
        "Applicant:",
        getattr(jobseeker, "full_name", "N/A"),
        "Certification:",
        "Electronically acknowledged" if getattr(jobseeker, "legal_consent", False) and getattr(jobseeker, "privacy_act_consent", False) else "Not acknowledged",
    )
    certified = getattr(jobseeker, "certified_at", None)
    add_data_row(
        "Date/Time:",
        certified.strftime("%Y-%m-%d %H:%M:%S") if certified else "N/A",
        "Record:",
        "Generated through PESO-IMS",
    )

    for col in ws.columns:
        max_len = max(len(str(cell.value or "")) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 16), 45)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


import io
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

def generate_bulk_jsrf_excel(jobseeker_list):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "NSRP Job Seekers Masterlist"
    ws.views.sheetView[0].showGridLines = True

    # Styling
    HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
    HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    NORMAL_FONT = Font(name="Calibri", size=10)
    
    THIN_BORDER = Border(
        left=Side(style='thin', color='D3D3D3'),
        right=Side(style='thin', color='D3D3D3'),
        top=Side(style='thin', color='D3D3D3'),
        bottom=Side(style='thin', color='D3D3D3')
    )

    headers = [
        "Job Seeker ID", "Status", "Full Name", "Sex", "Date of Birth", 
        "Civil Status", "4Ps Beneficiary", "PWD", "Disability", "Senior Citizen", "Contact No", "Email", 
        "Barangay", "City/Municipality", "Province", "Full Address", 
        "Employment Status", "Skills", "Preferred Occupations", 
        "Preferred Locations", "Educational Attainment"
    ]

    # Write Headers
    for col_idx, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws.row_dimensions[1].height = 28

    # Write Data Rows
    for row_idx, seeker in enumerate(jobseeker_list, 2):
        full_name = f"{seeker.last_name}, {seeker.first_name} {seeker.middle_name or ''}".strip()
        full_addr = f"{seeker.street or ''}, {seeker.barangay or ''}, {seeker.city or ''}, {seeker.province or ''}".strip(", ")
        
        pref_jobs = ", ".join(filter(None, [
            getattr(seeker, 'pref_occupation_1', None),
            getattr(seeker, 'pref_occupation_2', None),
            getattr(seeker, 'pref_occupation_3', None)
        ])) or "N/A"

        pref_locs = ", ".join(filter(None, [
            getattr(seeker, 'local_location_1', None),
            getattr(seeker, 'local_location_2', None),
            getattr(seeker, 'overseas_location_1', None)
        ])) or "N/A"

        edu_summary = getattr(seeker, 'tertiary_course', None) or getattr(seeker, 'senior_high_strand', None) or "N/A"

        row_data = [
            f"JS-{seeker.job_seeker_id:06d}",
            "Verified" if getattr(seeker, 'is_verified', False) else "Pending",
            full_name,
            getattr(seeker, 'sex', 'N/A'),
            seeker.birthdate.strftime('%Y-%m-%d') if getattr(seeker, 'birthdate', None) else 'N/A',
            getattr(seeker, 'civil_status', 'N/A'),
            "Yes" if getattr(seeker, 'fourps_beneficiary', False) else "No",
            "Yes" if getattr(seeker, 'is_pwd', False) else "No",
            getattr(seeker, 'disability', None) or 'N/A',
            "Yes" if getattr(seeker, 'is_senior_citizen', False) else "No",
            getattr(seeker, 'contact_number', 'N/A'),
            seeker.user.email if getattr(seeker, 'user', None) else 'N/A',
            getattr(seeker, 'barangay', 'N/A'),
            getattr(seeker, 'city', 'N/A'),
            getattr(seeker, 'province', 'N/A'),
            full_addr or 'N/A',
            getattr(seeker, 'employment_status', 'N/A'),
            getattr(seeker, 'skills', None) or "None listed",
            pref_jobs,
            pref_locs,
            edu_summary
        ]

        for col_idx, val in enumerate(row_data, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=str(val))
            cell.font = NORMAL_FONT
            cell.border = THIN_BORDER
            cell.alignment = Alignment(vertical="center")

    # Auto Column Width
    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 40)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output