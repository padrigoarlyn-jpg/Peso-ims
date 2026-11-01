from flask_wtf import FlaskForm
import re
from wtforms import (
    BooleanField,
    DateField,
    DecimalField,
    EmailField,
    FileField,
    IntegerField,
    RadioField, 
    SelectField,
    SelectMultipleField,
    StringField,
    SubmitField, 
    TextAreaField,
)
from wtforms.validators import DataRequired, Email, Optional, Length, NumberRange, ValidationError
from wtforms.widgets import ListWidget, CheckboxInput
from flask_wtf.file import FileAllowed, FileField, FileRequired

class NSRPRegistrationForm(FlaskForm):
    def validate_birthdate(self, field):
        from datetime import date
        today = date.today()
        try:
            cutoff = today.replace(year=today.year - 18)
        except ValueError:  # February 29 when the cutoff year is not a leap year
            cutoff = today.replace(year=today.year - 18, day=28)
        if field.data and field.data > cutoff:
            raise ValidationError("Applicant must be at least 18 years old.")

    def validate_tin(self, field):
        if not field.data:
            return

        tin = field.data.strip()

        if not re.fullmatch(r"\d{3}-\d{3}-\d{3}(?:-\d{1,3})?", tin):
            raise ValidationError(
                "TIN must contain 9 to 12 digits, for example 123-456-789 or 123-456-789-000."
            )
    def validate_contact_number(self, field):
        """Require a valid Philippine mobile number in the regular JSRF too."""
        value = (field.data or "").strip()
        if not re.fullmatch(r"09[0-9]{9}", value):
            raise ValidationError("Mobile number must be 11 digits and start with 09.")

    def validate_former_ofw_return_date(self, field):
        """Validate the documented MM/YYYY format and calendar month."""
        value = (field.data or "").strip()
        if self.former_ofw.data != "yes" and not value:
            return
        if self.former_ofw.data == "yes" and not value:
            raise ValidationError("Enter the month and year of return (MM/YYYY).")
        if value and not re.fullmatch(r"(0[1-9]|1[0-2])/[0-9]{4}", value):
            raise ValidationError("Return date must be a valid month/year (MM/YYYY).")

    # NSRP conditional detail checks shared by the regular and supervised forms.
    # Keep the same field definitions and error messages in both workflows.
    def _require_detail(self, detail_field, message):
        field = getattr(self, detail_field)
        if not str(field.data or "").strip():
            field.errors.append(message)
            return False
        return True

    def validate(self, extra_validators=None):
        valid = super().validate(extra_validators=extra_validators)
        requirements = (
            (self.fourps.data == "yes", "fourps_id", "Enter the 4Ps household ID."),
            (self.ofw_status.data == "yes", "ofw_country", "Enter the OFW country."),
            (self.former_ofw.data == "yes", "former_ofw_country", "Enter the last country of deployment."),
            (self.employed_type.data == "self" and self.self_sector.data == "Others", "self_sector_other", "Specify the self-employment sector."),
            (self.employment_status.data == "unemployed" and self.unemployed_reason.data == "laidOffAbroad", "termination_country", "Enter the country of termination."),
            (self.employment_status.data == "unemployed" and self.unemployed_reason.data == "otherUnemployed", "unemployed_reason_other", "Specify the reason for unemployment."),
            ("Others" in (self.disability.data or []), "disability_other", "Specify the disability."),
            ("Others" in (self.skills.data or []), "skills_other", "Specify the other skill."),
        )
        for selected, name, message in requirements:
            if selected and not self._require_detail(name, message):
                valid = False
        return valid

    # ==========================================
    # STEP 1: Personal & Contact Details
    # ==========================================
    last_name = StringField('Surname', validators=[
        DataRequired(message="Surname is required."), Length(max=100)
    ])
    first_name = StringField('First Name', validators=[
        DataRequired(message="First Name is required."), Length(max=100)
    ])
    middle_name = StringField('Middle Name', validators=[Optional(), Length(max=100)])
    suffix = StringField('Suffix', validators=[Optional(), Length(max=20)])
    birthdate = DateField('Date of Birth', format='%Y-%m-%d', validators=[
        DataRequired(message="Date of Birth is required.")
    ])
    religion = StringField('Religion', validators=[Optional(), Length(max=100)])
    tin = StringField('TIN', validators=[Optional(), Length(max=50)])
    height = StringField('Height', validators=[Optional(), Length(max=50)])
    
    # Matches <div class="radio-pill-group"> for civil status
    civil_status = RadioField('Civil Status', choices=[
        ('Single', 'Single'),
        ('Married', 'Married'),
        ('Widowed', 'Widowed')
    ], default='Single', validators=[DataRequired()])
    
    # Matches <div class="radio-pill-group"> for sex
    sex = RadioField('Sex', choices=[
        ('Male', 'Male'),
        ('Female', 'Female')
    ], default='Male', validators=[DataRequired()])

    # Address
    street = StringField('House No. / Street', validators=[
        DataRequired(message="Street address is required."), Length(max=255)
    ])
    barangay = StringField('Barangay', validators=[
        DataRequired(message="Barangay is required."), Length(max=100)
    ])
    city = StringField('Municipality / City', validators=[
        DataRequired(message="City is required."), Length(max=100)
    ])
    province = StringField('Province', validators=[
        DataRequired(message="Province is required."), Length(max=100)
    ])

    # Contact Credentials
    email = EmailField('Active Email Address', validators=[
        DataRequired(message="Email is required."),
        Email(message="Please enter a valid email address."), Length(max=100)
    ])
    contact_number = StringField('Mobile Contact Number', validators=[
        DataRequired(message="Mobile contact number is required."), Length(max=50)
    ])

    # ==========================================
    # STEP 2: Current Employment Situation
    # ==========================================
    employment_status = SelectField('Current Employment Status', choices=[
        ('employed', 'Employed'),
        ('unemployed', 'Unemployed')
    ], default='employed', validators=[DataRequired()])

    fourps = RadioField('4Ps Beneficiary', choices=[
        ('yes', 'Yes'),
        ('no', 'No')
    ], validators=[Optional()])
    fourps_id = StringField('4Ps Household ID', validators=[Optional(), Length(max=100)])

    ofw_status = RadioField('OFW Status', choices=[
        ('yes', 'Yes'),
        ('no', 'No')
    ], default='no', validators=[Optional()])
    ofw_country = StringField('Country', validators=[Optional(), Length(max=100)])

    former_ofw = RadioField('Former OFW', choices=[
        ('yes', 'Yes'),
        ('no', 'No')
    ], default='no', validators=[Optional()])
    former_ofw_country = StringField('Latest Country of Deployment', validators=[Optional(), Length(max=100)])
    former_ofw_return_date = StringField('Month/Year of Return to the Philippines (MM/YYYY)', validators=[Optional(), Length(max=50)])

    employed_type = RadioField('Employment Classification Type', choices=[
        ('wage', 'Wage Employed'),
        ('self', 'Self-Employed')
    ], default='wage', validators=[Optional()])
    
    self_sector = RadioField('Self-Employed Sector', choices=[
        ('Fisherman', 'Fisherman/Fisherfolk'),
        ('Vendor', 'Vendor/Retailer'),
        ('Home-based', 'Home-based worker'),
        ('Transport', 'Transport'),
        ('Domestic', 'Domestic Worker'),
        ('Freelancer', 'Freelancer'),
        ('Artisan', 'Artisan/Craft Worker'),
        ('Others', 'Others')
    ], validators=[Optional()])
    self_sector_other = StringField('Self-Employed Sector Other', validators=[Optional(), Length(max=255)])

    unemployed_reason = SelectField('Reason for Unemployment', choices=[
        ('Fresh Graduate', 'New Entrant/Fresh Graduate'),
        ('Finished Contract', 'Finished Contract'),
        ('Resigned', 'Resigned'),
        ('Retired', 'Retired'),
        ('Laid Off Local', 'Terminated/Laid off (local)'),
        ('laidOffAbroad', 'Terminated/Laid off (abroad)'),
        ('Calamity', 'Terminated/Laid off due to calamity'),
        ('otherUnemployed', 'Others, please specify')
    ], validators=[Optional()])
    jobsearch_duration = IntegerField('Duration Looking for Work (Months)', validators=[Optional(), NumberRange(min=0, max=1200)])
    termination_country = StringField('Country of Termination', validators=[Optional(), Length(max=100)])
    unemployed_reason_other = StringField('Reason Details', validators=[Optional(), Length(max=255)])

    # Multi-select checkboxes for Disability Classification
    disability = SelectMultipleField(
        'Disability Classification',
        choices=[
            ('Visual', 'Visual Disability'),
            ('Hearing', 'Hearing Disability'),
            ('Speech', 'Speech Impairment'),
            ('Physical', 'Physical / Mobility Disability'),
            ('Mental', 'Mental / Intellectual Disability'),
            ('Others', 'Others')
        ],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        validators=[Optional()]
    )
    disability_other = StringField('Disability Details', validators=[Optional(), Length(max=255)])

    # ==========================================
    # STEP 3: Job Preferences
    # ==========================================
    pref_job_type = SelectMultipleField(
        'Preferred Employment Type',
        choices=[
            ('Full-Time', 'Full-Time'),
            ('Part-Time', 'Part-Time')
        ],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        validators=[Optional()]
    )

    pref_occupation_1 = StringField('1st Choice Preference', validators=[
        DataRequired(message="Primary job preference is required."), Length(max=150)
    ])
    pref_occupation_2 = StringField('2nd Choice Preference', validators=[Optional(), Length(max=150)])
    pref_occupation_3 = StringField('3rd Choice Preference', validators=[Optional(), Length(max=150)])

    location_scope = SelectMultipleField(
        'Preferred Work Map Location Scope',
        choices=[
            ('Local', 'Local (Within Philippines)'),
            ('Overseas', 'Overseas (International)')
        ],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        validators=[Optional()]
    )

    local_location_1 = StringField('1st Local Choice', validators=[Optional(), Length(max=100)])
    local_location_2 = StringField('2nd Local Choice', validators=[Optional(), Length(max=100)])
    local_location_3 = StringField('3rd Local Choice', validators=[Optional(), Length(max=100)])

    overseas_location_1 = StringField('1st Overseas Choice', validators=[Optional(), Length(max=100)])
    overseas_location_2 = StringField('2nd Overseas Choice', validators=[Optional(), Length(max=100)])
    overseas_location_3 = StringField('3rd Overseas Choice', validators=[Optional(), Length(max=100)])

    # ==========================================
    # STEP 5: Educational Background
    # ==========================================
    schooling_status = RadioField('Currently in School?', choices=[
        ('yes', 'Yes'),
        ('no', 'No')
    ], default='no', validators=[Optional()])

    # Elementary Level Fields
    elem_school = StringField('Elementary School Name & Address', validators=[Optional(), Length(max=255)])
    elem_year_grad = StringField('Elementary Year Graduated', validators=[Optional(), Length(max=50)])
    elem_level_reached = StringField('Elementary Level Reached', validators=[Optional(), Length(max=100)])
    elem_year_attended = StringField('Elementary Year Last Attended', validators=[Optional(), Length(max=50)])

    # Secondary Level Fields
    sec_type = SelectMultipleField(
        'Secondary Type',
        choices=[
            ('Non-K12', 'Secondary (Non-K12)'),
            ('K-12', 'Secondary (K-12)')
        ],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        validators=[Optional()]
    )
    sec_school = StringField('Secondary School Name & Address', validators=[Optional(), Length(max=255)])
    senior_high_strand = StringField('Senior High Strand', validators=[Optional(), Length(max=100)])
    sec_year_grad = StringField('Secondary Year Graduated', validators=[Optional(), Length(max=50)])
    sec_level_reached = StringField('Secondary Level Reached', validators=[Optional(), Length(max=100)])
    sec_year_attended = StringField('Secondary Year Last Attended', validators=[Optional(), Length(max=50)])

    # Tertiary Level Fields
    tertiary_course = StringField('Tertiary Course / Degree Program', validators=[Optional(), Length(max=255)])
    tertiary_school = StringField('Tertiary Institution Name', validators=[Optional(), Length(max=255)])
    tertiary_year_grad = StringField('Tertiary Year Graduated', validators=[Optional(), Length(max=50)])
    tertiary_level_reached = StringField('Tertiary Level Reached', validators=[Optional(), Length(max=100)])
    tertiary_year_attended = StringField('Tertiary Year Last Attended', validators=[Optional(), Length(max=50)])

    # Graduate Studies / Post-graduate Fields
    grad_course = StringField('Graduate Studies Degree Program', validators=[Optional(), Length(max=255)])
    grad_school = StringField('Graduate Studies Institution Name', validators=[Optional(), Length(max=255)])
    grad_year_grad = StringField('Graduate Studies Year Graduated', validators=[Optional(), Length(max=50)])
    grad_level_reached = StringField('Graduate Studies Level Reached', validators=[Optional(), Length(max=100)])
    grad_year_attended = StringField('Graduate Studies Year Last Attended', validators=[Optional(), Length(max=50)])

    # ==========================================
    # STEP 6: Eligibility & License
    # ==========================================
    no_license = BooleanField('No License / Eligibility', validators=[Optional()])

    # ==========================================
    # STEP 7: Work History Bypass Option
    # ==========================================
    fresh_grad_bypass = BooleanField('Fresh Graduate Bypass', validators=[Optional()], false_values=(False, 'false', '', None))


    skills = SelectMultipleField(
        'Other Skills Acquired Without Certificate',
        choices=[
            ('Auto Mechanic', 'Auto Mechanic'),
            ('Beautician', 'Beautician'),
            ('Carpentry Work', 'Carpentry Work'),
            ('Computer Literate', 'Computer Literate'),
            ('Domestic Chores', 'Domestic Chores'),
            ('Driver', 'Driver'),
            ('Electrician', 'Electrician'),
            ('Embroidery', 'Embroidery'),
            ('Gardening', 'Gardening'),
            ('Masonry', 'Masonry'),
            ('Painter/Artist', 'Painter/Artist'),
            ('Painting Jobs', 'Painting Jobs'),
            ('Photography', 'Photography'),
            ('Plumbing', 'Plumbing'),
            ('Sewing Dresses', 'Sewing Dresses'),
            ('Stenography', 'Stenography'),
            ('Tailoring', 'Tailoring'),
            ('Others', 'Others')
        ],
        option_widget=CheckboxInput(),
        widget=ListWidget(prefix_label=False),
        validators=[Optional()]
    )
    skills_other = StringField('Other Skill (Please Specify)', validators=[Optional(), Length(max=150)])

    legal_consent = BooleanField('Legal Consent', validators=[DataRequired(message='Certification is required.')])
    privacy_act_consent = BooleanField('Privacy Act Consent', validators=[DataRequired(message='Privacy consent is required.')])

    submit = SubmitField('Submit Registration')


class SupervisedNSRPRegistrationForm(NSRPRegistrationForm):
    # Only the supervised workflow permits applicants without an email address.
    email = EmailField("Email Address (optional)", validators=[Optional(), Email(), Length(max=100)])

    """Reuse the exact online NSRP field definitions and validators at the station.

    Do not use this form to persist a jobseeker until the supervised submit
    endpoint and atomic persistence service are implemented in a later phase.
    The station must never prefill data from current_user or another applicant.
    """

    # Supervised-only server-side guards; the regular dashboard JSRF is unchanged.
    def validate_contact_number(self, field):
        value = (field.data or "").strip()
        if not re.fullmatch(r"09[0-9]{9}", value):
            raise ValidationError("Mobile number must be 11 digits and start with 09.")

    def validate_tin(self, field):
        """Require a complete 9-digit TIN or 12-digit TIN with branch code."""
        value = (field.data or "").strip()
        if value and not re.fullmatch(r"[0-9]{3}-[0-9]{3}-[0-9]{3}(?:-[0-9]{3})?", value):
            raise ValidationError("TIN must be 123-456-789 or 123-456-789-000.")

    def validate_birthdate(self, field):
        from datetime import date
        today = date.today()
        try:
            cutoff = today.replace(year=today.year - 18)
        except ValueError:  # February 29 when the cutoff year is not a leap year
            cutoff = today.replace(year=today.year - 18, day=28)
        if field.data and field.data > cutoff:
            raise ValidationError("Applicant must be at least 18 years old.")

    def validate_height(self, field):
        value = (field.data or "").strip()
        if not value:
            return
        if not re.fullmatch(r"[0-9]{2,3}", value) or not 50 <= int(value) <= 250:
            raise ValidationError("Height must be a number between 50 and 250 cm.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Online registration uses defaults for some choice fields. A new
        # supervised applicant must make their own choices instead.
        # Never clear submitted data, including a form re-render after errors.
        from flask import request
        if request.method == "GET" and not args and not kwargs.get("data") and not kwargs.get("obj"):
            for field_name in (
                "civil_status", "sex", "employment_status", "fourps",
                "ofw_status", "former_ofw", "employed_type", "schooling_status",
            ):
                getattr(self, field_name).data = None


class ProfilePictureForm(FlaskForm):
    profile_picture = FileField(
        "Select Profile Photo",
        validators=[
            FileRequired(message="Please select an image file to upload."),
            FileAllowed(
                ["jpg", "jpeg", "png", "webp"],
                "Images only! (PNG, JPG, JPEG, WEBP)",
            ),
        ],
    )


# --------------------------------------------------------------------------
# 2. PROFESSIONAL SUMMARY FORM
# --------------------------------------------------------------------------
class ProfessionalSummaryForm(FlaskForm):
    summary = TextAreaField("Professional Summary", validators=[Optional()])


# --------------------------------------------------------------------------
# 3. PERSONAL INFORMATION & CONTACT FORM
# --------------------------------------------------------------------------
class PersonalInformationForm(FlaskForm):
    # Contact & Address (Editable in Modal)
    email = EmailField("Email Address", validators=[DataRequired(), Email()])
    contact_number = StringField(
        "Mobile Contact Number", validators=[Optional()]
    )
    street = StringField("House No. / Street", validators=[Optional()])
    barangay = StringField("Barangay", validators=[Optional()])
    city = StringField("Municipality / City", validators=[Optional()])
    province = StringField("Province", validators=[Optional()])


# --------------------------------------------------------------------------
# 4. JOB PREFERENCES FORM
# --------------------------------------------------------------------------
class JobPreferencesForm(FlaskForm):
    # Preferred Occupations
    pref_occupation_1 = StringField(
        "1st Choice Occupation", validators=[Optional()]
    )
    pref_occupation_2 = StringField(
        "2nd Choice Occupation", validators=[Optional()]
    )
    pref_occupation_3 = StringField(
        "3rd Choice Occupation", validators=[Optional()]
    )

    # Preferred Job Types (Checkboxes / Multi-select)
    pref_job_type = SelectMultipleField(
        "Preferred Employment Type",
        choices=[("Full-Time", "Full-Time"), ("Part-Time", "Part-Time")],
        validators=[Optional()],
    )

    # Local Locations
    local_location_1 = StringField(
        "1st Choice Local", validators=[Optional()]
    )
    local_location_2 = StringField(
        "2nd Choice Local", validators=[Optional()]
    )
    local_location_3 = StringField(
        "3rd Choice Local", validators=[Optional()]
    )

    # Overseas Locations
    overseas_location_1 = StringField(
        "1st Choice Overseas", validators=[Optional()]
    )
    overseas_location_2 = StringField(
        "2nd Choice Overseas", validators=[Optional()]
    )
    overseas_location_3 = StringField(
        "3rd Choice Overseas", validators=[Optional()]
    )


# --------------------------------------------------------------------------
# 5. SKILLS & COMPETENCIES FORM
# --------------------------------------------------------------------------
class SkillsForm(FlaskForm):
    skills = StringField("Skills & Competencies", validators=[Optional()])


# --------------------------------------------------------------------------
# 6. EDUCATIONAL BACKGROUND FORM
# --------------------------------------------------------------------------
class EducationForm(FlaskForm):
    # Tertiary Education
    tertiary_course = StringField(
        "Course / Degree Program", validators=[Optional()]
    )
    tertiary_school = StringField("School Name", validators=[Optional()])
    tertiary_year_grad = StringField(
        "Year Graduated", validators=[Optional()]
    )

    # Secondary Education
    senior_high_strand = StringField(
        "Senior High Strand / Curriculum", validators=[Optional()]
    )
    sec_school = StringField(
        "Secondary School Name", validators=[Optional()]
    )
    sec_year_grad = StringField("Year Graduated", validators=[Optional()])


# --------------------------------------------------------------------------
# 7. WORK EXPERIENCE FORM
# --------------------------------------------------------------------------
class WorkExperienceForm(FlaskForm):
    experience = TextAreaField(
        "Work Experience Overview", validators=[Optional()]
    )
    # Detailed Experience fields (if editing a specific role entry)
    company_name = StringField("Company Name", validators=[Optional()])
    start_date = StringField("Start Date", validators=[Optional()])
    end_date = StringField("End Date", validators=[Optional()])
    position = StringField("Position", validators=[Optional()])
    job_description = TextAreaField("Job Description", validators=[Optional()])


# --------------------------------------------------------------------------
# 8. RESUME UPLOAD FORM
# --------------------------------------------------------------------------
class ResumeUploadForm(FlaskForm):
    resume = FileField(
        "Upload New Resume",
        validators=[
            FileRequired(message="Please select a resume file."),
            FileAllowed(
                ["pdf", "doc", "docx"], "PDF or Word documents only!"
            ),
        ],
    )