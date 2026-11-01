from flask_wtf import FlaskForm
import re
import datetime

from wtforms import (
    StringField,
    EmailField,
    PasswordField,
    SubmitField, SelectField
)

from wtforms.validators import (
    DataRequired,
    Email,
    EqualTo,
    Length,
    Regexp,
    ValidationError
)


def validate_password_complexity(form, field):
    password = field.data
    if not password:
        return
    if not re.search(r"[A-Z]", password):
        raise ValidationError("Password must contain at least one uppercase letter.")
    if not re.search(r"[a-z]", password):
        raise ValidationError("Password must contain at least one lowercase letter.")
    if not re.search(r"[0-9]", password):
        raise ValidationError("Password must contain at least one digit.")
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password):
        raise ValidationError("Password must contain at least one special character.")


def validate_phone_number(form, field):
    phone = (field.data or "").strip()

    if not phone:
        return

    if not re.fullmatch(r"09\d{9}", phone):
        raise ValidationError(
            "Please enter a valid 11-digit Philippine mobile number starting with 09."
        )

    field.data = phone


def validate_url(form, field):
    url = field.data
    if not url:
        return
    if not re.match(r"^https?:\/\/(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&\/=]*)$", url):
        raise ValidationError("Please enter a valid website URL (e.g., https://example.com).")


class LoginForm(FlaskForm):

    # Keep the existing field name for compatibility with auth templates.
    email = StringField(
        "Email or Applicant Login ID",
        validators=[DataRequired(), Length(max=100)]
    )

    password = PasswordField(
        "Password",
        validators=[
            DataRequired()
        ]
    )

    submit = SubmitField("Login")


class MFAForm(FlaskForm):

    code = StringField(
        "Verification Code",
        validators=[
            DataRequired(),
            Length(min=6, max=6),
            Regexp(r"^\d{6}$", message="Enter the 6-digit verification code.")
        ]
    )

    submit = SubmitField("Verify and Continue")


class ForgotPasswordForm(FlaskForm):

    # Preserve the field name for compatibility with the existing template.
    email = StringField(
        "Email or Applicant Login ID",
        validators=[DataRequired(), Length(max=100)]
    )

    submit = SubmitField("Continue")


class ResetPasswordForm(FlaskForm):

    password = PasswordField(
        "New Password",
        validators=[
            DataRequired(),
            Length(min=8, max=128),
            validate_password_complexity
        ]
    )

    confirm_password = PasswordField(
        "Confirm Password",
        validators=[
            DataRequired(),
            Length(max=128),
            EqualTo(
                "password",
                message="Passwords must match."
            )
        ]
    )

    submit = SubmitField("Reset Password")


class RegisterJobSeekerForm(FlaskForm):

    first_name = StringField(
        "First Name",
        validators=[
            DataRequired(),
            Length(min=2, max=50)
        ]
    )

    last_name = StringField(
        "Last Name",
        validators=[
            DataRequired(),
            Length(min=2, max=50)
        ]
    )

    email = EmailField(
        "Email Address",
        validators=[
            DataRequired(),
            Email(),
            Length(max=100)
        ]
    )

    contact_number = StringField(
        "Phone Number",
        validators=[
            DataRequired(),
            Length(min=11, max=11),
            validate_phone_number
        ]
    )

    password = PasswordField(
        "Password",
        validators=[
            DataRequired(),
            Length(min=8, max=128),
            validate_password_complexity
        ]
    )

    confirm_password = PasswordField(
        "Confirm Password",
        validators=[
            DataRequired(),
            Length(max=128),
            EqualTo(
                "password",
                message="Passwords must match."
            )
        ]
    )

    submit = SubmitField(
        "Create Account"
    )


def validate_safe_text(form, field):
    value = (field.data or '').strip()
    if len(value) < 3 or not re.search(r'[A-Za-z0-9]', value) or re.search(r'[<>\x00-\x1f]', value):
        raise ValidationError('Enter meaningful text without HTML or control characters.')
    field.data = value

def validate_identifier(form, field):
    value = (field.data or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 /._-]{3,99}', value) or not re.search(r'\d', value):
        raise ValidationError('Use 4–100 letters/numbers; spaces, /, periods and hyphens are allowed.')
    field.data = value

def validate_tin(form, field):
    value = re.sub(r'[ -]', '', field.data or '')
    if not re.fullmatch(r'\d{9}(?:\d{3,5})?', value) or len(set(value)) == 1:
        raise ValidationError('Enter a 9-digit TIN, optionally followed by a 3–5 digit branch code.')
    field.data = value

def validate_year(form, field):
    value = (field.data or '').strip()
    if not re.fullmatch(r'\d{4}', value) or not 1800 <= int(value) <= datetime.date.today().year:
        raise ValidationError('Enter a valid year no later than the current year.')
    field.data = value

class EmployerRequestForm(FlaskForm):

    email = EmailField(
        "Email",
        validators=[
            DataRequired(),
            Email(),
            Length(max=100)
        ]
    )

    contact_number = StringField(
        "Contact Number",
        validators=[
            DataRequired(),
            Length(min=11, max=11),
            validate_phone_number
        ]
    )

    company_name = StringField("Registered Company Name", validators=[DataRequired(), Length(min=2, max=150), validate_safe_text])
    business_type = SelectField("Business Type", choices=[("", "Select business type")] + [(x,x) for x in ["Private Direct Hire", "Private Employment Agency (PEA)", "Contractor / Subcontractor", "Overseas Recruitment Agency"]], validators=[DataRequired(message="Select a business type.")])
    registration_number = StringField("DTI / SEC / CDA Registration Number or Agency License Number", validators=[DataRequired(), Length(max=100), validate_identifier])
    bir_no = StringField("BIR TIN / Registration Number", validators=[DataRequired(), validate_tin])
    business_permit_no = StringField("Business / Mayor's Permit Number", validators=[DataRequired(), Length(max=100), validate_identifier])
    company_address = StringField("Registered Business Address", validators=[DataRequired(), Length(min=10, max=255), validate_safe_text])
    representative_name = StringField("Authorized Representative's Full Name", validators=[DataRequired(), Length(min=5, max=150), validate_safe_text])
    representative_position = SelectField("Representative's Position", choices=[("", "Select position")] + [(x,x) for x in ["Owner / Proprietor", "President / CEO", "General Manager", "HR Manager", "Recruitment Officer", "Authorized Company Representative"]], validators=[DataRequired(message="Select a position.")])

    submit = SubmitField(
        "Request Registration"
    )


class CompleteEmployerRegistrationForm(FlaskForm):

    company_name = StringField(
        "Company Name",
        validators=[
            DataRequired(),
            Length(min=2, max=100)
        ]
    )

    # Separated Address Fields
    street = StringField('House No. / Street', validators=[
        DataRequired(message="Street address is required."),
        Length(max=100)
    ])
    barangay = StringField('Barangay', validators=[
        DataRequired(message="Barangay is required."),
        Length(max=50)
    ])
    city = StringField('Municipality / City', validators=[
        DataRequired(message="City is required."),
        Length(max=50)
    ])
    province = StringField('Province', validators=[
        DataRequired(message="Province is required."),
        Length(max=50)
    ])

    company_email = EmailField(
        "Company Email",
        validators=[
            DataRequired(),
            Email(),
            Length(max=100)
        ]
    )

    company_phone = StringField(
        "Company Contact Number",
        validators=[
            DataRequired(),
            Length(min=11, max=11),
            validate_phone_number
        ]
    )

    website = StringField(
        "Website",
        validators=[
            Length(max=150, message="Website URL cannot exceed 150 characters."),
            validate_url
        ]
    )

    company_description = StringField("Company Description", validators=[DataRequired(), Length(min=20, max=1000), validate_safe_text])
    company_industry = SelectField("Industry", choices=[("", "Select industry")] + [(x,x) for x in ["Agriculture", "Construction", "Education", "Financial Services", "Food and Beverage", "Healthcare", "Hospitality and Tourism", "Information Technology", "Logistics and Transportation", "Manufacturing", "Professional Services", "Retail and Wholesale", "Other"]], validators=[DataRequired(message="Select an industry.")])
    company_industry_other = StringField("Specify Industry", validators=[Length(max=100)])

    def validate_company_industry_other(self, field):
        if self.company_industry.data == "Other":
            value = (field.data or "").strip()
            if len(value) < 2:
                raise ValidationError("Specify your industry (at least 2 characters).")
            if value.lower() == "other":
                raise ValidationError("Enter the actual industry name.")
            if not re.search(r"[A-Za-z0-9]", value) or re.search(r"[<>\x00-\x1f]", value):
                raise ValidationError("Enter meaningful text without HTML or control characters.")
            field.data = value
        else:
            # Ignore stale text when a predefined industry is selected.
            field.data = ""

    company_size = SelectField("Company Size / Number of Employees", choices=[("", "Select company size")] + [(x,x) for x in ["1-9", "10-49", "50-99", "100-199", "200-499", "500-999", "1000+"]], validators=[DataRequired(message="Select company size.")])
    founded_year = StringField("Year Established", validators=[DataRequired(), validate_year])
    facebook_url = StringField("Facebook Page URL (Optional)", validators=[Length(max=255), validate_url])
    linkedin_url = StringField("LinkedIn URL (Optional)", validators=[Length(max=255), validate_url])

    password = PasswordField(
        "Password",
        validators=[
            DataRequired(),
            Length(min=8, max=128),
            validate_password_complexity
        ]
    )

    confirm_password = PasswordField(
        "Confirm Password",
        validators=[
            DataRequired(),
            Length(max=128),
            EqualTo("password")
        ]
    )

    submit = SubmitField(
        "Create Employer Account"
    )