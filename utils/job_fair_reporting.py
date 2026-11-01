from website.utils.registration_time import registration_datetime_ph
from collections import Counter
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter


OUTCOME_PRIORITY = {
    "Hired": 4,
    "Nearly-hired": 3,
    "Qualified": 2,
    "Mismatch": 1,
}

OUTCOME_LABELS = {
    "Hired": "Hired on the Spot (HOTS)",
    "Nearly-hired": "Near-hire",
    "Qualified": "Qualified",
    "Mismatch": "Mismatched",
}

SUMMARY_OUTCOME_ORDER = [
    "Hired on the Spot (HOTS)",
    "Near-hire",
    "Qualified",
    "Mismatched",
    "Not Evaluated",
]


def _safe_name(seeker):
    if seeker is None:
        return "N/A"

    full_name = getattr(seeker, "full_name", None)
    if full_name:
        return full_name

    return " ".join(
        part
        for part in [
            getattr(seeker, "first_name", None),
            getattr(seeker, "middle_name", None),
            getattr(seeker, "last_name", None),
        ]
        if part
    ) or "N/A"


def _best_outcome(evaluations):
    statuses = {
        getattr(evaluation, "status", None)
        for evaluation in evaluations
        if getattr(evaluation, "status", None)
    }

    known = [
        status
        for status in statuses
        if status in OUTCOME_PRIORITY
    ]

    if not known:
        return "Not Evaluated"

    best_status = max(
        known,
        key=lambda status: OUTCOME_PRIORITY[status],
    )

    return OUTCOME_LABELS[best_status]


def _collect_applied_jobs(registration):
    """
    Queue creation is the Job Fair application-intent event in PESO-IMS:
    the participant selects/preselects jobs and receives employer queues.

    Return unique job records represented by the participant's queues.
    """
    jobs = {}

    for queue in getattr(registration, "queues", None) or []:
        for grouping in getattr(queue, "job_groupings", None) or []:
            job_fair_job = getattr(grouping, "job_fair_job", None)

            if job_fair_job is None:
                continue

            job = getattr(job_fair_job, "job", None)

            if job is None:
                continue

            job_id = getattr(job, "job_id", None)

            if job_id is None:
                continue

            jobs[job_id] = job

    return list(jobs.values())


def build_job_fair_report(job_fair, registrations):
    participant_rows = []
    evaluation_rows = []

    summary = {
        "total_registered": 0,
        "pre_registered": 0,
        "on_site": 0,
        "attended": 0,
        "registered_waiting": 0,
        "did_not_attend": 0,
        "applied": 0,
        "did_not_apply": 0,
        "hots": 0,
        "near_hire": 0,
        "qualified": 0,
        "mismatched": 0,
        "not_evaluated": 0,
    }

    for registration in registrations:
        seeker = getattr(registration, "job_seeker", None)

        source = (
            getattr(registration, "registration_source", None)
            or "Pre-registered"
        )

        attendance = (
            getattr(registration, "attendance_status", None)
            or "Registered"
        )

        applied_jobs = _collect_applied_jobs(registration)
        applied = bool(applied_jobs)

        evaluations = (
            getattr(registration, "candidate_evaluations", None)
            or []
        )

        best_outcome = (
            _best_outcome(evaluations)
            if applied
            else "Not Applicable"
        )

        participant_rows.append({
            "registration_id": registration.registration_id,
            "reference_id": getattr(registration, "reference_id", None) or "",
            "name": _safe_name(seeker),
            "email": (
                seeker.user.email
                if seeker is not None
                and getattr(seeker, "user", None)
                else ""
            ),
            "contact": (
                getattr(seeker, "contact_number", None)
                if seeker is not None
                else ""
            ) or "",
            "registration_source": source,
            "registration_date": getattr(
                registration,
                "registration_date",
                None,
            ),
            "attendance": attendance,
            "applied": applied,
            "jobs_applied_count": len(applied_jobs),
            "jobs_applied": ", ".join(
                getattr(job, "job_title", "Untitled Job")
                for job in applied_jobs
            ),
            "best_outcome": best_outcome,
        })

        summary["total_registered"] += 1

        if source == "On-site":
            summary["on_site"] += 1
        else:
            summary["pre_registered"] += 1

        # Attendance, application participation, and employer outcome
        # are independent dimensions.
        if attendance == "Attended":
            summary["attended"] += 1
        elif attendance == "Absent":
            summary["did_not_attend"] += 1
        else:
            summary["registered_waiting"] += 1

        if applied:
            summary["applied"] += 1

            if best_outcome == "Hired on the Spot (HOTS)":
                summary["hots"] += 1
            elif best_outcome == "Near-hire":
                summary["near_hire"] += 1
            elif best_outcome == "Qualified":
                summary["qualified"] += 1
            elif best_outcome == "Mismatched":
                summary["mismatched"] += 1
            elif best_outcome == "Not Evaluated":
                summary["not_evaluated"] += 1

        if attendance == "Attended" and not applied:
            summary["did_not_apply"] += 1

        for evaluation in evaluations:
            employer_registration = getattr(
                evaluation,
                "employer_registration",
                None,
            )

            employer = (
                getattr(employer_registration, "employer", None)
                if employer_registration
                else None
            )

            job_fair_job = getattr(
                evaluation,
                "job_fair_job",
                None,
            )

            job = (
                getattr(job_fair_job, "job", None)
                if job_fair_job
                else None
            )

            raw_status = getattr(evaluation, "status", None) or ""

            evaluation_rows.append({
                "reference_id": getattr(
                    registration,
                    "reference_id",
                    None,
                ) or "",
                "jobseeker": _safe_name(seeker),
                "employer": (
                    getattr(employer, "company_name", None)
                    if employer
                    else ""
                ) or "",
                "booth": (
                    getattr(
                        employer_registration,
                        "booth_number",
                        None,
                    )
                    if employer_registration
                    else ""
                ) or "",
                "job": (
                    getattr(job, "job_title", None)
                    if job
                    else ""
                ) or "",
                "evaluation": OUTCOME_LABELS.get(
                    raw_status,
                    raw_status or "Not Evaluated",
                ),
                "remarks": getattr(evaluation, "remarks", None) or "",
                "evaluated_at": getattr(
                    evaluation,
                    "updated_at",
                    None,
                ) or getattr(
                    evaluation,
                    "created_at",
                    None,
                ),
            })

    return {
        "job_fair": job_fair,
        "summary": summary,
        "participants": participant_rows,
        "evaluations": evaluation_rows,
    }


# ============================================================
# EXCEL STYLING
# ============================================================

HEADER_FILL = PatternFill(
    start_color="1F4E78",
    end_color="1F4E78",
    fill_type="solid",
)

SUBHEADER_FILL = PatternFill(
    start_color="D9EAF7",
    end_color="D9EAF7",
    fill_type="solid",
)

WHITE_BOLD = Font(
    name="Calibri",
    size=11,
    bold=True,
    color="FFFFFF",
)

TITLE_FONT = Font(
    name="Calibri",
    size=14,
    bold=True,
    color="1F4E78",
)

SECTION_FONT = Font(
    name="Calibri",
    size=11,
    bold=True,
    color="FFFFFF",
)

BOLD = Font(
    name="Calibri",
    size=10,
    bold=True,
)

NORMAL = Font(
    name="Calibri",
    size=10,
)

MUTED = Font(
    name="Calibri",
    size=9,
    italic=True,
    color="666666",
)

THIN_BORDER = Border(
    left=Side(style="thin", color="D3D3D3"),
    right=Side(style="thin", color="D3D3D3"),
    top=Side(style="thin", color="D3D3D3"),
    bottom=Side(style="thin", color="D3D3D3"),
)


def _style_title(ws, title, end_column):
    ws.merge_cells(
        start_row=1,
        start_column=1,
        end_row=1,
        end_column=end_column,
    )

    cell = ws.cell(
        row=1,
        column=1,
        value=title,
    )

    cell.font = TITLE_FONT
    cell.alignment = Alignment(
        horizontal="center",
        vertical="center",
    )

    ws.row_dimensions[1].height = 24


def _style_subtitle(ws, subtitle, row, end_column):
    ws.merge_cells(
        start_row=row,
        start_column=1,
        end_row=row,
        end_column=end_column,
    )

    cell = ws.cell(
        row=row,
        column=1,
        value=subtitle,
    )

    cell.font = Font(
        name="Calibri",
        size=10,
        italic=True,
        color="475569",
    )

    cell.alignment = Alignment(
        horizontal="center",
        vertical="center",
    )


def _style_section_header(
    ws,
    row,
    title,
    end_column,
):
    ws.merge_cells(
        start_row=row,
        start_column=1,
        end_row=row,
        end_column=end_column,
    )

    cell = ws.cell(
        row=row,
        column=1,
        value=title,
    )

    cell.fill = HEADER_FILL
    cell.font = SECTION_FONT
    cell.alignment = Alignment(
        horizontal="left",
        vertical="center",
        indent=1,
    )

    for column in range(1, end_column + 1):
        ws.cell(row=row, column=column).border = THIN_BORDER

    ws.row_dimensions[row].height = 20


def _style_table_header(
    ws,
    row,
    start_column,
    end_column,
):
    for column in range(
        start_column,
        end_column + 1,
    ):
        cell = ws.cell(
            row=row,
            column=column,
        )

        cell.fill = HEADER_FILL
        cell.font = WHITE_BOLD
        cell.border = THIN_BORDER
        cell.alignment = Alignment(
            horizontal="center",
            vertical="center",
            wrap_text=True,
        )

    ws.row_dimensions[row].height = 30


def _style_table_body(
    ws,
    start_row,
    end_row,
    start_column,
    end_column,
):
    if end_row < start_row:
        return

    for row in range(
        start_row,
        end_row + 1,
    ):
        for column in range(
            start_column,
            end_column + 1,
        ):
            cell = ws.cell(
                row=row,
                column=column,
            )

            cell.font = NORMAL
            cell.border = THIN_BORDER
            cell.alignment = Alignment(
                vertical="top",
                wrap_text=True,
            )


def _add_label_value_row(
    ws,
    row,
    pairs,
    end_column=6,
):
    """
    Add up to three label/value pairs across six columns.
    Format mirrors the JSRF export layout.
    """
    column = 1

    for label, value in pairs:
        if column > end_column:
            break

        label_cell = ws.cell(
            row=row,
            column=column,
            value=label,
        )

        value_cell = ws.cell(
            row=row,
            column=column + 1,
            value="N/A" if value in (None, "") else value,
        )

        label_cell.font = BOLD
        value_cell.font = NORMAL

        label_cell.alignment = Alignment(
            vertical="center",
        )

        value_cell.alignment = Alignment(
            vertical="center",
            wrap_text=True,
        )

        column += 2

    for current_column in range(
        1,
        end_column + 1,
    ):
        ws.cell(
            row=row,
            column=current_column,
        ).border = THIN_BORDER


def _fit_columns(
    ws,
    min_width=12,
    max_width=42,
):
    for column_cells in ws.columns:
        column_letter = get_column_letter(
            column_cells[0].column
        )

        max_length = 0

        for cell in column_cells:
            if cell.coordinate in ws.merged_cells:
                continue

            value = (
                ""
                if cell.value is None
                else str(cell.value)
            )

            max_length = max(
                max_length,
                len(value),
            )

        ws.column_dimensions[
            column_letter
        ].width = min(
            max(max_length + 3, min_width),
            max_width,
        )


def _set_print_layout(
    ws,
    orientation="portrait",
):
    ws.sheet_view.showGridLines = True
    ws.page_setup.orientation = orientation
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True

    ws.page_margins.left = 0.3
    ws.page_margins.right = 0.3
    ws.page_margins.top = 0.5
    ws.page_margins.bottom = 0.5


# ============================================================
# EXCEL GENERATOR
# ============================================================

def generate_job_fair_report_excel(report):
    wb = Workbook()

    job_fair = report["job_fair"]
    summary = report["summary"]

    fair_title = getattr(job_fair, "title", None) or "Job Fair"
    venue = getattr(job_fair, "venue", None) or "N/A"
    start_datetime = getattr(job_fair, "start_datetime", None)
    end_datetime = getattr(job_fair, "end_datetime", None)

    event_date = start_datetime.strftime("%B %d, %Y") if start_datetime else "N/A"
    event_schedule = "N/A"

    if start_datetime:
        event_schedule = start_datetime.strftime("%B %d, %Y %I:%M %p")
        if end_datetime:
            event_schedule += " - " + end_datetime.strftime("%B %d, %Y %I:%M %p")

    ws = wb.active
    ws.title = "Job Fair Summary"

    _style_title(
        ws,
        "PUBLIC EMPLOYMENT SERVICE OFFICE (PESO) - JOB FAIR REGISTRATION REPORT",
        6,
    )
    _style_subtitle(ws, fair_title, 2, 6)

    current_row = 4

    _style_section_header(ws, current_row, "I. EVENT INFORMATION", 6)
    current_row += 1

    _add_label_value_row(
        ws,
        current_row,
        [
            ("Job Fair:", fair_title),
            ("Event Date:", event_date),
            ("Venue:", venue),
        ],
    )
    current_row += 1

    _add_label_value_row(
        ws,
        current_row,
        [
            ("Schedule:", event_schedule),
            ("Event Type:", getattr(job_fair, "event_type", None) or "N/A"),
            ("Status:", getattr(job_fair, "status", None) or "N/A"),
        ],
    )
    current_row += 2

    _style_section_header(ws, current_row, "II. REGISTRATION SUMMARY", 6)
    current_row += 1

    _add_label_value_row(
        ws,
        current_row,
        [
            ("Total Registered:", summary["total_registered"]),
            ("Pre-registered:", summary["pre_registered"]),
            ("On-site / Walk-in:", summary["on_site"]),
        ],
    )
    current_row += 2

    _style_section_header(ws, current_row, "III. ATTENDANCE AND APPLICATION PARTICIPATION", 6)
    current_row += 1

    _add_label_value_row(
        ws,
        current_row,
        [
            ("Attended / Pumunta:", summary["attended"]),
            ("Registered / Waiting:", summary["registered_waiting"]),
            ("Applied:", summary["applied"]),
        ],
    )
    current_row += 1

    _add_label_value_row(
        ws,
        current_row,
        [
            ("Absent / Did Not Attend:", summary["did_not_attend"]),
            ("Attended, Did Not Apply:", summary["did_not_apply"]),
            ("Applied but Not Evaluated:", summary["not_evaluated"]),
        ],
    )
    current_row += 2

    _style_section_header(ws, current_row, "IV. EMPLOYMENT OUTCOMES", 6)
    current_row += 1

    _add_label_value_row(
        ws,
        current_row,
        [
            ("Hired on the Spot (HOTS):", summary["hots"]),
            ("Near-hire:", summary["near_hire"]),
            ("Qualified:", summary["qualified"]),
        ],
    )
    current_row += 1

    _add_label_value_row(
        ws,
        current_row,
        [
            ("Mismatched:", summary["mismatched"]),
        ],
    )
    current_row += 2

    ws.merge_cells(
        start_row=current_row,
        start_column=1,
        end_row=current_row,
        end_column=6,
    )

    note_cell = ws.cell(
        row=current_row,
        column=1,
        value="Note: Employment outcome totals use each participant's best recorded Job Fair evaluation.",
    )
    note_cell.font = MUTED
    note_cell.alignment = Alignment(wrap_text=True)

    ws.freeze_panes = "A4"

    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 24
    ws.column_dimensions["C"].width = 24
    ws.column_dimensions["D"].width = 24
    ws.column_dimensions["E"].width = 24
    ws.column_dimensions["F"].width = 24

    _set_print_layout(ws, "portrait")

    ws = wb.create_sheet("Participant Details")

    participant_headers = [
        "Registration ID",
        "Reference ID",
        "Job Seeker",
        "Email",
        "Contact Number",
        "Registration Type",
        "Registration Date",
        "Attendance",
        "Applied?",
        "No. of Jobs Applied",
        "Jobs Applied",
        "Best Outcome",
    ]
    participant_columns = len(participant_headers)

    _style_title(ws, "PESO JOB FAIR - PARTICIPANT DETAILS", participant_columns)
    _style_subtitle(ws, fair_title, 2, participant_columns)

    ws.merge_cells(
        start_row=3,
        start_column=1,
        end_row=3,
        end_column=participant_columns,
    )

    event_info_cell = ws.cell(
        row=3,
        column=1,
        value=f"Venue: {venue}  |  Event Date: {event_date}",
    )
    event_info_cell.font = MUTED
    event_info_cell.alignment = Alignment(horizontal="center", vertical="center")

    header_row = 5

    for column_index, header in enumerate(participant_headers, 1):
        ws.cell(row=header_row, column=column_index, value=header)

    _style_table_header(ws, header_row, 1, participant_columns)

    data_start_row = header_row + 1
    current_row = data_start_row

    for participant in report["participants"]:
        registration_date = participant["registration_date"]

        row_data = [
            participant["registration_id"],
            participant["reference_id"],
            participant["name"],
            participant["email"],
            participant["contact"],
            participant["registration_source"],
            (
                registration_datetime_ph(registration_date).strftime("%Y-%m-%d %I:%M %p")
                if registration_date
                else "N/A"
            ),
            participant["attendance"],
            "Yes" if participant["applied"] else "No",
            participant["jobs_applied_count"],
            participant["jobs_applied"] or "N/A",
            participant["best_outcome"],
        ]

        for column_index, value in enumerate(row_data, 1):
            ws.cell(row=current_row, column=column_index, value=value)

        current_row += 1

    data_end_row = current_row - 1

    _style_table_body(ws, data_start_row, data_end_row, 1, participant_columns)

    if data_end_row >= data_start_row:
        ws.auto_filter.ref = (
            f"A{header_row}:{get_column_letter(participant_columns)}{data_end_row}"
        )

    ws.freeze_panes = f"A{data_start_row}"

    _fit_columns(ws, min_width=13, max_width=38)

    ws.column_dimensions["C"].width = 24
    ws.column_dimensions["D"].width = 28
    ws.column_dimensions["K"].width = 38
    ws.column_dimensions["L"].width = 26

    _set_print_layout(ws, "landscape")

    ws = wb.create_sheet("Employer Evaluations")

    evaluation_headers = [
        "Reference ID",
        "Job Seeker",
        "Employer",
        "Booth",
        "Position",
        "Evaluation",
        "Remarks",
        "Evaluated At",
    ]
    evaluation_columns = len(evaluation_headers)

    _style_title(ws, "PESO JOB FAIR - EMPLOYER EVALUATION DETAILS", evaluation_columns)
    _style_subtitle(ws, fair_title, 2, evaluation_columns)

    ws.merge_cells(
        start_row=3,
        start_column=1,
        end_row=3,
        end_column=evaluation_columns,
    )

    evaluation_info = ws.cell(
        row=3,
        column=1,
        value=f"Venue: {venue}  |  Event Date: {event_date}",
    )
    evaluation_info.font = MUTED
    evaluation_info.alignment = Alignment(horizontal="center", vertical="center")

    header_row = 5

    for column_index, header in enumerate(evaluation_headers, 1):
        ws.cell(row=header_row, column=column_index, value=header)

    _style_table_header(ws, header_row, 1, evaluation_columns)

    data_start_row = header_row + 1
    current_row = data_start_row

    for evaluation in report["evaluations"]:
        evaluated_at = evaluation["evaluated_at"]

        row_data = [
            evaluation["reference_id"],
            evaluation["jobseeker"],
            evaluation["employer"],
            evaluation["booth"],
            evaluation["job"],
            evaluation["evaluation"],
            evaluation["remarks"] or "N/A",
            (
                evaluated_at.strftime("%Y-%m-%d %I:%M %p")
                if evaluated_at
                else "N/A"
            ),
        ]

        for column_index, value in enumerate(row_data, 1):
            ws.cell(row=current_row, column=column_index, value=value)

        current_row += 1

    data_end_row = current_row - 1

    _style_table_body(ws, data_start_row, data_end_row, 1, evaluation_columns)

    if data_end_row >= data_start_row:
        ws.auto_filter.ref = (
            f"A{header_row}:{get_column_letter(evaluation_columns)}{data_end_row}"
        )

    ws.freeze_panes = f"A{data_start_row}"

    _fit_columns(ws, min_width=13, max_width=38)

    ws.column_dimensions["B"].width = 24
    ws.column_dimensions["C"].width = 28
    ws.column_dimensions["E"].width = 26
    ws.column_dimensions["G"].width = 38

    _set_print_layout(ws, "landscape")

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    return output

# ============================================================
# EMPLOYER / BOOTH REPORT
# ============================================================

def build_job_fair_employer_report(
    job_fair,
    employer_registrations,
):
    """
    Build the Admin Employer & Booth report independently from
    the Job Fair attendee/registration report.
    """

    employer_rows = []
    vacancy_rows = []

    summary = {
        "total_employer_applications": 0,
        "approved_employers": 0,
        "pending_employers": 0,
        "rejected_employers": 0,
        "cancelled_employers": 0,
        "assigned_booths": 0,
        "unassigned_booths": 0,
        "total_representatives": 0,
        "total_submitted_jobs": 0,
        "approved_jobs": 0,
        "pending_jobs": 0,
        "rejected_jobs": 0,
        "flagged_jobs": 0,
        "referral_jobs": 0,
        "job_fair_exclusive_jobs": 0,
        "total_vacancies": 0,
    }

    for registration in employer_registrations:
        employer = getattr(
            registration,
            "employer",
            None,
        )

        company_name = (
            getattr(employer, "company_name", None)
            if employer
            else None
        ) or "N/A"

        company_email = (
            getattr(employer, "company_email", None)
            if employer
            else None
        )

        if (
            not company_email
            and employer
            and getattr(employer, "user", None)
        ):
            company_email = employer.user.email

        company_phone = (
            getattr(employer, "company_phone", None)
            if employer
            else None
        ) or "N/A"

        registration_status = (
            getattr(registration, "status", None)
            or "Pending"
        )

        booth_number = (
            getattr(registration, "booth_number", None)
            or "Unassigned"
        )

        representatives = int(
            getattr(
                registration,
                "representatives",
                0,
            )
            or 0
        )

        special_requests = (
            getattr(
                registration,
                "special_requests",
                None,
            )
            or "None"
        )

        selected_jobs = [
            entry.job
            for entry in (
                getattr(
                    registration,
                    "selected_jobs",
                    None,
                )
                or []
            )
            if getattr(entry, "job", None) is not None
        ]

        approved_count = 0
        pending_count = 0
        rejected_count = 0
        flagged_count = 0
        total_vacancies = 0

        job_titles = []

        for job in selected_jobs:
            approval_status = (
                getattr(
                    job,
                    "approval_status",
                    None,
                )
                or "Pending"
            )

            job_type = (
                getattr(
                    job,
                    "job_type",
                    None,
                )
                or "Referral"
            )

            vacancies = int(
                getattr(
                    job,
                    "vacancies",
                    0,
                )
                or 0
            )

            total_vacancies += vacancies

            if approval_status == "Approved":
                approved_count += 1
                summary["approved_jobs"] += 1

            elif approval_status == "Rejected":
                rejected_count += 1
                summary["rejected_jobs"] += 1

            elif approval_status == "Flagged":
                flagged_count += 1
                summary["flagged_jobs"] += 1

            else:
                pending_count += 1
                summary["pending_jobs"] += 1

            if job_type == "Job Fair":
                summary["job_fair_exclusive_jobs"] += 1
                display_job_type = "Job Fair Exclusive"
            else:
                summary["referral_jobs"] += 1
                display_job_type = "Referral Job"

            job_title = (
                getattr(
                    job,
                    "job_title",
                    None,
                )
                or "Untitled Job"
            )

            job_titles.append(job_title)

            vacancy_rows.append({
                "registration_id":
                    registration.registration_id,
                "company_name":
                    company_name,
                "booth_number":
                    booth_number,
                "job_id":
                    getattr(
                        job,
                        "job_id",
                        None,
                    ),
                "job_title":
                    job_title,
                "job_type":
                    display_job_type,
                "approval_status":
                    approval_status,
                "job_status":
                    getattr(
                        job,
                        "status",
                        None,
                    )
                    or "N/A",
                "vacancies":
                    vacancies,
                "deadline":
                    getattr(
                        job,
                        "deadline",
                        None,
                    ),
            })

        summary["total_employer_applications"] += 1
        summary["total_representatives"] += representatives
        summary["total_submitted_jobs"] += len(
            selected_jobs
        )
        summary["total_vacancies"] += total_vacancies

        if registration_status == "Approved":
            summary["approved_employers"] += 1
        elif registration_status == "Rejected":
            summary["rejected_employers"] += 1
        elif registration_status == "Cancelled":
            summary["cancelled_employers"] += 1
        else:
            summary["pending_employers"] += 1

        if booth_number == "Unassigned":
            summary["unassigned_booths"] += 1
        else:
            summary["assigned_booths"] += 1

        employer_rows.append({
            "registration_id":
                registration.registration_id,
            "company_name":
                company_name,
            "company_email":
                company_email or "N/A",
            "company_phone":
                company_phone,
            "registration_date":
                getattr(
                    registration,
                    "registration_date",
                    None,
                ),
            "registration_status":
                registration_status,
            "booth_number":
                booth_number,
            "representatives":
                representatives,
            "special_requests":
                special_requests,
            "submitted_jobs":
                len(selected_jobs),
            "approved_jobs":
                approved_count,
            "pending_jobs":
                pending_count,
            "rejected_jobs":
                rejected_count,
            "flagged_jobs":
                flagged_count,
            "total_vacancies":
                total_vacancies,
            "job_titles":
                ", ".join(job_titles)
                if job_titles
                else "None",
        })

    return {
        "job_fair": job_fair,
        "summary": summary,
        "employers": employer_rows,
        "vacancies": vacancy_rows,
    }


def generate_job_fair_employer_report_excel(
    report,
):
    """
    Generate an Employer & Booth workbook using the same visual
    language as the JSRF and Job Fair registration exports.
    """

    wb = Workbook()

    job_fair = report["job_fair"]
    summary = report["summary"]

    fair_title = (
        getattr(
            job_fair,
            "title",
            None,
        )
        or "Job Fair"
    )

    venue = (
        getattr(
            job_fair,
            "venue",
            None,
        )
        or "N/A"
    )

    start_datetime = getattr(
        job_fair,
        "start_datetime",
        None,
    )

    end_datetime = getattr(
        job_fair,
        "end_datetime",
        None,
    )

    event_date = (
        start_datetime.strftime(
            "%B %d, %Y"
        )
        if start_datetime
        else "N/A"
    )

    event_schedule = "N/A"

    if start_datetime:
        event_schedule = start_datetime.strftime(
            "%B %d, %Y %I:%M %p"
        )

        if end_datetime:
            event_schedule += (
                " - "
                + end_datetime.strftime(
                    "%B %d, %Y %I:%M %p"
                )
            )

    # ========================================================
    # SHEET 1 — EMPLOYER SUMMARY
    # ========================================================
    ws = wb.active
    ws.title = "Employer Summary"

    _style_title(
        ws,
        (
            "PUBLIC EMPLOYMENT SERVICE OFFICE (PESO) - "
            "JOB FAIR EMPLOYER & BOOTH REPORT"
        ),
        6,
    )

    _style_subtitle(
        ws,
        fair_title,
        2,
        6,
    )

    current_row = 4

    _style_section_header(
        ws,
        current_row,
        "I. EVENT INFORMATION",
        6,
    )

    current_row += 1

    _add_label_value_row(
        ws,
        current_row,
        [
            ("Job Fair:", fair_title),
            ("Event Date:", event_date),
            ("Venue:", venue),
        ],
    )

    current_row += 1

    _add_label_value_row(
        ws,
        current_row,
        [
            ("Schedule:", event_schedule),
            (
                "Event Type:",
                getattr(
                    job_fair,
                    "event_type",
                    None,
                )
                or "N/A",
            ),
            (
                "Status:",
                getattr(
                    job_fair,
                    "status",
                    None,
                )
                or "N/A",
            ),
        ],
    )

    current_row += 2

    _style_section_header(
        ws,
        current_row,
        "II. EMPLOYER REGISTRATION SUMMARY",
        6,
    )

    current_row += 1

    _add_label_value_row(
        ws,
        current_row,
        [
            (
                "Total Applications:",
                summary[
                    "total_employer_applications"
                ],
            ),
            (
                "Approved:",
                summary[
                    "approved_employers"
                ],
            ),
            (
                "Pending:",
                summary[
                    "pending_employers"
                ],
            ),
        ],
    )

    current_row += 1

    _add_label_value_row(
        ws,
        current_row,
        [
            (
                "Rejected:",
                summary[
                    "rejected_employers"
                ],
            ),
            (
                "Cancelled:",
                summary[
                    "cancelled_employers"
                ],
            ),
            (
                "Representatives:",
                summary[
                    "total_representatives"
                ],
            ),
        ],
    )

    current_row += 2

    _style_section_header(
        ws,
        current_row,
        "III. BOOTH ASSIGNMENT SUMMARY",
        6,
    )

    current_row += 1

    _add_label_value_row(
        ws,
        current_row,
        [
            (
                "Assigned Booths:",
                summary[
                    "assigned_booths"
                ],
            ),
            (
                "Unassigned Booths:",
                summary[
                    "unassigned_booths"
                ],
            ),
        ],
    )

    current_row += 2

    _style_section_header(
        ws,
        current_row,
        "IV. JOB VACANCY SUMMARY",
        6,
    )

    current_row += 1

    _add_label_value_row(
        ws,
        current_row,
        [
            (
                "Submitted Jobs:",
                summary[
                    "total_submitted_jobs"
                ],
            ),
            (
                "Approved Jobs:",
                summary[
                    "approved_jobs"
                ],
            ),
            (
                "Pending Jobs:",
                summary[
                    "pending_jobs"
                ],
            ),
        ],
    )

    current_row += 1

    _add_label_value_row(
        ws,
        current_row,
        [
            (
                "Rejected Jobs:",
                summary[
                    "rejected_jobs"
                ],
            ),
            (
                "Flagged Jobs:",
                summary[
                    "flagged_jobs"
                ],
            ),
            (
                "Total Vacancies:",
                summary[
                    "total_vacancies"
                ],
            ),
        ],
    )

    current_row += 1

    _add_label_value_row(
        ws,
        current_row,
        [
            (
                "Referral Jobs:",
                summary[
                    "referral_jobs"
                ],
            ),
            (
                "Job Fair Exclusive:",
                summary[
                    "job_fair_exclusive_jobs"
                ],
            ),
        ],
    )

    ws.freeze_panes = "A4"

    for column in (
        "A",
        "B",
        "C",
        "D",
        "E",
        "F",
    ):
        ws.column_dimensions[
            column
        ].width = 24

    _set_print_layout(
        ws,
        "portrait",
    )

    # ========================================================
    # SHEET 2 — EMPLOYERS & BOOTHS
    # ========================================================
    ws = wb.create_sheet(
        "Employers & Booths"
    )

    headers = [
        "Registration ID",
        "Company",
        "Company Email",
        "Company Phone",
        "Registration Date",
        "Registration Status",
        "Booth Number",
        "Representatives",
        "Special Requests",
        "Submitted Jobs",
        "Approved Jobs",
        "Pending Jobs",
        "Rejected Jobs",
        "Flagged Jobs",
        "Total Vacancies",
        "Job Titles",
    ]

    column_count = len(headers)

    _style_title(
        ws,
        "PESO JOB FAIR - EMPLOYERS & BOOTHS",
        column_count,
    )

    _style_subtitle(
        ws,
        fair_title,
        2,
        column_count,
    )

    ws.merge_cells(
        start_row=3,
        start_column=1,
        end_row=3,
        end_column=column_count,
    )

    info_cell = ws.cell(
        row=3,
        column=1,
        value=(
            f"Venue: {venue}  |  "
            f"Event Date: {event_date}"
        ),
    )

    info_cell.font = MUTED
    info_cell.alignment = Alignment(
        horizontal="center",
        vertical="center",
    )

    header_row = 5

    for index, header in enumerate(
        headers,
        1,
    ):
        ws.cell(
            row=header_row,
            column=index,
            value=header,
        )

    _style_table_header(
        ws,
        header_row,
        1,
        column_count,
    )

    current_row = header_row + 1
    data_start = current_row

    for employer in report["employers"]:
        registration_date = employer[
            "registration_date"
        ]

        row_data = [
            employer[
                "registration_id"
            ],
            employer[
                "company_name"
            ],
            employer[
                "company_email"
            ],
            employer[
                "company_phone"
            ],
            (
                registration_date.strftime(
                    "%Y-%m-%d %I:%M %p"
                )
                if registration_date
                else "N/A"
            ),
            employer[
                "registration_status"
            ],
            employer[
                "booth_number"
            ],
            employer[
                "representatives"
            ],
            employer[
                "special_requests"
            ],
            employer[
                "submitted_jobs"
            ],
            employer[
                "approved_jobs"
            ],
            employer[
                "pending_jobs"
            ],
            employer[
                "rejected_jobs"
            ],
            employer[
                "flagged_jobs"
            ],
            employer[
                "total_vacancies"
            ],
            employer[
                "job_titles"
            ],
        ]

        for index, value in enumerate(
            row_data,
            1,
        ):
            ws.cell(
                row=current_row,
                column=index,
                value=value,
            )

        current_row += 1

    data_end = current_row - 1

    _style_table_body(
        ws,
        data_start,
        data_end,
        1,
        column_count,
    )

    if data_end >= data_start:
        ws.auto_filter.ref = (
            f"A{header_row}:"
            f"{get_column_letter(column_count)}"
            f"{data_end}"
        )

    ws.freeze_panes = (
        f"A{data_start}"
    )

    _fit_columns(
        ws,
        min_width=12,
        max_width=36,
    )

    ws.column_dimensions["B"].width = 28
    ws.column_dimensions["C"].width = 30
    ws.column_dimensions["I"].width = 32
    ws.column_dimensions["P"].width = 38

    _set_print_layout(
        ws,
        "landscape",
    )

    # ========================================================
    # SHEET 3 — SUBMITTED VACANCIES
    # ========================================================
    ws = wb.create_sheet(
        "Submitted Vacancies"
    )

    vacancy_headers = [
        "Employer Registration ID",
        "Company",
        "Booth",
        "Job ID",
        "Job Title",
        "Job Type",
        "Approval Status",
        "Job Status",
        "Vacancies",
        "Deadline",
    ]

    vacancy_columns = len(
        vacancy_headers
    )

    _style_title(
        ws,
        "PESO JOB FAIR - SUBMITTED JOB VACANCIES",
        vacancy_columns,
    )

    _style_subtitle(
        ws,
        fair_title,
        2,
        vacancy_columns,
    )

    ws.merge_cells(
        start_row=3,
        start_column=1,
        end_row=3,
        end_column=vacancy_columns,
    )

    vacancy_info = ws.cell(
        row=3,
        column=1,
        value=(
            f"Venue: {venue}  |  "
            f"Event Date: {event_date}"
        ),
    )

    vacancy_info.font = MUTED
    vacancy_info.alignment = Alignment(
        horizontal="center",
        vertical="center",
    )

    header_row = 5

    for index, header in enumerate(
        vacancy_headers,
        1,
    ):
        ws.cell(
            row=header_row,
            column=index,
            value=header,
        )

    _style_table_header(
        ws,
        header_row,
        1,
        vacancy_columns,
    )

    current_row = header_row + 1
    data_start = current_row

    for vacancy in report["vacancies"]:
        deadline = vacancy[
            "deadline"
        ]

        row_data = [
            vacancy[
                "registration_id"
            ],
            vacancy[
                "company_name"
            ],
            vacancy[
                "booth_number"
            ],
            vacancy[
                "job_id"
            ],
            vacancy[
                "job_title"
            ],
            vacancy[
                "job_type"
            ],
            vacancy[
                "approval_status"
            ],
            vacancy[
                "job_status"
            ],
            vacancy[
                "vacancies"
            ],
            (
                deadline.strftime(
                    "%Y-%m-%d"
                )
                if deadline
                else "N/A"
            ),
        ]

        for index, value in enumerate(
            row_data,
            1,
        ):
            ws.cell(
                row=current_row,
                column=index,
                value=value,
            )

        current_row += 1

    data_end = current_row - 1

    _style_table_body(
        ws,
        data_start,
        data_end,
        1,
        vacancy_columns,
    )

    if data_end >= data_start:
        ws.auto_filter.ref = (
            f"A{header_row}:"
            f"{get_column_letter(vacancy_columns)}"
            f"{data_end}"
        )

    ws.freeze_panes = (
        f"A{data_start}"
    )

    _fit_columns(
        ws,
        min_width=12,
        max_width=34,
    )

    ws.column_dimensions["B"].width = 28
    ws.column_dimensions["E"].width = 30

    _set_print_layout(
        ws,
        "landscape",
    )

    output = BytesIO()

    wb.save(output)
    output.seek(0)

    return output

