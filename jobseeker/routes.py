from sqlalchemy.exc import IntegrityError
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo
from itertools import zip_longest
from flask import render_template, redirect, url_for, flash, request, current_app, send_from_directory, jsonify, abort, make_response
from flask_login import login_required, current_user
import os
import uuid
import re
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from sqlalchemy import func
from website.models import User
from website.utils.email import send_email, is_deliverable_email
from website.extensions import db
from website.utils.application_state import sync_jobseeker_employment_status
from website.models import (
     JobSeeker, Job, Application, Announcement, 
     Referral, Notification, JobSeekerLanguage, 
     JobFairRegistration, JobSeekerLicense,
     JobSeekerTVET, JobSeekerWorkHistory, JobFair
)
from website.utils.notifications import notify_admins, notify_user, commit_notifications
from website.utils.audit import record_audit
from website.utils.job_fair_lifecycle import is_registration_open
from website.utils.referral_flow import validate_referral_request
from website.utils.qrcode_gen import generate_qr_code
from website.utils.decorators import role_required
from werkzeug.utils import secure_filename
from website.utils.uploads import (
    save_validated_image,
    save_validated_resume,
)
from .forms import (
    NSRPRegistrationForm,
    ProfilePictureForm,
    ProfessionalSummaryForm,
    PersonalInformationForm,
    JobPreferencesForm,
    WorkExperienceForm,
    ResumeUploadForm,
    EducationForm,
    SkillsForm
)
from . import jobseeker
from flask import send_file
from .nsrp_pdf import generate_nsrp_pdf


ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}
ALLOWED_RESUME_EXTENSIONS = {'pdf', 'doc', 'docx'}

def allowed_file(filename, allowed_extensions=None):
    """Checks if the uploaded file has a permitted extension."""
    if allowed_extensions is None:
        allowed_extensions = ALLOWED_IMAGE_EXTENSIONS | ALLOWED_RESUME_EXTENSIONS
    
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in allowed_extensions
           
def format_work_date(value):
    if not value:
        return None

    if hasattr(value, "strftime"):
        return value.strftime("%B %d, %Y")

    try:
        parsed = datetime.strptime(str(value), "%Y-%m-%d")
        return parsed.strftime("%B %d, %Y")
    except (ValueError, TypeError):
        return str(value)


@jobseeker.before_request
@login_required
@role_required("jobseeker")
def check_verification():
    """Ensure unverified jobseekers complete the JSRF first."""
    if current_user.role == "jobseeker":
        allowed_endpoints = ["jobseeker.jsrf", "static", "auth.logout"]
        if request.endpoint in allowed_endpoints:
            return

        seeker = JobSeeker.query.filter_by(user_id=current_user.user_id).first()
        # A completed JSRF is sufficient to enter jobseeker pages. Formal PESO
        # verification remains a separate status and is not changed here.
        jsrf_completed = bool(seeker and (seeker.jsrf_completed or seeker.is_verified))

        if not jsrf_completed:
            # Do not enqueue a flash on a navigation guard: repeated redirects
            # otherwise accumulate identical session messages.
            return redirect(url_for("jobseeker.jsrf"))


@jobseeker.route('/jsrf', methods=['GET', 'POST'])
@login_required
@role_required("jobseeker")
def jsrf():
    seeker = JobSeeker.query.filter_by(user_id=current_user.user_id).first()
    
    # Initialize seeker profile record if missing
    if not seeker:
        seeker = JobSeeker(user_id=current_user.user_id)
        db.session.add(seeker)
        db.session.commit()

    # If the user has already completed the form or is verified, bypass JSRF and redirect to dashboard
    if getattr(seeker, 'jsrf_completed', False) or getattr(seeker, 'is_verified', False):
        return redirect(url_for('jobseeker.dashboard'))

    # Strict check to see if this is genuinely a fresh account with no saved draft content
    is_fresh_account = not seeker.is_verified and not seeker.last_name and not seeker.street and not seeker.employment_status

    if request.method == 'GET':
        if is_fresh_account:
            # Force an absolute clean slate for WTForms
            form = NSRPRegistrationForm(formdata=None, obj=None)
            form.process(formdata=None, obj=None)
            
            # Reset every single form field explicitly to None / blank
            for field in form:
                if field.type in ['RadioField', 'SelectField', 'SelectMultipleField']:
                    field.data = None
                elif field.type in ['StringField', 'TextAreaField', 'DateField', 'IntegerField', 'FloatField']:
                    field.data = ""
        else:
            # Pre-fill with database draft for returning accounts
            form = NSRPRegistrationForm(obj=seeker)
    else:
        # POST Request handling
        form = NSRPRegistrationForm(request.form)

    if request.method == 'POST':
        try:
            # 1. Update single-entry fields on JobSeeker model (saves current draft progress)
            seeker.last_name = form.last_name.data
            seeker.first_name = form.first_name.data
            seeker.middle_name = form.middle_name.data
            seeker.suffix = form.suffix.data
            seeker.birthdate = form.birthdate.data
            seeker.religion = form.religion.data
            seeker.tin = form.tin.data
            seeker.height = form.height.data
            seeker.civil_status = form.civil_status.data
            seeker.sex = form.sex.data
            
            # Address & Contact
            seeker.street = form.street.data
            seeker.barangay = form.barangay.data
            seeker.city = form.city.data
            seeker.province = form.province.data
            seeker.contact_number = form.contact_number.data
            
            # Step 2: Employment Status
            # Save only fields that apply to the selected situation. This prevents
            # hidden/default form values from polluting reports and matching.
            seeker.employment_status = form.employment_status.data

            seeker.fourps_beneficiary = form.fourps.data == "yes"
            seeker.fourps_id = (form.fourps_id.data or "").strip() if seeker.fourps_beneficiary else None
            if seeker.fourps_beneficiary and not seeker.fourps_id:
                raise ValueError("4Ps Household ID is required for 4Ps beneficiaries.")

            seeker.ofw_status = form.ofw_status.data or "no"
            seeker.ofw_country = (form.ofw_country.data or "").strip() if seeker.ofw_status == "yes" else None
            if seeker.ofw_status == "yes" and not seeker.ofw_country:
                raise ValueError("Country is required for OFW applicants.")

            seeker.former_ofw = form.former_ofw.data or "no"
            seeker.former_ofw_country = (form.former_ofw_country.data or "").strip() if seeker.former_ofw == "yes" else None
            seeker.former_ofw_return_date = (form.former_ofw_return_date.data or "").strip() if seeker.former_ofw == "yes" else None
            if seeker.former_ofw == "yes" and (not seeker.former_ofw_country or not seeker.former_ofw_return_date):
                raise ValueError("Latest country of deployment and month/year of return are required for Former OFW applicants.")

            if seeker.employment_status == "employed":
                seeker.employed_type = form.employed_type.data
                if seeker.employed_type == "self":
                    seeker.self_sector = form.self_sector.data
                    seeker.self_sector_other = (form.self_sector_other.data or "").strip() if form.self_sector.data == "Others" else None
                else:
                    seeker.self_sector = None
                    seeker.self_sector_other = None

                seeker.unemployed_reason = None
                seeker.jobsearch_duration = None
                seeker.termination_country = None
                seeker.unemployed_reason_other = None
            else:
                seeker.employed_type = None
                seeker.self_sector = None
                seeker.self_sector_other = None
                seeker.unemployed_reason = form.unemployed_reason.data
                seeker.jobsearch_duration = form.jobsearch_duration.data
                seeker.termination_country = (form.termination_country.data or "").strip() if form.unemployed_reason.data == "laidOffAbroad" else None
                seeker.unemployed_reason_other = (form.unemployed_reason_other.data or "").strip() if form.unemployed_reason.data == "otherUnemployed" else None
                if seeker.unemployed_reason == "otherUnemployed" and not seeker.unemployed_reason_other:
                    raise ValueError("Please specify the reason for unemployment.")
                if seeker.unemployed_reason == "laidOffAbroad" and not seeker.termination_country:
                    raise ValueError("Country of termination is required when laid off abroad.")

            selected_disabilities = form.disability.data or []
            seeker.disability = ", ".join(selected_disabilities) if selected_disabilities else None
            seeker.disability_other = (form.disability_other.data or "").strip() if "Others" in selected_disabilities else None
            if "Others" in selected_disabilities and not seeker.disability_other:
                raise ValueError("Please specify the other disability.")

            # Step 3: Job Preferences
            seeker.pref_job_type = ", ".join(form.pref_job_type.data) if isinstance(form.pref_job_type.data, list) else form.pref_job_type.data
            seeker.pref_occupation_1 = form.pref_occupation_1.data
            seeker.pref_occupation_2 = form.pref_occupation_2.data
            seeker.pref_occupation_3 = form.pref_occupation_3.data
            seeker.location_scope = ", ".join(form.location_scope.data) if isinstance(form.location_scope.data, list) else form.location_scope.data
            selected_scopes = form.location_scope.data or []
            if "Local" in selected_scopes:
                seeker.local_location_1 = form.local_location_1.data
                seeker.local_location_2 = form.local_location_2.data
                seeker.local_location_3 = form.local_location_3.data
            else:
                seeker.local_location_1 = None
                seeker.local_location_2 = None
                seeker.local_location_3 = None

            if "Overseas" in selected_scopes:
                seeker.overseas_location_1 = form.overseas_location_1.data
                seeker.overseas_location_2 = form.overseas_location_2.data
                seeker.overseas_location_3 = form.overseas_location_3.data
            else:
                seeker.overseas_location_1 = None
                seeker.overseas_location_2 = None
                seeker.overseas_location_3 = None

            # Skills & Competencies
            selected_skills = form.skills.data or []
            seeker.skills = ", ".join(selected_skills) if selected_skills else None
            seeker.skills_other = (form.skills_other.data or "").strip() if "Others" in selected_skills else None
            if "Others" in selected_skills and not seeker.skills_other:
                raise ValueError("Please specify the other skill selected.")

            # Step 5: Educational Background
            seeker.schooling_status = form.schooling_status.data
            
            # Elementary Level
            seeker.elem_school = form.elem_school.data
            seeker.elem_year_grad = form.elem_year_grad.data
            seeker.elem_level_reached = form.elem_level_reached.data
            seeker.elem_year_attended = form.elem_year_attended.data
            
            # Secondary Level
            seeker.sec_type = ", ".join(form.sec_type.data) if isinstance(form.sec_type.data, list) else form.sec_type.data
            seeker.sec_school = form.sec_school.data
            seeker.senior_high_strand = form.senior_high_strand.data
            seeker.sec_year_grad = form.sec_year_grad.data
            seeker.sec_level_reached = form.sec_level_reached.data
            seeker.sec_year_attended = form.sec_year_attended.data
            
            # Tertiary Level
            seeker.tertiary_course = form.tertiary_course.data
            seeker.tertiary_school = form.tertiary_school.data
            seeker.tertiary_year_grad = form.tertiary_year_grad.data
            seeker.tertiary_level_reached = form.tertiary_level_reached.data
            seeker.tertiary_year_attended = form.tertiary_year_attended.data
            
            # Graduate Studies
            seeker.grad_course = form.grad_course.data
            seeker.grad_school = form.grad_school.data
            seeker.grad_year_grad = form.grad_year_grad.data
            seeker.grad_level_reached = form.grad_level_reached.data
            seeker.grad_year_attended = form.grad_year_attended.data

            # Consents
            seeker.legal_consent = form.legal_consent.data
            seeker.privacy_act_consent = form.privacy_act_consent.data

            # Flush main seeker ID so dynamic child rows have valid foreign key context
            db.session.flush()

            # Dynamic JSRF rows are submitted outside WTForms. Validate them
            # server-side before deleting/replacing any saved child records.
            def _ensure_max(value, limit, label):
                value = (value or "").strip()
                if len(value) > limit:
                    raise ValueError(f"{label} cannot exceed {limit} characters.")
                return value

            dynamic_groups = {
                "Languages": request.form.getlist('lang_name[]'),
                "TVET courses": request.form.getlist('tvet_course[]'),
                "Licenses/eligibilities": request.form.getlist('license_name[]') + request.form.getlist('eligibility_name[]'),
                "Work history": request.form.getlist('company[]'),
            }
            for label, values in dynamic_groups.items():
                if len(values) > 50:
                    raise ValueError(f"Too many {label.lower()} entries were submitted.")

            for value in request.form.getlist('lang_name[]'):
                _ensure_max(value, 50, "Language name")
            for value in request.form.getlist('tvet_course[]'):
                _ensure_max(value, 150, "TVET course")
            for value in request.form.getlist('tvet_institution[]'):
                _ensure_max(value, 150, "TVET institution")
            for value in request.form.getlist('tvet_certificate[]'):
                _ensure_max(value, 100, "TVET certificate")
            for value in request.form.getlist('eligibility_name[]') + request.form.getlist('license_name[]'):
                _ensure_max(value, 150, "License/eligibility name")
            for value in request.form.getlist('company[]') + request.form.getlist('position[]'):
                _ensure_max(value, 150, "Work history value")
            for value in request.form.getlist('appointment_status[]'):
                _ensure_max(value, 50, "Appointment status")
            for value in request.form.getlist('tvet_hours[]'):
                raw = (value or "").strip()
                if raw and (not raw.isdigit() or int(raw) > 100000):
                    raise ValueError("TVET hours must be a whole number between 0 and 100000.")

            # Shared NSRP repeatable-row validation (also used by the future
            # supervised station submit endpoint). Run before deleting rows.
            from .nsrp_row_validation import validate_nsrp_rows
            validate_nsrp_rows(request.form)

            # Clear and update relational child tables
            JobSeekerLanguage.query.filter_by(job_seeker_id=seeker.job_seeker_id).delete()
            JobSeekerTVET.query.filter_by(job_seeker_id=seeker.job_seeker_id).delete()
            JobSeekerLicense.query.filter_by(job_seeker_id=seeker.job_seeker_id).delete()
            JobSeekerWorkHistory.query.filter_by(job_seeker_id=seeker.job_seeker_id).delete()

            # Helper function for safe date parsing
            def parse_date(date_str):
                # Reject malformed submitted dates rather than silently erasing them.
                if date_str is None or not str(date_str).strip():
                    return None
                raw = str(date_str).strip()
                try:
                    parsed = datetime.strptime(raw, '%Y-%m-%d').date()
                    if parsed.isoformat() != raw:
                        raise ValueError
                    return parsed
                except (ValueError, TypeError) as exc:
                    raise ValueError("Eligibility and license dates must use a valid YYYY-MM-DD date.") from exc
                
            def normalize_work_date(date_str):
                if not date_str:
                    return None

                date_str = str(date_str).strip()

                if not date_str:
                    return None

                try:
                    parsed = datetime.strptime(
                        date_str,
                        "%Y-%m-%d"
                    )

                    return parsed.strftime("%Y-%m-%d")

                except (ValueError, TypeError):
                    return None

            # Save Languages (Captures dynamically registered lang_name[] fields)
            lang_names = request.form.getlist('lang_name[]')
            for name in lang_names:
                name_clean = name.strip()
                if name_clean:
                    lang_row = JobSeekerLanguage(
                        job_seeker_id=seeker.job_seeker_id,
                        language_name=name_clean,
                        can_read=request.form.get(f'lang_read_{name_clean}') == 'on' or request.form.get(f'lang_read[]') == 'on',
                        can_write=request.form.get(f'lang_write_{name_clean}') == 'on' or request.form.get(f'lang_write[]') == 'on',
                        can_speak=request.form.get(f'lang_speak_{name_clean}') == 'on' or request.form.get(f'lang_speak[]') == 'on',
                        can_understand=request.form.get(f'lang_understand_{name_clean}') == 'on' or request.form.get(f'lang_understand[]') == 'on'
                    )
                    db.session.add(lang_row)

            # Save TVET Courses (Safe Integer parsing + zip_longest iteration)
            tvet_courses = request.form.getlist('tvet_course[]')
            tvet_hours = request.form.getlist('tvet_hours[]')
            tvet_institutions = request.form.getlist('tvet_institution[]')
            tvet_skills = request.form.getlist('tvet_skills[]')
            tvet_certs = request.form.getlist('tvet_certificate[]')

            for course, hours, inst, skill, cert in zip_longest(tvet_courses, tvet_hours, tvet_institutions, tvet_skills, tvet_certs, fillvalue=""):
                if course and course.strip():
                    parsed_hours = None
                    if hours and hours.strip().isdigit():
                        try:
                            parsed_hours = int(hours.strip())
                        except ValueError:
                            parsed_hours = None

                    tvet_row = JobSeekerTVET(
                        job_seeker_id=seeker.job_seeker_id,
                        training_course=course.strip(),
                        hours=parsed_hours,
                        institution=inst.strip() if inst else None,
                        skills_acquired=skill.strip() if skill else None,
                        certificate_received=cert.strip() if cert else None
                    )
                    db.session.add(tvet_row)

            # Save Licenses / Eligibilities (Safe Date parsing + zip_longest iteration)
            elig_names = request.form.getlist('eligibility_name[]')
            elig_dates = request.form.getlist('eligibility_date[]')
            lic_names = request.form.getlist('license_name[]')
            lic_validities = request.form.getlist('license_validity[]')

            for elig, e_date, lic, l_val in zip_longest(elig_names, elig_dates, lic_names, lic_validities, fillvalue=""):
                if (elig and elig.strip()) or (lic and lic.strip()):
                    lic_row = JobSeekerLicense(
                        job_seeker_id=seeker.job_seeker_id,
                        eligibility_name=elig.strip() if elig else None,
                        eligibility_date=parse_date(e_date),
                        license_name=lic.strip() if lic else None,
                        license_validity=parse_date(l_val)
                    )
                    db.session.add(lic_row)

            # Save Work History (Synced Fresh Grad Bypass + Safe Date parsing)

            is_fresh_grad = (
                request.form.get("fresh_grad_bypass") == "on"
                or (
                    getattr(form, "fresh_grad_bypass", None)
                    and form.fresh_grad_bypass.data
                )
            )

            if not is_fresh_grad:
                companies = request.form.getlist("company[]")
                company_addresses = request.form.getlist("company_address[]")
                positions = request.form.getlist("position[]")
                months_values = request.form.getlist("number_of_months[]")
                appointment_statuses = request.form.getlist("appointment_status[]")

                for comp, address, pos, months, appt in zip_longest(
                    companies, company_addresses, positions, months_values, appointment_statuses, fillvalue=""
                ):
                    company = _ensure_max(comp, 150, "Company name")
                    company_address = _ensure_max(address, 150, "Company address")
                    position = _ensure_max(pos, 150, "Position")
                    appointment_status = _ensure_max(appt, 50, "Work status")
                    if not company:
                        continue
                    if not company_address or not position or not str(months).strip() or not appointment_status:
                        raise ValueError("Each work experience requires company name, address, position, number of months, and status.")
                    try:
                        number_of_months = int(months) if str(months).strip() else None
                    except (TypeError, ValueError):
                        raise ValueError("Number of months must be a whole number.")
                    if number_of_months is not None and not 0 <= number_of_months <= 1200:
                        raise ValueError("Number of months must be between 0 and 1200.")

                    work_row = JobSeekerWorkHistory(
                        job_seeker_id=seeker.job_seeker_id,
                        company=company,
                        company_address=company_address or None,
                        position=position or None,
                        number_of_months=number_of_months,
                        appointment_status=appointment_status or None
                    )

                    db.session.add(work_row)

            # Final Validation & Persistence
            if form.validate_on_submit():
                seeker.certified_at = datetime.utcnow()
                seeker.is_verified = True
                seeker.verification_status = "Verified"
                seeker.verified_at = datetime.utcnow()
                seeker.jsrf_completed = True
                current_user.status = "active"
                
                db.session.commit()

                flash("NSRP Registration completed and account verified successfully!", "success")
                return redirect(url_for('jobseeker.dashboard'))
            else:
                # Commit intermediate progress as draft so reloads keep entered fields intact.
                # Surface the exact WTForms errors instead of silently returning to Step 1.
                db.session.commit()

                validation_messages = []
                for field_name, errors in form.errors.items():
                    field = getattr(form, field_name, None)
                    label = field.label.text if field is not None and getattr(field, "label", None) else field_name
                    for error in errors:
                        validation_messages.append(f"{label}: {error}")

                if validation_messages:
                    flash("Please correct: " + " | ".join(validation_messages), "danger")
                else:
                    flash("Progress saved as draft. Please check the required fields to complete verification.", "warning")

        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
        except Exception:
            db.session.rollback()
            current_app.logger.exception("Unexpected error saving jobseeker JSRF")
            flash("Unable to save your registration right now. Please try again.", "danger")

    return render_template('jobseeker/jobseeker_jsrf.html', form=form, jobseeker=seeker, jsrf_field_errors=(form.errors if request.method == 'POST' else {}))


@jobseeker.route("/dashboard")
@role_required("jobseeker")
@login_required
def dashboard():
    if current_user.role != "jobseeker":
        return render_template("errors/404.html"), 404

    seeker = JobSeeker.query.filter_by(user_id=current_user.user_id).first()

    if not seeker:
        flash("Jobseeker profile not found. Please complete registration.", "warning")
        return redirect(url_for("jobseeker.jsrf"))

    # Dashboard statistics
    jobs_applied = Application.query.filter_by(
        job_seeker_id=seeker.job_seeker_id
    ).count()

    scheduled_interviews = Application.query.filter_by(
        job_seeker_id=seeker.job_seeker_id,
        status="Interview"
    ).count()

    pending_offers = Application.query.filter_by(
        job_seeker_id=seeker.job_seeker_id,
        status="Accepted"
    ).count()

    # Count upcoming job fair pre-registrations for this jobseeker
    now = datetime.now(timezone.utc)
    upcoming_job_fairs_count = (
        JobFairRegistration.query
        .join(JobFair, JobFairRegistration.job_fair_id == JobFair.job_fair_id)
        .filter(
            JobFairRegistration.job_seeker_id == seeker.job_seeker_id,
            JobFair.status == "Upcoming",
            JobFair.start_datetime >= now
        )
        .count()
    )

    # Recommended jobs prioritizing jobseeker profile matches
    base_query = Job.query.filter_by(
        status="Open",
        approval_status="Approved"
    )

    recommended_jobs = []
    
    if seeker:
        matches = base_query.filter(
            db.or_(
                Job.job_title.ilike(f"%{seeker.pref_occupation_1}%") if seeker.pref_occupation_1 else False,
                Job.job_title.ilike(f"%{seeker.pref_occupation_2}%") if seeker.pref_occupation_2 else False,
                Job.location.ilike(f"%{seeker.city}%") if seeker.city else False
            )
        ).order_by(Job.created_at.desc()).limit(3).all()
        
        recommended_jobs.extend(matches)

    if len(recommended_jobs) < 3:
        needed = 3 - len(recommended_jobs)
        existing_ids = [j.job_id for j in recommended_jobs]
        
        fallbacks = (
            base_query
            .filter(~Job.job_id.in_(existing_ids)) if existing_ids else base_query
        ).order_by(Job.created_at.desc()).limit(needed).all()
        
        recommended_jobs.extend(fallbacks)

    # Recent applications
    recent_applications = (
        Application.query
        .filter_by(job_seeker_id=seeker.job_seeker_id)
        .order_by(Application.application_date.desc())
        .limit(3)
        .all()
    )

    # Latest announcements
    announcements = (
        Announcement.query
        .filter_by(status="Published")
        .order_by(Announcement.event_date.asc())
        .limit(3)
        .all()
    )

    # Upcoming job fairs
    now = datetime.now(timezone.utc)
    job_fairs = (
        JobFair.query
        .filter(
            JobFair.status == "Upcoming",
            JobFair.start_datetime >= now
        )
        .order_by(JobFair.start_datetime.asc())
        .limit(3)
        .all()
    )

    return render_template(
        "jobseeker/jobseeker_dashboard.html",
        jobseeker=seeker,
        jobs_applied=jobs_applied,
        scheduled_interviews=scheduled_interviews,
        pending_offers=pending_offers,
        recommended_jobs=recommended_jobs,
        recent_applications=recent_applications,
        announcements=announcements,
        job_fairs=job_fairs,
        upcoming_job_fairs_count=upcoming_job_fairs_count
    )


@jobseeker.route("/profile", methods=["GET", "POST"])
@login_required
@role_required("jobseeker")
def profile():
    if current_user.role != "jobseeker":
        return render_template("errors/404.html"), 404

    seeker_obj = JobSeeker.query.filter_by(user_id=current_user.user_id).first()

    if seeker_obj is None:
        flash("Your job seeker profile could not be found.", "danger")
        return redirect(url_for("auth.login"))

    # Repair profiles hired before automatic employment-status syncing.
    if sync_jobseeker_employment_status(seeker_obj):
        db.session.commit()

    if request.method == "POST":
        action = (request.form.get("action") or "").strip()
        allowed_actions = {
            "update_summary", "update_personal", "update_skills",
            "update_education", "update_experience", "update_preferences",
            "upload_pfp", "upload_resume",
        }
        if action not in allowed_actions:
            abort(400, description="Unsupported profile update action.")

        def _profile_text(name, current, limit):
            value = request.form.get(name)
            if value is None:
                return current
            value = value.strip()
            if len(value) > limit:
                abort(400, description=f"{name.replace('_', ' ').title()} is too long.")
            return value or None

        # --- UPDATE SUMMARY ---
        if action == "update_summary":
            seeker_obj.summary = _profile_text("summary", seeker_obj.summary, 5000)

        # --- UPDATE PERSONAL INFORMATION ---
        elif action == "update_personal":
            # Email is a verified authentication/recovery identity, not an
            # ordinary editable profile field. Changes require confirmation.
            # Do not overwrite the old verified address here.
            requested_email = (request.form.get("email") or "").strip().lower()
            existing_email = (current_user.email or "").strip().lower()
            if requested_email and requested_email != existing_email:
                flash("To change your email, use Verify New Email below. Other profile changes were saved.", "info")
            contact = _profile_text("contact_number", seeker_obj.contact_number, 50)
            if contact and not re.fullmatch(r"(?:09\d{9}|\+639\d{9})", contact):
                flash("Enter a valid Philippine mobile number (09XXXXXXXXX or +639XXXXXXXXX).", "danger")
                return redirect(request.path)
            seeker_obj.contact_number = contact
            seeker_obj.street = _profile_text("street", seeker_obj.street, 255)
            seeker_obj.barangay = _profile_text("barangay", seeker_obj.barangay, 100)
            seeker_obj.city = _profile_text("city", seeker_obj.city, 100)
            seeker_obj.province = _profile_text("province", seeker_obj.province, 100)
            seeker_obj.religion = _profile_text("religion", seeker_obj.religion, 100)

        # --- UPDATE SKILLS ---
        elif action == "update_skills":
            allowed_skills = {
                "Auto Mechanic", "Beautician", "Carpentry Work", "Computer Literate",
                "Domestic Chores", "Driver", "Electrician", "Embroidery", "Gardening",
                "Masonry", "Painter/Artist", "Painting Jobs", "Photography", "Plumbing",
                "Sewing Dresses", "Stenography", "Tailoring", "Others"
            }
            submitted_skills = [x.strip() for x in request.form.getlist("skills")]
            if any(not value or value not in allowed_skills for value in submitted_skills):
                flash("Select only valid skills from the available options.", "danger")
                return redirect(request.path)
            selected = list(dict.fromkeys(submitted_skills))
            seeker_obj.skills = ", ".join(selected) or None
            seeker_obj.skills_other = _profile_text("skills_other", seeker_obj.skills_other, 150) if "Others" in selected else None
            if "Others" in selected and not seeker_obj.skills_other:
                abort(400, description="Please specify the other skill selected.")

        # --- UPDATE EDUCATION ---
        elif action == "update_education":
            seeker_obj.tertiary_course = _profile_text("tertiary_course", seeker_obj.tertiary_course, 255)
            seeker_obj.tertiary_school = _profile_text("tertiary_school", seeker_obj.tertiary_school, 255)
            tertiary_year = _profile_text("tertiary_year_grad", seeker_obj.tertiary_year_grad, 50)
            if tertiary_year and (not re.fullmatch(r"[0-9]{4}", tertiary_year) or not 1900 <= int(tertiary_year) <= date.today().year):
                flash("Tertiary graduation year must be a valid four-digit year (1900 through the current year).", "danger")
                return redirect(request.path)
            seeker_obj.tertiary_year_grad = tertiary_year
            seeker_obj.senior_high_strand = _profile_text("senior_high_strand", seeker_obj.senior_high_strand, 100)
            seeker_obj.sec_school = _profile_text("sec_school", seeker_obj.sec_school, 255)
            secondary_year = _profile_text("sec_year_grad", seeker_obj.sec_year_grad, 50)
            if secondary_year and (not re.fullmatch(r"[0-9]{4}", secondary_year) or not 1900 <= int(secondary_year) <= date.today().year):
                flash("Secondary graduation year must be a valid four-digit year (1900 through the current year).", "danger")
                return redirect(request.path)
            seeker_obj.sec_year_grad = secondary_year

        # --- UPDATE WORK EXPERIENCE ---
        elif action == "update_experience":
            work_ids = request.form.getlist("work_id[]")
            companies = request.form.getlist("company[]")
            company_addresses = request.form.getlist("company_address[]")
            positions = request.form.getlist("position[]")
            months_values = request.form.getlist("number_of_months[]")
            appointment_statuses = request.form.getlist("appointment_status[]")
            row_lists = (work_ids, companies, company_addresses, positions,
                         months_values, appointment_statuses)
            if len(set(map(len, row_lists))) != 1:
                abort(400, description="Work experience rows are incomplete. Please reload and try again.")
            if len(work_ids) > 50:
                abort(400, description="Too many work experience entries.")

            existing_rows = {
                str(row.work_id): row
                for row in JobSeekerWorkHistory.query.filter_by(
                    job_seeker_id=seeker_obj.job_seeker_id
                ).all()
            }
            submitted_ids = set()
            seen_ids = set()
            for work_id, company, address, position, months, appointment_status in zip(
                work_ids, companies, company_addresses, positions,
                months_values, appointment_statuses
            ):
                company = (company or "").strip()
                address = (address or "").strip()
                position = (position or "").strip()
                months = (months or "").strip()
                appointment_status = (appointment_status or "").strip()
                if (len(company) > 150 or len(address) > 150 or len(position) > 150
                        or len(appointment_status) > 50):
                    abort(400, description="A work experience field exceeds its character limit.")
                if work_id:
                    if not work_id.isdigit() or work_id not in existing_rows or work_id in seen_ids:
                        abort(400, description="Invalid or duplicate work experience record.")
                    seen_ids.add(work_id)
                if not any((company, address, position, months, appointment_status)):
                    if work_id:
                        abort(400, description="Existing work experience cannot be submitted blank.")
                    continue
                if not all((company, address, position, months, appointment_status)):
                    abort(400, description="Complete all work experience fields before saving.")
                if not months.isascii() or not months.isdecimal():
                    abort(400, description="Number of months must be a whole number.")
                number_of_months = int(months)
                if not 0 <= number_of_months <= 1200:
                    abort(400, description="Number of months must be between 0 and 1200.")
                work = existing_rows[work_id] if work_id else JobSeekerWorkHistory(
                    job_seeker_id=seeker_obj.job_seeker_id
                )
                if not work_id:
                    db.session.add(work)
                    db.session.flush()
                work.company = company
                work.company_address = address
                work.position = position
                work.number_of_months = number_of_months
                work.appointment_status = appointment_status
                submitted_ids.add(work.work_id)

            for existing in existing_rows.values():
                if existing.work_id not in submitted_ids:
                    db.session.delete(existing)

        # --- UPDATE PREFERENCES ---
        elif action == "update_preferences":
            seeker_obj.pref_occupation_1 = _profile_text("pref_occupation_1", seeker_obj.pref_occupation_1, 150)
            seeker_obj.pref_occupation_2 = _profile_text("pref_occupation_2", seeker_obj.pref_occupation_2, 150)
            seeker_obj.pref_occupation_3 = _profile_text("pref_occupation_3", seeker_obj.pref_occupation_3, 150)

            job_types = request.form.getlist("pref_job_type")
            allowed_job_types = {"Full Time", "Part Time"}
            if any(job_type not in allowed_job_types for job_type in job_types):
                abort(400, description="Invalid preferred job type.")
            seeker_obj.pref_job_type = ", ".join(dict.fromkeys(job_types)) or None

            seeker_obj.local_location_1 = _profile_text("local_location_1", seeker_obj.local_location_1, 100)
            seeker_obj.local_location_2 = _profile_text("local_location_2", seeker_obj.local_location_2, 100)
            seeker_obj.local_location_3 = _profile_text("local_location_3", seeker_obj.local_location_3, 100)

            seeker_obj.overseas_location_1 = _profile_text("overseas_location_1", seeker_obj.overseas_location_1, 100)
            seeker_obj.overseas_location_2 = _profile_text("overseas_location_2", seeker_obj.overseas_location_2, 100)
            seeker_obj.overseas_location_3 = _profile_text("overseas_location_3", seeker_obj.overseas_location_3, 100)

        # --- UPLOAD PROFILE PICTURE ---
        elif action == "upload_pfp":
            if "profile_picture" in request.files:
                file = request.files["profile_picture"]

                if file and file.filename:
                    upload_folder = os.path.join(
                        current_app.static_folder,
                        "uploads",
                        "jobseekers",
                    )

                    try:
                        filename = save_validated_image(
                            file,
                            upload_folder,
                            prefix=f"pfp_{seeker_obj.job_seeker_id}",
                        )
                    except ValueError as error:
                        flash(str(error), "danger")
                        return redirect(request.path)

                    seeker_obj.profile_picture = filename

        # --- UPLOAD RESUME ---
        elif action == "upload_resume":
            if "resume" in request.files:
                file = request.files["resume"]

                if file and file.filename:
                    original_filename = file.filename

                    from website.utils.private_resumes import private_resume_directory
                    upload_folder = private_resume_directory()

                    try:
                        filename = save_validated_resume(
                            file,
                            upload_folder,
                            prefix=f"resume_{seeker_obj.job_seeker_id}",
                        )
                    except ValueError as error:
                        flash(str(error), "danger")
                        return redirect(request.path)

                    seeker_obj.resume = filename
                    seeker_obj.resume_filename = original_filename

        try:
            db.session.commit()
            flash("Profile updated successfully!", "success")
        except Exception:
            db.session.rollback()
            flash("An error occurred while saving changes.", "danger")

        return redirect(request.path)

    return render_template(
        "jobseeker/jobseeker_profile.html",
        current_user=current_user,
        jobseeker=seeker_obj,
        format_work_date=format_work_date
    )


@jobseeker.route("/download_resume")
@login_required
@role_required("jobseeker")
def download_resume():
    if current_user.role != "jobseeker":
        return render_template("errors/404.html"), 404

    seeker_obj = JobSeeker.query.filter_by(user_id=current_user.user_id).first()

    if not seeker_obj or not seeker_obj.resume:
        flash("No resume available for download.", "warning")
        return redirect(request.path)

    from website.utils.private_resumes import private_resume_directory
    upload_folder = private_resume_directory()
    stored_name = str(seeker_obj.resume)
    if (not stored_name or stored_name in {".", ".."}
            or os.path.basename(stored_name) != stored_name
            or "\\" in stored_name):
        abort(404)
    if not os.path.isfile(os.path.join(upload_folder, stored_name)):
        abort(404)
    
    return send_from_directory(
        directory=upload_folder,
        path=seeker_obj.resume,
        as_attachment=True,
        download_name=getattr(seeker_obj, 'resume_filename', seeker_obj.resume)
    )


@jobseeker.route("/download_nsrp_form", methods=["GET"])
@login_required
def download_nsrp_form():
    """Download the logged-in jobseeker's completed NSRP registration PDF."""

    if getattr(current_user, "role", None) != "jobseeker":
        abort(403)

    seeker = JobSeeker.query.filter_by(
        user_id=current_user.user_id
    ).first()

    if not seeker:
        abort(404)

    if not seeker.jsrf_completed:
        flash(
            "Please complete your JSRF registration before downloading NSRP Form 1.",
            "warning",
        )
        return redirect(url_for("jobseeker.profile"))

    pdf_file = generate_nsrp_pdf(seeker)

    filename_parts = [
        str(getattr(seeker, "last_name", "") or ""),
        str(getattr(seeker, "first_name", "") or ""),
    ]
    safe_name = "_".join(
        part.strip().replace(" ", "_")
        for part in filename_parts
        if part.strip()
    ) or "Jobseeker"

    response = send_file(
        pdf_file,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"NSRP_Form_1_{safe_name}.pdf",
    )
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response



@jobseeker.route("/request-referral", methods=["POST"])
@login_required
@role_required("jobseeker")
def request_referral():

    # =========================================================
    # SECURITY
    # =========================================================
    if current_user.role != "jobseeker":
        return render_template("errors/404.html"), 404


    # =========================================================
    # GET JOBSEEKER
    # =========================================================
    seeker = JobSeeker.query.filter_by(
        user_id=current_user.user_id
    ).first()

    if not seeker:
        flash(
            "Jobseeker profile not found.",
            "danger"
        )
        return redirect(
            url_for("jobseeker.dashboard")
        )


    # =========================================================
    # GET JOB ID
    # =========================================================
    job_id = request.form.get(
        "job_id",
        type=int
    )

    if not job_id:
        flash(
            "Invalid job selected.",
            "danger"
        )
        return redirect(
            url_for("jobseeker.joblist")
        )


    # =========================================================
    # GET JOB
    # =========================================================
    job = Job.query.get(job_id)

    if not job:
        flash(
            "Job listing not found.",
            "danger"
        )
        return redirect(
            url_for("jobseeker.joblist")
        )


    # =========================================================
    # CENTRAL REFERRAL VALIDATION
    # =========================================================
    can_request, reason = validate_referral_request(
        seeker,
        job,
    )

    if not can_request:
        flash(reason, "warning")
        return redirect(
            url_for("jobseeker.joblist")
        )


    # =========================================================
    # STEP 1:
    # CREATE APPLICATION + REFERRAL
    #
    # IMPORTANT:
    # Commit these BEFORE doing anything unrelated such as
    # notifications.
    # =========================================================
    try:

        application = Application(
            job_id=job.job_id,
            job_seeker_id=seeker.job_seeker_id,
            status="Pending"
        )

        db.session.add(application)

        # Flush generates application.application_id
        # without committing yet.
        db.session.flush()


        referral = Referral(
            application_id=application.application_id,
            referred_by=None,
            status="Pending",
            remarks="Referral requested by jobseeker."
        )

        db.session.add(referral)
        db.session.flush()
        record_audit(
            f"REFERRAL_REQUESTED referral_id={referral.referral_id} application_id={application.application_id} job_id={job.job_id}"
        )

        # -----------------------------------------------------
        # COMMIT THE ACTUAL REFERRAL REQUEST
        # -----------------------------------------------------
        db.session.commit()


    except IntegrityError:

        db.session.rollback()
        # A concurrent submission may have created the same application.
        # The database unique constraint prevents duplicate records.
        existing_application = Application.query.filter_by(
            job_id=job.job_id,
            job_seeker_id=seeker.job_seeker_id,
        ).first()
        if existing_application:
            flash("You have already submitted a request for this job.", "warning")
        else:
            current_app.logger.exception("Referral request integrity failure")
            flash("Unable to submit referral request. Please try again.", "danger")
        return redirect(url_for("jobseeker.joblist"))

    except Exception as e:

        db.session.rollback()

        current_app.logger.exception(
            "FAILED TO CREATE REFERRAL REQUEST: %s",
            e
        )

        flash(
            "Unable to submit referral request. "
            "Please try again.",
            "danger"
        )

        return redirect(
            url_for("jobseeker.joblist")
        )


    # =========================================================
    # STEP 2:
    # NOTIFICATIONS
    #
    # Notification failure must NOT undo the already committed
    # application/referral.
    # =========================================================
    try:

        notify_admins(
            "New Referral Request",
            (
                f"{seeker.full_name} requested a referral "
                f"for {job.job_title}."
            ),
            "Referral"
        )
        commit_notifications("new referral request admin notification")

    except Exception as e:

        # Do NOT rollback the referral request here.
        # It has already been safely committed above.

        current_app.logger.exception(
            "Referral was created, but admin notification failed: %s",
            e
        )


    flash(
        "Your referral request has been submitted successfully.",
        "success"
    )

    return redirect(
        url_for("jobseeker.joblist")
    )

@jobseeker.route("/announcement")
@login_required
@role_required("jobseeker")
def announcement():
    if current_user.role != "jobseeker":
        return render_template("errors/404.html"), 404

    seeker = JobSeeker.query.filter_by(user_id=current_user.user_id).first()
    if not seeker:
        flash("Jobseeker profile not found.", "danger")
        return redirect(url_for("jobseeker.dashboard"))

    # Job fair DATETIME values are stored as local Philippine wall-clock time.
    # Compare in Asia/Manila; treating naive database values as UTC delays
    # the Ongoing status by eight hours.
    manila_tz = ZoneInfo("Asia/Manila")
    now = datetime.now(manila_tz)

    db_fairs = JobFair.query.order_by(JobFair.start_datetime.asc()).all()

    all_fairs = []
    def _to_aware(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=manila_tz)
        return dt.astimezone(manila_tz)

    for item in db_fairs:
        start_dt = _to_aware(item.start_datetime)
        end_dt = _to_aware(item.end_datetime)

        # Include if it starts in the future, or it hasn't ended yet (ongoing)
        if (start_dt and start_dt >= now) or (end_dt and end_dt >= now):
            all_fairs.append(item)

    announcements_data = []
    for item in all_fairs:
        # Recalculate dates for this fair; values from the filtering loop are stale.
        start_dt = _to_aware(item.start_datetime)
        end_dt = _to_aware(item.end_datetime)
        registration = JobFairRegistration.query.filter_by(
            job_seeker_id=seeker.job_seeker_id,
            job_fair_id=item.job_fair_id
        ).first()

        # Event lifecycle status for the announcement card.
        # The route already filters out fairs that have fully ended,
        # so visible events are either Upcoming or Ongoing.
        if (
            start_dt is not None
            and start_dt <= now
            and (
                end_dt is None
                or end_dt >= now
            )
        ):
            event_status = "Ongoing"
        else:
            event_status = "Upcoming"

        item_dict = {
            'announcement_id': item.job_fair_id,
            'title': item.title,
            'short_description': item.description[:100] if item.description else '',
            'description': item.description,
            'category': item.event_type or 'Job Fair',
            'event_date': item.start_datetime.date() if item.start_datetime else None,
            'start_time': item.start_datetime,
            'end_time': item.end_datetime,
            'venue': item.venue,
            'job_fair_id': item.job_fair_id,
            'poster_image': getattr(item, 'poster_image', None),
            'status': event_status,
            'is_registered': bool(registration),
            'reg_id': registration.registration_id if registration else None
        }

        announcements_data.append(item_dict)

    return render_template(
        "jobseeker/jobseeker_announcement.html",
        announcements=announcements_data,
        jobseeker=seeker
    )
    

UNANSWERED_INTERVIEW_STATUSES = {None, "Pending Confirmation"}


@jobseeker.route("/application")
@login_required
@role_required("jobseeker")
def application():

    # --------------------------------------------------------
    # 1. GET CURRENT JOBSEEKER
    # --------------------------------------------------------
    seeker = JobSeeker.query.filter_by(user_id=current_user.user_id).first()

    if not seeker:
        flash("Jobseeker profile not found.", "danger")
        return redirect(url_for("jobseeker.dashboard"))

    today = date.today()

    # --------------------------------------------------------
    # 2. FETCH ALL APPLICATIONS
    # --------------------------------------------------------
    all_apps = (
        Application.query.filter_by(job_seeker_id=seeker.job_seeker_id)
        .order_by(Application.application_date.desc())
        .all()
    )

    # --------------------------------------------------------
    # 3. UPDATE EXPIRED INTERVIEWS & FILTER LISTS IN ONE PASS
    # --------------------------------------------------------
    interviews = []
    offers = []
    referral_request_count = 0
    job_fair_application_count = 0
    status_changed = False

    for app in all_apps:
        # Check and mark expired/unanswered interviews
        if (
            app.status == "Interview"
            and app.interview_date
            and app.interview_date < today
            and app.interview_response_status in UNANSWERED_INTERVIEW_STATUSES
        ):
            app.interview_response_status = "Missed"
            status_changed = True

        # Collect interview records
        if app.status == "Interview" and app.interview_date is not None:
            interviews.append(app)

        # Collect offer/outcome records
        if app.status in {"Accepted", "Rejected"} or app.job_fair_evaluation is not None:
            offers.append(app)

        # Increment counts
        if app.job_fair_evaluation is not None:
            job_fair_application_count += 1
        elif app.referrals:
            referral_request_count += 1

    # Persist batch updates if any status changed
    if status_changed:
        try:
            db.session.commit()
        except Exception as error:
            db.session.rollback()
            current_app.logger.exception(
                "Unable to update expired interview responses: %s", error
            )

    # --------------------------------------------------------
    # 4. RENDER TEMPLATE
    # --------------------------------------------------------
    return render_template(
        "jobseeker/jobseeker_application.html",
        jobseeker=seeker,
        applications=all_apps,
        interviews=interviews,
        offers=offers,
        referral_request_count=referral_request_count,
        job_fair_application_count=job_fair_application_count,
        current_date=today.isoformat(),
    )


def _get_owned_application(application_id):
    seeker = JobSeeker.query.filter_by(user_id=current_user.user_id).first()
    if not seeker:
        flash("Jobseeker profile not found.", "danger")
        return None
    return Application.query.filter_by(
        application_id=application_id,
        job_seeker_id=seeker.job_seeker_id
    ).first()


MANILA_TZ = ZoneInfo("Asia/Manila")


def _job_fair_manila_time(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=MANILA_TZ)
    return value.astimezone(MANILA_TZ)


# ============================================================
# CONFIRM INTERVIEW ATTENDANCE
# ============================================================

@jobseeker.route("/application/<int:application_id>/confirm-interview", methods=["POST"])
@login_required
@role_required("jobseeker")
def confirm_interview(application_id):
    redirect_url = url_for("jobseeker.application", _anchor="interviews")
    application = _get_owned_application(application_id)
    if application is None:
        flash("That interview is no longer available.", "warning")
        db.session.rollback()
        return redirect(redirect_url)

    # Serialize responses with employer scheduling and other applicant requests.
    application = Application.query.filter_by(
        application_id=application.application_id,
        job_seeker_id=application.job_seeker_id,
    ).with_for_update().first()

    if not application or application.status != "Interview" or not application.interview_date:
        flash("That interview is no longer available for confirmation.", "warning")
        db.session.rollback()
        return redirect(redirect_url)

    if application.interview_date < datetime.now(MANILA_TZ).date():
        flash("This interview schedule has already passed.", "warning")
        db.session.rollback()
        return redirect(redirect_url)

    if application.interview_response_status == "Confirmed":
        flash("You have already confirmed this interview.", "info")
        db.session.rollback()
        return redirect(redirect_url)

    if application.interview_response_status == "Unable to Attend":
        flash("You already informed the employer that you cannot attend this schedule.", "warning")
        db.session.rollback()
        return redirect(redirect_url)

    application.interview_response_status = "Confirmed"
    application.interview_response_note = None
    record_audit(f"INTERVIEW_CONFIRMED application_id={application.application_id} job_id={application.job_id}")

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("Interview confirmation failed: %s", e)
        flash("Unable to confirm your interview.", "danger")
        db.session.rollback()
        return redirect(redirect_url)

    if application.job and application.job.employer:
        notify_user(
            application.job.employer.user_id,
            "Interview Attendance Confirmed",
            f"{application.job_seeker.full_name} confirmed attendance for the interview for {application.job.job_title}.",
            "Application",
        )
        commit_notifications("interview confirmation employer notification")

    flash("Interview attendance confirmed.", "success")
    return redirect(redirect_url)


# ============================================================
# UNABLE TO ATTEND INTERVIEW
# ============================================================

@jobseeker.route("/application/<int:application_id>/unable-to-attend", methods=["POST"])
@login_required
@role_required("jobseeker")
def unable_to_attend_interview(application_id):
    redirect_url = url_for("jobseeker.application", _anchor="interviews")
    application = _get_owned_application(application_id)
    if application is None:
        flash("That interview is no longer available.", "warning")
        db.session.rollback()
        return redirect(redirect_url)

    # Serialize responses with employer scheduling and other applicant requests.
    application = Application.query.filter_by(
        application_id=application.application_id,
        job_seeker_id=application.job_seeker_id,
    ).with_for_update().first()

    if not application or application.status != "Interview" or not application.interview_date:
        flash("That interview is no longer available.", "warning")
        db.session.rollback()
        return redirect(redirect_url)

    if application.interview_date < datetime.now(MANILA_TZ).date():
        flash("This interview schedule has already passed.", "warning")
        db.session.rollback()
        return redirect(redirect_url)

    if application.interview_response_status == "Confirmed":
        flash("You already confirmed attendance for this interview.", "warning")
        db.session.rollback()
        return redirect(redirect_url)

    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("Please provide a short reason why you cannot attend.", "warning")
        db.session.rollback()
        return redirect(redirect_url)

    if len(reason) > 500:
        flash("Your reason must not exceed 500 characters.", "warning")
        db.session.rollback()
        return redirect(redirect_url)

    application.interview_response_status = "Unable to Attend"
    application.interview_response_note = reason
    record_audit(f"INTERVIEW_UNABLE_TO_ATTEND application_id={application.application_id} job_id={application.job_id}")

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("Unable to save interview response: %s", e)
        flash("Unable to submit your response. Please try again.", "danger")
        db.session.rollback()
        return redirect(redirect_url)

    if application.job and application.job.employer:
        notify_user(
            application.job.employer.user_id,
            "Applicant Unable to Attend Interview",
            f"{application.job_seeker.full_name} indicated that they cannot attend the scheduled interview for {application.job.job_title}.",
            "Application",
        )
        commit_notifications("interview unable-to-attend employer notification")

    flash("Your response was sent to the employer.", "success")
    return redirect(redirect_url)



@jobseeker.route("/job-fair/<int:fair_id>")
@login_required
@role_required("jobseeker")
def job_fair_details(fair_id):
    if current_user.role != "jobseeker":
        return render_template("errors/404.html"), 404

    seeker = JobSeeker.query.filter_by(user_id=current_user.user_id).first()
    if not seeker:
        flash("Jobseeker profile not found.", "danger")
        return redirect(url_for("jobseeker.dashboard"))

    fair = JobFair.query.get_or_404(fair_id)
    
    fair_deadline = _job_fair_manila_time(
        fair.registration_deadline
    )

    existing_reg = JobFairRegistration.query.filter_by(
        job_fair_id=fair.job_fair_id, 
        job_seeker_id=seeker.job_seeker_id
    ).first()
    
    registrations_count = JobFairRegistration.query.filter_by(
        job_fair_id=fair.job_fair_id
    ).count()
    
    now = datetime.now(MANILA_TZ)

    return render_template(
        "jobseeker/jobseeker_job_fair_details.html",
        fair=fair,
        fair_deadline=fair_deadline,
        existing_reg=existing_reg,
        jobseeker=seeker,
        registrations_count=registrations_count,
        now=now,
        registration_open=is_registration_open(fair)
    )


@jobseeker.route("/job-fair/<int:fair_id>/register", methods=["POST"])
@login_required
@role_required("jobseeker")
def register_job_fair(fair_id):
    if current_user.role != "jobseeker":
        return render_template("errors/404.html"), 404

    seeker = JobSeeker.query.filter_by(user_id=current_user.user_id).first()
    if not seeker:
        flash("Jobseeker profile not found.", "danger")
        return redirect(url_for("jobseeker.dashboard"))

    # Serialize registration against this fair's capacity and status.
    # All registration entry points must lock this same JobFair row to
    # guarantee capacity across web and designated on-site registration.
    try:
        fair = JobFair.query.filter_by(job_fair_id=fair_id).with_for_update().first_or_404()

        if not is_registration_open(fair):
            if fair.status == "Finished":
                message = "Registration is closed because this Job Fair has already finished."
            elif getattr(fair, "registration_manually_closed", False):
                message = "Registration for this Job Fair has been closed by PESO."
            else:
                message = "Registration for this Job Fair has already closed."
            db.session.rollback()
            flash(message, "warning")
            return redirect(url_for("jobseeker.job_fair_details", fair_id=fair_id))

        existing_reg = JobFairRegistration.query.filter_by(
            job_fair_id=fair_id, job_seeker_id=seeker.job_seeker_id
        ).first()
        if existing_reg:
            existing_id = existing_reg.registration_id
            db.session.rollback()
            flash("You are already registered for this job fair.", "warning")
            return redirect(url_for("jobseeker.job_fair_confirmation", reg_id=existing_id))

        current_registrations_count = JobFairRegistration.query.filter_by(job_fair_id=fair_id).count()
        if current_registrations_count >= fair.capacity:
            db.session.rollback()
            flash("Sorry, this job fair has reached its maximum capacity.", "danger")
            return redirect(url_for("jobseeker.announcement"))

        unique_ref_id = str(uuid.uuid4()).replace("-", "")[:8].upper()

        # Classification is based on registration method, not event timing.
        # The normal jobseeker web route is always Pre-registered; the
        # designated staff-assisted on-site flow assigns On-site separately.
        registration_source = "Pre-registered"

        new_reg = JobFairRegistration(
            job_fair_id=fair.job_fair_id,
            job_seeker_id=seeker.job_seeker_id,
            reference_id=unique_ref_id,
            registration_source=registration_source,
            attendance_status="Registered"
        )
        db.session.add(new_reg)
        db.session.flush()
        record_audit(
            f"JOB_FAIR_REGISTERED registration_id={new_reg.registration_id} job_fair_id={fair.job_fair_id}"
        )
        db.session.commit()

        flash("Successfully registered for the job fair!", "success")
        return redirect(url_for('jobseeker.job_fair_confirmation', reg_id=new_reg.registration_id))

    except IntegrityError:
        db.session.rollback()
        # The unique constraint remains the final protection against duplicate submissions.
        existing = JobFairRegistration.query.filter_by(
            job_fair_id=fair_id, job_seeker_id=seeker.job_seeker_id
        ).first()
        if existing:
            flash("You are already registered for this job fair.", "warning")
            return redirect(url_for("jobseeker.job_fair_confirmation", reg_id=existing.registration_id))
        flash("Registration could not be completed. Please try again.", "danger")
        return redirect(url_for("jobseeker.job_fair_details", fair_id=fair_id))
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Job fair registration failed for fair_id=%s", fair_id
        )
        flash("An error occurred during registration. Please try again.", "danger")
        return redirect(url_for('jobseeker.job_fair_details', fair_id=fair_id))


@jobseeker.route("/job-fair/confirmation/<int:reg_id>")
@login_required
@role_required("jobseeker")
def job_fair_confirmation(reg_id):
    if current_user.role != "jobseeker":
        return render_template("errors/404.html"), 404

    seeker = JobSeeker.query.filter_by(user_id=current_user.user_id).first()
    if seeker is None:
        abort(404)
    # Filter by ownership at the database boundary; never load another
    # applicant's registration into the authenticated request context.
    registration = JobFairRegistration.query.filter_by(
        registration_id=reg_id, job_seeker_id=seeker.job_seeker_id
    ).first_or_404()

    return render_template(
        "jobseeker/jobseeker_job_fair_confirmation.html",
        registration=registration,
        fair=registration.job_fair,
        jobseeker=seeker
    )

@jobseeker.route("/profile/request-email-verification", methods=["POST"])
@login_required
@role_required("jobseeker")
def request_email_verification():
    """Send a short-lived proof-of-ownership link; never change email on request."""
    address = (request.form.get("new_email") or "").strip().lower()
    if len(address) > 100 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", address) or not is_deliverable_email(address):
        flash("Enter a valid email address.", "danger")
        return redirect(request.path)
    if User.query.filter(func.lower(User.email) == address, User.user_id != current_user.user_id).first():
        flash("This email cannot be used. Please choose another address.", "danger")
        return redirect(request.path)
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="peso-jobseeker-email-verification-v1")
    token = serializer.dumps({"uid": current_user.user_id, "address": address,
                              "previous": current_user.email, "version": current_user.auth_version or 0})
    link = url_for("jobseeker.confirm_email_verification", token=token, _external=True)
    try:
        send_email(address, "Verify your PESO-IMS email address",
                   "Open this link within 30 minutes to verify your new email address:\n" + link)
    except Exception:
        current_app.logger.exception("Could not queue jobseeker email verification")
        flash("Unable to send the verification email. Please try again later.", "danger")
        return redirect(request.path)
    flash("If delivery succeeds, a verification link will arrive at the new address. It expires in 30 minutes.", "info")
    return redirect(request.path)


@jobseeker.route("/profile/verify-email/<token>", methods=["GET", "POST"])
@login_required
@role_required("jobseeker")
def confirm_email_verification(token):
    serializer = URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="peso-jobseeker-email-verification-v1")
    try:
        data = serializer.loads(token, max_age=1800)
    except (BadSignature, SignatureExpired):
        flash("This email verification link is invalid or expired.", "danger")
        return redirect(request.path)
    if (data.get("uid") != current_user.user_id or
        data.get("version") != (current_user.auth_version or 0) or
        data.get("previous") != current_user.email):
        flash("This verification link is no longer valid.", "danger")
        return redirect(request.path)
    address = str(data.get("address") or "").strip().lower()
    if not is_deliverable_email(address) or len(address) > 100:
        abort(400)
    if User.query.filter(func.lower(User.email) == address, User.user_id != current_user.user_id).first():
        flash("This email address is no longer available.", "danger")
        return redirect(request.path)
    seeker = JobSeeker.query.filter_by(user_id=current_user.user_id).first()
    if seeker is None:
        abort(404)
    if request.method == "GET":
        response = make_response(render_template(
            "jobseeker/confirm_email_verification.html",
            email_address=address,
            confirmation_action=url_for("jobseeker.confirm_email_verification", token=token),
        ))
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response
    # Flask-WTF's global CSRFProtect validates the POST before this handler runs.
    try:
        current_user.email = address
        current_user.email_verified = True
        seeker.email = address
        # Invalidate earlier verification links and login sessions.
        current_user.auth_version = (current_user.auth_version or 0) + 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Jobseeker email verification failed")
        flash("Unable to save the verified email. Please try again.", "danger")
        return redirect(request.path)
    from flask_login import logout_user
    logout_user()
    flash("Email verified successfully. Please sign in again.", "success")
    return redirect(url_for("auth.login"))
