from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField
from wtforms.validators import DataRequired
from wtforms import (
    StringField,
    TextAreaField,
    SelectField,
    DateField,
    DecimalField
)

class CompanyForm(FlaskForm):
    company_name = StringField("Company Name", validators=[DataRequired()])
    company_industry = StringField("Industry")
    company_email = StringField("Email")
    company_phone = StringField("Phone")
    company_address = StringField("Address")
    company_description = TextAreaField("Description")


class JobForm(FlaskForm):

    company_name = StringField("Company Name")

    job_title = StringField(
        "Job Title",
        validators=[DataRequired()]
    )

    employment_type = SelectField(
        "Employment Type",
        choices=[
            ("Full Time", "Full-Time"),
            ("Part Time", "Part-Time"),
        ]
    )

    salary_min = DecimalField("Minimum Salary")
    salary_max = DecimalField("Maximum Salary")
    location = StringField("Location")
    deadline = DateField(
        "Deadline",
        format="%Y-%m-%d"
    )
    description = TextAreaField("Description")
    qualifications = TextAreaField("Qualifications")
    responsibilities = TextAreaField("Responsibilities")