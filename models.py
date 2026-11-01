from datetime import datetime, timedelta, timezone
import uuid
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash
from website.extensions import db


def generate_public_user_id(context):
    """Generate an opaque, role-labelled ID for URLs and UI only."""
    role = (context.get_current_parameters().get("role") or "user").lower()
    prefix = {
        "admin": "ADM",
        "employer": "EMP",
        "jobseeker": "JS",
        "kiosk": "KSK",
    }.get(role, "USR")
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


class User(UserMixin, db.Model):
    __tablename__ = "users"

    user_id = db.Column(db.Integer, primary_key=True)
    public_id = db.Column(db.String(20), unique=True, nullable=False, index=True, default=generate_public_user_id)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.Enum("admin", "employer", "jobseeker", "kiosk"), nullable=False)
    status = db.Column(db.Enum("pending", "active", "rejected", "suspended"), default="pending")
    email_verified = db.Column(db.Boolean, default=False)
    is_unclaimed = db.Column(db.Boolean, nullable=False, default=False)
    # Increment after credential recovery to invalidate all prior Flask-Login sessions.
    auth_version = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    last_login = db.Column(db.DateTime)
    is_deleted = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    failed_login_attempts = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime, nullable=True)

    admin = db.relationship("Admin", back_populates="user", uselist=False)
    employer = db.relationship("Employer", foreign_keys="Employer.user_id", back_populates="user", uselist=False)
    job_seeker = db.relationship("JobSeeker", back_populates="user", uselist=False)
    notifications = db.relationship("Notification", back_populates="user", cascade="all, delete-orphan", lazy=True)
    audit_logs = db.relationship("AuditLog", back_populates="user", cascade="all, delete-orphan", lazy=True)
    kiosk = db.relationship("Kiosk", back_populates="user", uselist=False, cascade="all, delete-orphan")

    def is_locked(self):
        if self.locked_until and self.locked_until > datetime.utcnow():
            return True
        return False

    def reset_login_attempts(self):
        self.failed_login_attempts = 0
        self.locked_until = None
        db.session.commit()

    def handle_failed_login(self, max_attempts=5, lockout_minutes=15):
        self.failed_login_attempts += 1
        if self.failed_login_attempts >= max_attempts:
            self.locked_until = datetime.utcnow() + timedelta(minutes=lockout_minutes)
        db.session.commit()

    def get_id(self):
        return f"{self.user_id}:{self.auth_version or 0}"

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if self.is_unclaimed:
            return False
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User {self.username}>"


class EmployerRegistrationRequest(db.Model):
    __tablename__ = "employer_registration_requests"

    request_id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    contact_number = db.Column(db.String(20), unique=True, nullable=False)
    company_name = db.Column(db.String(150), nullable=True)
    business_type = db.Column(db.String(100), nullable=True)
    registration_number = db.Column(db.String(100), nullable=True)
    bir_no = db.Column(db.String(100), nullable=True)
    business_permit_no = db.Column(db.String(100), nullable=True)
    company_address = db.Column(db.String(255), nullable=True)
    representative_name = db.Column(db.String(150), nullable=True)
    representative_position = db.Column(db.String(100), nullable=True)
    status = db.Column(db.Enum("Pending", "Approved", "Rejected", "Completed", name="registration_request_status"), default="Pending", nullable=False)
    registration_token = db.Column(db.String(255), unique=True, nullable=True)
    token_expiry = db.Column(db.DateTime, nullable=True)
    remarks = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    approved_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    def __repr__(self):
        return f"<EmployerRegistrationRequest {self.email}>"


class Admin(db.Model):
    __tablename__ = "admins"

    admin_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False, unique=True)
    first_name = db.Column(db.String(50))
    middle_name = db.Column(db.String(50))
    last_name = db.Column(db.String(50))
    contact_number = db.Column(db.String(20))
    date_of_birth = db.Column(db.Date, nullable=True)
    country = db.Column(db.String(100), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    postal_code = db.Column(db.String(4), nullable=True)
    profile_picture = db.Column(db.String(255), nullable=True)
    # Explicit privilege boundary for sensitive administrator-management actions.
    # Regular admins remain role="admin"; only the designated account has this flag.

    user = db.relationship("User", back_populates="admin")
    referrals_made = db.relationship("Referral", foreign_keys="Referral.referred_by", back_populates="referrer", lazy=True)
    generated_reports = db.relationship("Report", back_populates="generator", lazy=True)

    def __repr__(self):
        return f"<Admin {self.admin_id}>"


class Employer(db.Model):
    __tablename__ = "employers"

    employer_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False, unique=True)
    company_name = db.Column(db.String(150), nullable=False)
    company_email = db.Column(db.String(100))
    company_phone = db.Column(db.String(20))
    street = db.Column(db.String(255))
    barangay = db.Column(db.String(100))
    city = db.Column(db.String(100))
    province = db.Column(db.String(100))
    website = db.Column(db.String(255), nullable=True)
    company_description = db.Column(db.Text)
    company_logo = db.Column(db.String(255))
    founded_year = db.Column(db.String(10))
    company_size = db.Column(db.String(50))
    account_status = db.Column(db.String(50), default="Active")
    company_industry = db.Column(db.String(150))
    google_map_url = db.Column(db.String(500))
    facebook_url = db.Column(db.String(255))
    linkedin_url = db.Column(db.String(255))
    instagram_url = db.Column(db.String(255))
    twitter_url = db.Column(db.String(255))
    gallery_1 = db.Column(db.String(255))
    gallery_2 = db.Column(db.String(255))
    gallery_3 = db.Column(db.String(255))
    business_permit_no = db.Column(db.String(100))
    mayor_permit_no = db.Column(db.String(100))
    bir_no = db.Column(db.String(100))
    verified = db.Column(db.Enum("pending", "approved", "rejected"), default="pending")
    remarks = db.Column(db.Text)
    verified_by = db.Column(db.Integer, db.ForeignKey("admins.admin_id"))
    verified_at = db.Column(db.DateTime)

    user = db.relationship("User", foreign_keys=[user_id], back_populates="employer")
    verifier = db.relationship("Admin", foreign_keys=[verified_by])
    jobs = db.relationship("Job", back_populates="employer", cascade="all, delete-orphan", lazy=True)
    job_fair_registrations = db.relationship("JobFairEmployerRegistration", back_populates="employer", cascade="all, delete-orphan", lazy=True)

    @property
    def email(self):
        if self.company_email:
            return self.company_email
        if self.user and self.user.email:
            return self.user.email
        return ""

    @property
    def logo(self):
        return self.company_logo

    @property
    def summary(self):
        return self.company_description

    @property
    def address(self):
        parts = [self.street, self.barangay, self.city, self.province]
        return ", ".join([part for part in parts if part])

    def __repr__(self):
        return f"<Employer {self.company_name}>"


class JobSeeker(db.Model):
    __tablename__ = "job_seekers"

    job_seeker_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    first_name = db.Column(db.String(100))
    middle_name = db.Column(db.String(100))
    last_name = db.Column(db.String(100))
    suffix = db.Column(db.String(20))
    sex = db.Column(db.String(20))
    birthdate = db.Column(db.Date)
    religion = db.Column(db.String(100))
    tin = db.Column(db.String(50))
    height = db.Column(db.String(50))
    civil_status = db.Column(db.String(50))
    contact_number = db.Column(db.String(50))
    summary = db.Column(db.Text, nullable=True)
    street = db.Column(db.String(255))
    barangay = db.Column(db.String(100))
    city = db.Column(db.String(100))
    province = db.Column(db.String(100))
    employment_status = db.Column(db.String(100))
    fourps_beneficiary = db.Column(db.Boolean, default=False)
    fourps_id = db.Column(db.String(100))
    ofw_status = db.Column(db.String(50))
    ofw_country = db.Column(db.String(100))
    former_ofw = db.Column(db.String(50))
    former_ofw_country = db.Column(db.String(100))
    former_ofw_return_date = db.Column(db.String(50))
    employed_type = db.Column(db.String(100))
    self_sector = db.Column(db.String(100))
    self_sector_other = db.Column(db.String(255))
    unemployed_reason = db.Column(db.String(255))
    jobsearch_duration = db.Column(db.Integer)
    termination_country = db.Column(db.String(100))
    unemployed_reason_other = db.Column(db.String(255))
    disability = db.Column(db.String(255))
    disability_other = db.Column(db.String(255))
    pref_job_type = db.Column(db.String(100))
    expected_salary = db.Column(db.Numeric(10, 2))
    pref_occupation_1 = db.Column(db.String(150))
    pref_occupation_2 = db.Column(db.String(150))
    pref_occupation_3 = db.Column(db.String(150))
    location_scope = db.Column(db.String(100))
    overseas_location_1 = db.Column(db.String(100))
    local_location_1 = db.Column(db.String(100))
    local_location_2 = db.Column(db.String(100))
    local_location_3 = db.Column(db.String(100))
    overseas_location_2 = db.Column(db.String(100))
    overseas_location_3 = db.Column(db.String(100))
    skills = db.Column(db.Text)
    skills_other = db.Column(db.String(150))
    experience = db.Column(db.Text)
    schooling_status = db.Column(db.String(50))
    elem_school = db.Column(db.String(255))
    elem_year_grad = db.Column(db.String(50))
    elem_level_reached = db.Column(db.String(100))
    elem_year_attended = db.Column(db.String(50))
    sec_type = db.Column(db.String(100))
    sec_school = db.Column(db.String(255))
    senior_high_strand = db.Column(db.String(100))
    sec_year_grad = db.Column(db.String(50))
    sec_level_reached = db.Column(db.String(100))
    sec_year_attended = db.Column(db.String(50))
    tertiary_course = db.Column(db.String(255))
    tertiary_school = db.Column(db.String(255))
    tertiary_year_grad = db.Column(db.String(50))
    tertiary_level_reached = db.Column(db.String(100))
    tertiary_year_attended = db.Column(db.String(50))
    grad_course = db.Column(db.String(255))
    grad_school = db.Column(db.String(255))
    grad_year_grad = db.Column(db.String(50))
    grad_level_reached = db.Column(db.String(100))
    grad_year_attended = db.Column(db.String(50))
    resume = db.Column(db.String(255))
    profile_picture = db.Column(db.String(255))
    is_verified = db.Column(db.Boolean, default=False)
    verification_status = db.Column(db.String(50), default="Unverified")
    verified_at = db.Column(db.DateTime, nullable=True)
    legal_consent = db.Column(db.Boolean, nullable=False, default=False)
    privacy_act_consent = db.Column(db.Boolean, nullable=False, default=False)
    certified_at = db.Column(db.DateTime)
    jsrf_completed = db.Column(db.Boolean, nullable=False, default=False)

    user = db.relationship("User", back_populates="job_seeker")
    applications = db.relationship("Application", back_populates="job_seeker", cascade="all, delete-orphan", lazy=True)
    job_fair_registrations = db.relationship("JobFairRegistration", back_populates="job_seeker", cascade="all, delete-orphan")
    work_history = db.relationship("JobSeekerWorkHistory", back_populates="job_seeker", cascade="all, delete-orphan", lazy=True)

    @property
    def full_name(self):
        parts = [self.first_name]
        if self.middle_name:
            parts.append(self.middle_name)
        parts.append(self.last_name)
        return " ".join([p for p in parts if p])

    @property
    def is_pwd(self):
        return bool((self.disability or "").strip())

    @property
    def is_senior_citizen(self):
        if not self.birthdate:
            return False
        today = datetime.utcnow().date()
        age = today.year - self.birthdate.year - ((today.month, today.day) < (self.birthdate.month, self.birthdate.day))
        return age >= 60

    @property
    def masked_fourps_id(self):
        value = (self.fourps_id or "").strip()
        if not value:
            return "N/A"
        return ("•" * max(4, len(value) - 4)) + value[-4:]

    @property
    def education_display(self):
        """Highest education summary from current JSRF fields."""
        if self.grad_year_grad:
            return "Graduate / Postgraduate Degree"

        grad_status = (self.grad_level_reached or "").strip().lower()
        if self.grad_course or self.grad_school or (
            grad_status and grad_status not in {"not applicable", "n/a"}
        ):
            if "graduated" in grad_status:
                return "Graduate / Postgraduate Degree"
            return "Graduate Studies / Postgraduate"

        tertiary_status = (self.tertiary_level_reached or "").strip().lower()
        if self.tertiary_year_grad or "graduated" in tertiary_status:
            course_text = (self.tertiary_course or "").lower()
            if any(token in course_text for token in (
                "associate", "2-year", "2 year", "two-year", "two year", "diploma"
            )):
                return "Associate / 2-Year College Graduate"
            return "Bachelor's Degree / College Graduate"

        if self.tertiary_course or (
            tertiary_status and tertiary_status not in {"not applicable", "n/a"}
        ):
            return "College Undergraduate / Attending"

        sec_status = (self.sec_level_reached or "").strip().lower()
        if self.senior_high_strand:
            return (
                "Senior High School Graduate"
                if self.sec_year_grad
                else "Senior High School / Attending"
            )
        if self.sec_year_grad:
            return "High School Graduate / Equivalent"
        if sec_status and sec_status not in {"not applicable", "n/a"}:
            return self.sec_level_reached

        elem_status = (self.elem_level_reached or "").strip().lower()
        if self.elem_year_grad:
            return "Elementary Graduate"
        if elem_status and elem_status not in {"not applicable", "n/a"}:
            return self.elem_level_reached

        return "No education record provided"

    @property
    def experience_display(self):
        """Use structured work_history instead of the legacy experience column."""
        history = list(self.work_history or [])
        if history:
            def sort_key(item):
                return (
                    getattr(item, "work_to", None)
                    or getattr(item, "work_from", None)
                    or datetime.min
                )
            history.sort(key=sort_key, reverse=True)
            latest = history[0]
            position = getattr(latest, "position", None) or "Position not specified"
            company = getattr(latest, "company", None) or "Company not specified"
            if len(history) == 1:
                return f"{position} at {company}"
            extra = len(history) - 1
            return f"{position} at {company} (+{extra} more work record{'s' if extra != 1 else ''})"

        legacy = (self.experience or "").strip()
        return legacy or "No work experience recorded"

    def __repr__(self):
        return f"<JobSeeker {self.job_seeker_id}>"


class JobSeekerLanguage(db.Model):
    __tablename__ = "job_seeker_languages"

    language_id = db.Column(db.Integer, primary_key=True)
    job_seeker_id = db.Column(db.Integer, db.ForeignKey("job_seekers.job_seeker_id"), nullable=False)
    language_name = db.Column(db.String(50), nullable=False)
    can_read = db.Column(db.Boolean, default=False)
    can_write = db.Column(db.Boolean, default=False)
    can_speak = db.Column(db.Boolean, default=False)
    can_understand = db.Column(db.Boolean, default=False)


class JobSeekerTVET(db.Model):
    __tablename__ = "job_seeker_tvet"

    tvet_id = db.Column(db.Integer, primary_key=True)
    job_seeker_id = db.Column(db.Integer, db.ForeignKey("job_seekers.job_seeker_id"), nullable=False)
    training_course = db.Column(db.String(150))
    hours = db.Column(db.Integer)
    institution = db.Column(db.String(150))
    skills_acquired = db.Column(db.Text)
    certificate_received = db.Column(db.String(100))


class JobSeekerLicense(db.Model):
    __tablename__ = "job_seeker_licenses"

    license_id = db.Column(db.Integer, primary_key=True)
    job_seeker_id = db.Column(db.Integer, db.ForeignKey("job_seekers.job_seeker_id"), nullable=False)
    eligibility_name = db.Column(db.String(150))
    eligibility_date = db.Column(db.Date)
    license_name = db.Column(db.String(150))
    license_validity = db.Column(db.Date)


class JobSeekerWorkHistory(db.Model):
    __tablename__ = "job_seeker_work_history"

    work_id = db.Column(db.Integer, primary_key=True)
    job_seeker_id = db.Column(db.Integer, db.ForeignKey("job_seekers.job_seeker_id"), nullable=False)
    company = db.Column(db.String(150))
    company_address = db.Column(db.String(150))
    position = db.Column(db.String(150))
    number_of_months = db.Column(db.Integer)
    work_from = db.Column(db.String(20))
    work_to = db.Column(db.String(20))
    appointment_status = db.Column(db.String(50))

    job_seeker = db.relationship("JobSeeker", back_populates="work_history")


class Job(db.Model):
    __tablename__ = "jobs"

    job_id = db.Column(db.Integer, primary_key=True)
    employer_id = db.Column(db.Integer, db.ForeignKey("employers.employer_id"), nullable=False)
    job_title = db.Column(db.String(150))
    category = db.Column(db.String(100))
    employment_type = db.Column(db.Enum("Full Time", "Part Time", "Contract", "Internship", "Temporary", name="employment_type_enum"), nullable=False)
    salary_min = db.Column(db.Numeric(10, 2))
    salary_max = db.Column(db.Numeric(10, 2))
    location = db.Column(db.String(150))
    vacancies = db.Column(db.Integer, default=1)
    description = db.Column(db.Text)
    minimum_education = db.Column(db.String(30), nullable=False, default="NONE")
    minimum_experience_months = db.Column(db.Integer, nullable=False, default=0)
    qualifications = db.Column(db.Text)
    responsibilities = db.Column(db.Text)
    deadline = db.Column(db.Date)
    job_type = db.Column(db.Enum("Job Fair", "Referral", name="job_type_enum"), nullable=False, default="Referral")
    status = db.Column(db.Enum("Pending", "Open", "Closed", name="job_status_enum"), nullable=False, default="Pending")
    approval_status = db.Column(db.Enum("Pending", "Approved", "Rejected", "Flagged"), default="Pending")
    remarks = db.Column(db.Text)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.user_id"))
    reviewed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    company_industry = db.Column(db.String(150), nullable=False, default="")

    employer = db.relationship("Employer", back_populates="jobs")
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    applications = db.relationship("Application", back_populates="job", cascade="all, delete-orphan", lazy=True)
    job_fair_entries = db.relationship("JobFairJob", back_populates="job", cascade="all, delete-orphan", lazy=True)
    structured_qualifications = db.relationship("JobQualification", back_populates="job", cascade="all, delete-orphan", lazy=True)

    def __repr__(self):
        return f"<Job {self.job_title}>"


class JobQualification(db.Model):
    __tablename__ = "job_qualifications"
    __table_args__ = (
        db.UniqueConstraint("job_id", "qualification_code", name="uq_job_qualification_code"),
    )

    job_qualification_id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False, index=True)
    qualification_code = db.Column(db.String(50), nullable=False)
    importance = db.Column(db.Enum("Required", "Preferred", name="job_qualification_importance_enum"), nullable=False, default="Required")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    job = db.relationship("Job", back_populates="structured_qualifications")

    def __repr__(self):
        return f"<JobQualification {self.job_qualification_id}: {self.qualification_code}>"


class Application(db.Model):
    __tablename__ = "applications"
    __table_args__ = (
        db.UniqueConstraint(
            "job_id",
            "job_seeker_id",
            name="uq_application_job_jobseeker",
        ),
    )

    application_id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.job_id"), nullable=False)
    job_seeker_id = db.Column(db.Integer, db.ForeignKey("job_seekers.job_seeker_id"), nullable=False)
    application_date = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.Enum("Pending", "Reviewed", "Interview", "Accepted", "Rejected"), nullable=False, default="Pending")
    cover_letter = db.Column(db.Text)
    interview_date = db.Column(db.Date, nullable=True)
    interview_time = db.Column(db.Time, nullable=True)
    interview_location = db.Column(db.String(255), nullable=True)
    interview_notes = db.Column(db.Text, nullable=True)
    interview_response_status = db.Column(db.Enum("Pending Confirmation", "Confirmed", "Unable to Attend", "Missed", name="interview_response_status_enum"), nullable=True)
    interview_response_note = db.Column(db.Text, nullable=True)
    hired_at = db.Column(db.DateTime)
    rejected_at = db.Column(db.DateTime)
    employment_ended_at = db.Column(db.DateTime, nullable=True)
    employment_end_reason = db.Column(db.String(255), nullable=True)
    work_history_id = db.Column(db.Integer, db.ForeignKey("job_seeker_work_history.work_id"), nullable=True)

    job = db.relationship("Job", back_populates="applications")
    job_seeker = db.relationship("JobSeeker", back_populates="applications")
    job_fair_evaluation = db.relationship("JobFairCandidateEvaluation", back_populates="application", uselist=False)
    referrals = db.relationship("Referral", back_populates="application", cascade="all, delete-orphan", lazy=True)

    def __repr__(self):
        return f"<Application {self.application_id}>"


class Notification(db.Model):
    __tablename__ = "notifications"

    notification_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    title = db.Column(db.String(150))
    message = db.Column(db.Text)
    notification_type = db.Column(db.Enum("Application", "Referral", "Job", "Job Fair", "Announcement", "Employer", "Employer Registration", "System"))
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", back_populates="notifications")

    def __repr__(self):
        return f"<Notification {self.notification_id}: {self.title}>"


class Referral(db.Model):
    __tablename__ = "referrals"
    __table_args__ = (
        db.UniqueConstraint(
            "application_id",
            name="uq_referral_application",
        ),
    )

    referral_id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("applications.application_id"), nullable=False)
    referred_by = db.Column(db.Integer, db.ForeignKey("admins.admin_id"))
    status = db.Column(db.String(50), default="Pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed_at = db.Column(db.DateTime)
    referral_date = db.Column(db.DateTime, default=datetime.utcnow)
    remarks = db.Column(db.Text)

    application = db.relationship("Application", back_populates="referrals")
    referrer = db.relationship("Admin", foreign_keys=[referred_by], back_populates="referrals_made")

    @property
    def job(self):
        if self.application and hasattr(self.application, "job"):
            return self.application.job
        return None

    @property
    def job_title(self):
        if self.job and hasattr(self.job, "job_title"):
            return self.job.job_title
        return "General Referral / Not Specified"

    @property
    def company_name(self):
        if self.job and hasattr(self.job, "employer") and self.job.employer:
            return self.job.employer.company_name
        if self.application and hasattr(self.application, "employer") and self.application.employer:
            return self.application.employer.company_name
        return "N/A"


class JobFair(db.Model):
    __tablename__ = "job_fairs"

    job_fair_id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    venue = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    event_type = db.Column(db.String(50), default="Mega Job Fair", nullable=False)
    lead_agency_id = db.Column(db.Integer, db.ForeignKey("employers.employer_id"), nullable=True)
    start_datetime = db.Column(db.DateTime, nullable=False)
    end_datetime = db.Column(db.DateTime, nullable=False)
    registration_deadline = db.Column(db.DateTime, nullable=False)
    queue_entry_deadline = db.Column(db.DateTime, nullable=False)
    registration_manually_closed = db.Column(db.Boolean, nullable=False, default=False)
    queue_entry_manually_closed = db.Column(db.Boolean, nullable=False, default=False)
    additional_venues = db.Column(db.JSON, nullable=True)
    structured_vacancies = db.Column(db.JSON, nullable=True)
    capacity = db.Column(db.Integer, nullable=False)
    poster_image = db.Column(db.String(255), nullable=True)
    status = db.Column(db.Enum("Upcoming", "Ongoing", "Finished", name="job_fair_status"), default="Upcoming")
    require_srs_id = db.Column(db.Boolean, default=True)
    enforce_dress_code = db.Column(db.Boolean, default=True)
    require_gov_ids = db.Column(db.Boolean, default=True)

    lead_agency = db.relationship("Employer", foreign_keys=[lead_agency_id])
    candidate_evaluations = db.relationship("JobFairCandidateEvaluation", back_populates="job_fair", cascade="all, delete-orphan")
    registrations = db.relationship("JobFairRegistration", back_populates="job_fair", cascade="all, delete-orphan")
    employer_registrations = db.relationship("JobFairEmployerRegistration", back_populates="job_fair", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<JobFair {self.job_fair_id}: {self.title} ({self.event_type})>"


class JobFairEmployerRegistration(db.Model):
    __tablename__ = "job_fair_employer_registrations"
    __table_args__ = (
        db.UniqueConstraint("job_fair_id", "employer_id", name="uq_job_fair_employer"),
    )

    registration_id = db.Column(db.Integer, primary_key=True)
    job_fair_id = db.Column(db.Integer, db.ForeignKey("job_fairs.job_fair_id"), nullable=False)
    employer_id = db.Column(db.Integer, db.ForeignKey("employers.employer_id"), nullable=False)
    registration_date = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    status = db.Column(db.Enum("Pending", "Approved", "Rejected", "Cancelled", name="employer_reg_status_enum"), default="Pending", nullable=False)
    booth_number = db.Column(db.String(50), nullable=True)
    representatives = db.Column(db.Integer, nullable=False, default=1)
    simultaneous_interview_capacity = db.Column(db.Integer, nullable=False, default=1)
    special_requests = db.Column(db.Text, nullable=True)

    candidate_evaluations = db.relationship("JobFairCandidateEvaluation", back_populates="employer_registration", cascade="all, delete-orphan")
    job_fair = db.relationship("JobFair", back_populates="employer_registrations")
    employer = db.relationship("Employer", back_populates="job_fair_registrations")
    selected_jobs = db.relationship("JobFairJob", back_populates="registration", cascade="all, delete-orphan")
    queues = db.relationship("JobFairQueue", back_populates="employer_registration", cascade="all, delete-orphan")
    interview_stations = db.relationship("JobFairInterviewStation", back_populates="employer_registration", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<JobFairEmployerRegistration {self.registration_id}>"


class JobFairInterviewStation(db.Model):
    __tablename__ = "job_fair_interview_stations"
    __table_args__ = (
        db.UniqueConstraint("employer_registration_id", "station_number", name="uq_job_fair_station_number"),
    )

    station_id = db.Column(db.Integer, primary_key=True)
    job_fair_id = db.Column(db.Integer, db.ForeignKey("job_fairs.job_fair_id"), nullable=False, index=True)
    employer_registration_id = db.Column(db.Integer, db.ForeignKey("job_fair_employer_registrations.registration_id"), nullable=False, index=True)
    station_number = db.Column(db.Integer, nullable=False)
    access_token_hash = db.Column(db.String(64), nullable=True, unique=True)
    short_code_hash = db.Column(db.String(64), nullable=True)
    token_expires_at = db.Column(db.DateTime, nullable=True)
    is_revoked = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at = db.Column(db.DateTime, nullable=True)
    initial_auto_claim_done = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    employer_registration = db.relationship("JobFairEmployerRegistration", back_populates="interview_stations")
    assigned_queues = db.relationship("JobFairQueue", back_populates="assigned_station", foreign_keys="JobFairQueue.assigned_station_id")

    def __repr__(self):
        return f"<JobFairInterviewStation {self.station_id}: Station {self.station_number}>"


class JobFairJob(db.Model):
    __tablename__ = "job_fair_jobs"
    __table_args__ = (
        db.UniqueConstraint("registration_id", "job_id", name="uq_job_fair_registration_job"),
    )

    job_fair_job_id = db.Column(db.Integer, primary_key=True)
    registration_id = db.Column(db.Integer, db.ForeignKey("job_fair_employer_registrations.registration_id"), nullable=False)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.job_id"), nullable=False)

    candidate_evaluations = db.relationship("JobFairCandidateEvaluation", back_populates="job_fair_job", cascade="all, delete-orphan")
    registration = db.relationship("JobFairEmployerRegistration", back_populates="selected_jobs")
    preselections = db.relationship("JobFairJobPreselection", back_populates="job_fair_job", cascade="all, delete-orphan", lazy=True)
    job = db.relationship("Job", back_populates="job_fair_entries")
    queue_groupings = db.relationship("JobFairJobGrouping", back_populates="job_fair_job", cascade="all, delete-orphan", lazy=True)

    def __repr__(self):
        return f"<JobFairJob {self.job_fair_job_id}>"


class JobFairRegistration(db.Model):
    __tablename__ = "job_fair_registrations"
    __table_args__ = (
        db.UniqueConstraint("job_fair_id", "job_seeker_id", name="uq_job_fair_job_seeker"),
    )

    registration_id = db.Column(db.Integer, primary_key=True)
    job_fair_id = db.Column(db.Integer, db.ForeignKey("job_fairs.job_fair_id"), nullable=False)
    job_seeker_id = db.Column(db.Integer, db.ForeignKey("job_seekers.job_seeker_id"), nullable=False)
    registration_date = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    registration_source = db.Column(
        db.Enum(
            "Pre-registered",
            "On-site",
            name="job_fair_registration_source_enum"
        ),
        nullable=False,
        default="Pre-registered"
    )
    attendance_status = db.Column(db.Enum("Registered", "Attended", "Absent", name="attendance_status_enum"), default="Registered")
    reference_id = db.Column(db.String(50), unique=True, nullable=True)
    qr_code_token = db.Column(db.String(255), unique=True, nullable=True)

    candidate_evaluations = db.relationship("JobFairCandidateEvaluation", back_populates="job_fair_registration", cascade="all, delete-orphan")
    job_fair = db.relationship("JobFair", back_populates="registrations")
    job_seeker = db.relationship("JobSeeker", back_populates="job_fair_registrations")
    preselections = db.relationship("JobFairJobPreselection", back_populates="job_fair_registration", cascade="all, delete-orphan", lazy=True)
    queues = db.relationship("JobFairQueue", back_populates="job_fair_registration", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<JobFairRegistration {self.registration_id}: JobFair {self.job_fair_id}, JobSeeker {self.job_seeker_id}, RefID {self.reference_id}>"


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    log_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=True)
    action = db.Column(db.String(255))
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", back_populates="audit_logs")

    def __repr__(self):
        return f"<AuditLog {self.log_id}: {self.action}>"


class JobFairJobPreselection(db.Model):
    __tablename__ = "job_fair_job_preselections"
    __table_args__ = (
        db.UniqueConstraint("job_fair_registration_id", "job_fair_job_id", name="uq_job_fair_registration_preselection"),
    )

    preselection_id = db.Column(db.Integer, primary_key=True)
    job_fair_registration_id = db.Column(db.Integer, db.ForeignKey("job_fair_registrations.registration_id"), nullable=False, index=True)
    job_fair_job_id = db.Column(db.Integer, db.ForeignKey("job_fair_jobs.job_fair_job_id"), nullable=False, index=True)
    preselected_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    job_fair_registration = db.relationship("JobFairRegistration", back_populates="preselections")
    job_fair_job = db.relationship("JobFairJob", back_populates="preselections")

    def __repr__(self):
        return f"<JobFairJobPreselection {self.preselection_id}: Registration {self.job_fair_registration_id}, JobFairJob {self.job_fair_job_id}>"


class JobFairJobGrouping(db.Model):
    __tablename__ = "job_fair_job_grouping"
    __table_args__ = (
        db.UniqueConstraint("queue_group_id", "job_fair_job_id", name="uq_queue_group_job"),
    )

    grouping_id = db.Column(db.Integer, primary_key=True)
    queue_group_id = db.Column(db.Integer, db.ForeignKey("job_fair_queue.queue_group_id"), nullable=False, index=True)
    job_fair_job_id = db.Column(db.Integer, db.ForeignKey("job_fair_jobs.job_fair_job_id"), nullable=False, index=True)

    queue_group = db.relationship("JobFairQueue", back_populates="job_groupings")
    job_fair_job = db.relationship("JobFairJob", back_populates="queue_groupings")

    def __repr__(self):
        return f"<JobFairJobGrouping {self.grouping_id}: QueueGroup {self.queue_group_id}, JobFairJob {self.job_fair_job_id}>"


class JobFairQueue(db.Model):
    __tablename__ = "job_fair_queue"
    __table_args__ = (
        db.UniqueConstraint("job_fair_id", "employer_registration_id", "queue_number", name="uq_queue_number_per_employer"),
        db.UniqueConstraint("job_fair_registration_id", "priority_order", name="uq_queue_registration_priority"),
    )

    queue_group_id = db.Column(db.Integer, primary_key=True)
    job_fair_id = db.Column(db.Integer, db.ForeignKey("job_fairs.job_fair_id"), nullable=False, index=True)
    job_fair_registration_id = db.Column(db.Integer, db.ForeignKey("job_fair_registrations.registration_id"), nullable=False, index=True)
    employer_registration_id = db.Column(db.Integer, db.ForeignKey("job_fair_employer_registrations.registration_id"), nullable=False, index=True)
    priority_order = db.Column(db.Integer, nullable=False)
    queue_number = db.Column(db.Integer, nullable=False)
    status = db.Column(db.Enum("Dormant", "Active", "Finished", "Cancelled", name="job_fair_queue_status_enum"), nullable=False, default="Dormant")
    queued_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    activated_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    cancellation_reason = db.Column(db.String(100), nullable=True)
    # Phase 1B-III: temporary skips keep the queue Active and preserve
    # its queue number.  The count is used only to rotate skipped
    # applicants behind applicants skipped fewer times; FIFO is still
    # preserved inside the same skip level.
    skip_count = db.Column(db.Integer, nullable=False, default=0)
    assigned_station_id = db.Column(db.Integer, db.ForeignKey("job_fair_interview_stations.station_id"), nullable=True, index=True)
    serving_started_at = db.Column(db.DateTime, nullable=True)

    job_fair = db.relationship("JobFair", backref=db.backref("queues", cascade="all, delete-orphan"))
    job_fair_registration = db.relationship("JobFairRegistration", back_populates="queues")
    employer_registration = db.relationship("JobFairEmployerRegistration", back_populates="queues")
    assigned_station = db.relationship("JobFairInterviewStation", back_populates="assigned_queues", foreign_keys=[assigned_station_id])
    job_groupings = db.relationship("JobFairJobGrouping", back_populates="queue_group", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<JobFairQueue {self.queue_group_id}: Queue {self.queue_number}, Priority {self.priority_order}, Status {self.status}>"


class JobFairTicketPrintAttempt(db.Model):
    """Durable record of a kiosk ticket dispatch attempt.

    A printer acknowledgement is not proof of physical paper output.
    Rows are append-only audit records; a retry requires an explicit new
    attempt and must not silently overwrite an uncertain outcome.
    """
    __tablename__ = "job_fair_ticket_print_attempts"

    attempt_id = db.Column(db.Integer, primary_key=True)
    request_key = db.Column(db.String(64), nullable=False, unique=True)
    queue_group_id = db.Column(db.Integer, db.ForeignKey("job_fair_queue.queue_group_id"), nullable=False, index=True)
    job_ids_json = db.Column(db.Text, nullable=False)
    status = db.Column(db.Enum("Reserved", "Acknowledged", "Uncertain", "Failed",
                               name="job_fair_ticket_print_status_enum"),
                       nullable=False, default="Reserved")
    created_at = db.Column(db.DateTime, nullable=False, server_default=db.func.current_timestamp())
    updated_at = db.Column(db.DateTime, nullable=False, server_default=db.func.current_timestamp(),
                           onupdate=db.func.current_timestamp())
    note = db.Column(db.String(255), nullable=True)


class Report(db.Model):
    __tablename__ = "reports"

    report_id = db.Column(db.Integer, primary_key=True)
    generated_by = db.Column(db.Integer, db.ForeignKey("admins.admin_id"), nullable=True)
    report_name = db.Column(db.String(150))
    report_type = db.Column(db.String(100))
    file_path = db.Column(db.String(255))
    generated_at = db.Column(db.DateTime, default=datetime.utcnow)

    generator = db.relationship("Admin", back_populates="generated_reports")

    def __repr__(self):
        return f"<Report {self.report_name}>"


class Announcement(db.Model):
    __tablename__ = "announcements"

    announcement_id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text, nullable=False)
    event_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    location = db.Column(db.String(200), nullable=False)
    city = db.Column(db.String(100), nullable=False)
    companies = db.Column(db.Integer, nullable=False)
    cities = db.Column(db.Integer, nullable=False)
    entry_label = db.Column(db.String(50), default="FREE ENTRY")
    poster_image = db.Column(db.String(255))
    status = db.Column(db.Enum("Draft", "Published", "Archived"), default="Published")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Kiosk(db.Model):
    __tablename__ = "kiosks"

    kiosk_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False, unique=True)
    kiosk_name = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.Enum("active", "disabled"), default="active", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    mode = db.Column(db.Enum("standby", "job_fair", "queue_display"), default="standby", nullable=False)
    job_fair_id = db.Column(db.Integer, db.ForeignKey("job_fairs.job_fair_id"), nullable=True)

    job_fair = db.relationship("JobFair", foreign_keys=[job_fair_id])
    user = db.relationship("User", back_populates="kiosk")

    def __repr__(self):
        return f"<Kiosk {self.kiosk_name}>"


class JobFairCandidateEvaluation(db.Model):
    __tablename__ = "job_fair_candidate_evaluations"
    __table_args__ = (
        db.UniqueConstraint("job_fair_id", "job_fair_registration_id", "employer_registration_id", "job_fair_job_id", name="uq_job_fair_candidate_employer_job_evaluation"),
    )

    evaluation_id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("applications.application_id"), nullable=False, unique=True)
    job_fair_id = db.Column(db.Integer, db.ForeignKey("job_fairs.job_fair_id"), nullable=False)
    job_fair_registration_id = db.Column(db.Integer, db.ForeignKey("job_fair_registrations.registration_id"), nullable=False)
    employer_registration_id = db.Column(db.Integer, db.ForeignKey("job_fair_employer_registrations.registration_id"), nullable=False)
    job_fair_job_id = db.Column(db.Integer, db.ForeignKey("job_fair_jobs.job_fair_job_id"), nullable=False)
    evaluated_by = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    status = db.Column(db.String(50), nullable=False)
    remarks = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    application = db.relationship("Application", back_populates="job_fair_evaluation")
    job_fair = db.relationship("JobFair", back_populates="candidate_evaluations")
    job_fair_registration = db.relationship("JobFairRegistration", back_populates="candidate_evaluations")
    employer_registration = db.relationship("JobFairEmployerRegistration", back_populates="candidate_evaluations")
    job_fair_job = db.relationship("JobFairJob", back_populates="candidate_evaluations")
    evaluator = db.relationship("User", foreign_keys=[evaluated_by])

    @property
    def job_seeker(self):
        if self.job_fair_registration:
            return self.job_fair_registration.job_seeker
        return None

    @property
    def employer(self):
        if self.employer_registration:
            return self.employer_registration.employer
        return None

class SupervisedRegistrationSession(db.Model):
    """Short-lived, staff-authorized station session; not an applicant login.

    Store only the hash of a random station bearer token. Applicant JSRF data
    must never be saved in this table or a shared administrator session.
    """

    __tablename__ = "supervised_registration_sessions"

    session_id = db.Column(db.Integer, primary_key=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    authorized_by_user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    job_fair_id = db.Column(db.Integer, db.ForeignKey("job_fairs.job_fair_id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)

    @property
    def is_usable(self):
        return self.used_at is None and self.revoked_at is None and self.expires_at > datetime.utcnow()


class SupervisedAccountClaim(db.Model):
    """One-time independent claim credential for a supervised jobseeker."""
    __tablename__ = "supervised_account_claims"
    claim_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), unique=True, nullable=False, index=True)
    reference_id = db.Column(db.String(50), nullable=False, unique=True, index=True)
    token_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)

class StaffPasswordRecovery(db.Model):
    """One-use, staff-issued recovery token. Only the SHA-256 digest is stored."""
    __tablename__ = "staff_password_recoveries"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False, index=True)
    issued_by = db.Column(db.Integer, db.ForeignKey("users.user_id"), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime)
    revoked_at = db.Column(db.DateTime)
