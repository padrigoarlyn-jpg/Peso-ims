from flask import (
    render_template,
    redirect,
    url_for,
    flash,
    request,
    current_app,
    session
)

from flask_login import (
    login_user,
    logout_user,
    login_required,
    current_user
)

from website.extensions import limiter
from datetime import datetime, timedelta
import hashlib
import hmac
import secrets
import time
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from math import ceil
from website.models import (
    User,
    JobSeeker,
    Employer,
    Notification,
    EmployerRegistrationRequest
)

from .forms import (
    LoginForm,
    MFAForm,
    ForgotPasswordForm,
    ResetPasswordForm,
    RegisterJobSeekerForm,
    EmployerRequestForm,
    CompleteEmployerRegistrationForm
)

from website.extensions import db
from website.utils.email import send_email
from website.utils.sms import send_mfa_sms, normalize_ph_mobile
from website.utils.notifications import notify_role, notify_user
from website.utils.audit import record_audit
from . import auth


def _get_user_by_email(email):
    return User.query.filter(
        db.func.lower(User.email) == email.lower()
    ).first()


def _make_unique_username(base_username):
    username = base_username.lower().replace(" ", "").replace("-", "").replace("_", "")

    if not username:
        username = "user"

    counter = 1
    candidate = username

    while User.query.filter_by(username=candidate).first():
        candidate = f"{username}{counter}"
        counter += 1

    return candidate


def _generate_registration_token():
    return secrets.token_urlsafe(32)


def _get_password_reset_serializer():
    return URLSafeTimedSerializer(
        current_app.config["SECRET_KEY"],
        salt="password-reset-salt"
    )


def _clear_mfa_session():
    for key in (
        "mfa_user_id",
        "mfa_code_hash",
        "mfa_expires_at",
        "mfa_attempts",
        "mfa_last_sent_at",
        "mfa_sms_sent",
    ):
        session.pop(key, None)


def _clear_password_reset_mfa_session():
    for key in (
        "password_reset_mfa_user_id",
        "password_reset_mfa_code_hash",
        "password_reset_mfa_expires_at",
        "password_reset_mfa_attempts",
        "password_reset_mfa_last_sent_at",
        "password_reset_mfa_sms_sent",
    ):
        session.pop(key, None)


def _password_state_signature(user):
    # Binds a reset token to the user's current password state. Once the
    # password changes, the previously issued token can no longer be reused.
    return hashlib.sha256(user.password_hash.encode("utf-8")).hexdigest()


def _hash_mfa_code(user_id, code):
    payload = f"{user_id}:{code}".encode("utf-8")
    secret = current_app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _issue_admin_mfa_code(user):
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = int(time.time())
    lifetime = int(current_app.config.get("MFA_CODE_TTL_SECONDS", 600))

    session["mfa_user_id"] = user.user_id
    session["mfa_code_hash"] = _hash_mfa_code(user.user_id, code)
    session["mfa_expires_at"] = now + lifetime
    session["mfa_attempts"] = 0
    session["mfa_last_sent_at"] = now
    session.modified = True

    minutes = max(1, lifetime // 60)
    body = (
        f"Hello {user.username},\n\n"
        "A sign-in attempt was made for your PESO administrator account.\n\n"
        f"Your verification code is: [[important:{code}]]\n\n"
        f"[[bold:This code expires in {minutes} minutes.]] Do not share this code with anyone.\n\n"
        "If you did not attempt to sign in, you can ignore this email and consider changing your password."
    )

    # Email is the primary/fallback delivery channel and uses the existing mail architecture.
    send_email(user.email, "PESO Admin Login Verification Code", body)

    # Send the SAME OTP by SMS when the admin has a valid mobile number and
    # Twilio is configured. SMS failure never bypasses MFA and never prevents
    # the already-issued email code from being used.
    admin_profile = getattr(user, "admin", None)
    phone = getattr(admin_profile, "contact_number", None) if admin_profile else None
    sms_sent = send_mfa_sms(phone, code, minutes)
    session["mfa_sms_sent"] = bool(sms_sent)
    session.modified = True


def _hash_password_reset_mfa_code(user_id, code):
    payload = f"password-reset:{user_id}:{code}".encode("utf-8")
    secret = current_app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _issue_admin_password_reset_mfa_code(user):
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = int(time.time())
    lifetime = int(current_app.config.get("MFA_CODE_TTL_SECONDS", 600))

    session["password_reset_mfa_user_id"] = user.user_id
    session["password_reset_mfa_code_hash"] = _hash_password_reset_mfa_code(user.user_id, code)
    session["password_reset_mfa_expires_at"] = now + lifetime
    session["password_reset_mfa_attempts"] = 0
    session["password_reset_mfa_last_sent_at"] = now
    session.modified = True

    minutes = max(1, lifetime // 60)
    body = (
        f"Hello {user.username},\n\n"
        "A password reset was requested for your PESO administrator account.\n\n"
        f"Your password reset verification code is: [[important:{code}]]\n\n"
        f"[[bold:This code expires in {minutes} minutes.]] Do not share this code with anyone.\n\n"
        "If you did not request this password reset, do not use this code and contact the system administrator if necessary."
    )
    send_email(user.email, "PESO Admin Password Reset Verification Code", body)

    admin_profile = getattr(user, "admin", None)
    phone = getattr(admin_profile, "contact_number", None) if admin_profile else None
    sms_sent = send_mfa_sms(phone, code, minutes)
    session["password_reset_mfa_sms_sent"] = bool(sms_sent)
    session.modified = True


def _pending_password_reset_mfa_user():
    user_id = session.get("password_reset_mfa_user_id")
    if not user_id:
        return None

    user = db.session.get(User, user_id)
    if not user or user.role != "admin" or user.status != "active":
        _clear_password_reset_mfa_session()
        return None

    return user


def _pending_mfa_user():
    user_id = session.get("mfa_user_id")
    if not user_id:
        return None

    user = db.session.get(User, user_id)
    if not user or user.role != "admin" or user.status != "active":
        _clear_mfa_session()
        return None

    return user


@auth.route("/register")
@limiter.limit("10 per minute")
def register():
    return render_template(
        "auth/register.html"
    )


@auth.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("3 per minute")
def forgot_password():
    if current_user.is_authenticated:
        if current_user.role == "admin":
            return redirect(url_for("admin.dashboard"))

        if current_user.role == "employer":
            return redirect(url_for("employer.dashboard"))

        return redirect(url_for("jobseeker.dashboard"))

    form = ForgotPasswordForm()

    if form.validate_on_submit():
        identifier = form.email.data.strip()
        user = User.query.filter(
            (db.func.lower(User.email) == identifier.lower()) |
            (db.func.lower(User.username) == identifier.lower())
        ).first()

        if user and getattr(user, "is_unclaimed", False):
            # Do not reveal account state or send password reset links.
            flash("If the address is eligible, recovery instructions will be sent.", "info")
            return redirect(url_for("auth.forgot_password"))

        # Email-free jobseekers cannot receive a reset link. Never email the
        # internal placeholder address or reveal whether an account exists.
        if user and user.role == "jobseeker" and (
            not user.email or user.email.lower().endswith("@peso.invalid")
        ):
            flash("If your account has no registered email, visit PESO with proof of identity to request password recovery. No email was sent.", "info")
            return redirect(url_for("auth.forgot_password"))

        if user and user.role == "admin" and user.status == "active":
            # Administrators must prove possession of their registered recovery
            # channel before a reset token is issued. They are not logged in by
            # completing this verification.
            _clear_password_reset_mfa_session()
            try:
                _issue_admin_password_reset_mfa_code(user)
            except Exception:
                current_app.logger.exception("Administrator password-reset email delivery failed")
                _clear_password_reset_mfa_session()
                flash("Unable to send the verification code. Check SMTP configuration and server logs, then try again.", "danger")
                return redirect(url_for("auth.forgot_password"))
            channels = "email and SMS" if session.get("password_reset_mfa_sms_sent") else "email"
            flash(f"A 6-digit password reset verification code was sent by {channels}.", "info")
            return redirect(url_for("auth.verify_admin_password_reset"))

        if user:
            # Preserve the existing Employer/Jobseeker reset-link experience.
            serializer = _get_password_reset_serializer()
            token = serializer.dumps({
                "user_id": user.user_id,
                "purpose": "password-reset",
                "password_state": _password_state_signature(user),
            })
            reset_url = url_for("auth.reset_password", token=token, _external=True)

            body = (
                f"Hello {user.username},\n\n"
                f"You requested a password reset. Click the link below to set a new password:\n\n"
                f"{reset_url}\n\n"
                "If you did not request this change, please ignore this message."
            )

            try:
                send_email(user.email, "PESO Password Reset", body)
            except Exception:
                # Do not expose account existence, addresses, tokens, or SMTP secrets.
                current_app.logger.exception("Password reset email delivery failed")
                flash("Password recovery email could not be sent. Please try again later or contact PESO support.", "danger")
                return redirect(url_for("auth.forgot_password"))

        flash("If the account has a registered email and is eligible, recovery instructions will be sent.", "info")
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html", form=form)


@auth.route("/verify-admin-password-reset", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def verify_admin_password_reset():
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard") if current_user.role == "admin" else url_for("auth.login"))

    user = _pending_password_reset_mfa_user()
    if not user:
        flash("Your password reset verification session is no longer valid. Please start again.", "warning")
        return redirect(url_for("auth.forgot_password"))

    form = MFAForm()

    if form.validate_on_submit():
        now = int(time.time())
        expires_at = int(session.get("password_reset_mfa_expires_at", 0))

        if now > expires_at:
            _clear_password_reset_mfa_session()
            flash("Your password reset verification code has expired. Please start again.", "warning")
            return redirect(url_for("auth.forgot_password"))

        attempts = int(session.get("password_reset_mfa_attempts", 0))
        max_attempts = int(current_app.config.get("MFA_MAX_ATTEMPTS", 5))
        if attempts >= max_attempts:
            _clear_password_reset_mfa_session()
            flash("Too many incorrect verification attempts. Please start password recovery again.", "danger")
            return redirect(url_for("auth.forgot_password"))

        code = (form.code.data or "").strip()
        expected = session.get("password_reset_mfa_code_hash", "")
        supplied = _hash_password_reset_mfa_code(user.user_id, code)

        if hmac.compare_digest(expected, supplied):
            serializer = _get_password_reset_serializer()
            token = serializer.dumps({
                "user_id": user.user_id,
                "purpose": "admin-password-reset-verified",
                "password_state": _password_state_signature(user),
            })
            _clear_password_reset_mfa_session()
            return redirect(url_for("auth.reset_password", token=token))

        attempts += 1
        session["password_reset_mfa_attempts"] = attempts
        session.modified = True
        remaining = max_attempts - attempts

        if remaining <= 0:
            _clear_password_reset_mfa_session()
            flash("Too many incorrect verification attempts. Please start password recovery again.", "danger")
            return redirect(url_for("auth.forgot_password"))

        flash(f"Invalid verification code. You have {remaining} attempt(s) remaining.", "danger")

    admin_profile = getattr(user, "admin", None)
    phone = normalize_ph_mobile(getattr(admin_profile, "contact_number", None) if admin_profile else None)
    masked_phone = phone[:3] + "••••••" + phone[-3:] if phone else None
    local_part, domain = user.email.split("@", 1)
    masked_email = (local_part[:2] if len(local_part) >= 2 else local_part[:1]) + "***@" + domain

    return render_template(
        "auth/verify_password_reset_mfa.html",
        form=form,
        masked_email=masked_email,
        masked_phone=masked_phone,
        sms_sent=bool(session.get("password_reset_mfa_sms_sent")),
    )


@auth.route("/resend-admin-password-reset-code", methods=["POST"])
@limiter.limit("3 per 10 minutes")
def resend_admin_password_reset_code():
    user = _pending_password_reset_mfa_user()
    if not user:
        flash("Your password reset verification session is no longer valid. Please start again.", "warning")
        return redirect(url_for("auth.forgot_password"))

    now = int(time.time())
    cooldown = int(current_app.config.get("MFA_RESEND_COOLDOWN_SECONDS", 60))
    last_sent = int(session.get("password_reset_mfa_last_sent_at", 0))

    if now - last_sent < cooldown:
        wait = cooldown - (now - last_sent)
        flash(f"Please wait {wait} seconds before requesting another code.", "warning")
        return redirect(url_for("auth.verify_admin_password_reset"))

    try:
        _issue_admin_password_reset_mfa_code(user)
    except Exception:
        current_app.logger.exception("Administrator password-reset resend failed")
        _clear_password_reset_mfa_session()
        flash("Unable to resend the verification code. Check SMTP configuration and server logs.", "danger")
        return redirect(url_for("auth.forgot_password"))
    channels = "email and SMS" if session.get("password_reset_mfa_sms_sent") else "email"
    flash(f"A new password reset code was sent by {channels}. The previous code is no longer valid.", "success")
    return redirect(url_for("auth.verify_admin_password_reset"))


@auth.route("/cancel-admin-password-reset", methods=["POST"])
def cancel_admin_password_reset():
    _clear_password_reset_mfa_session()
    flash("Administrator password recovery was cancelled.", "info")
    return redirect(url_for("auth.login"))


@auth.route("/reset-password/<token>", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def reset_password(token):
    if current_user.is_authenticated:
        if current_user.role == "admin":
            return redirect(url_for("admin.dashboard"))

        if current_user.role == "employer":
            return redirect(url_for("employer.dashboard"))

        return redirect(url_for("jobseeker.dashboard"))

    form = ResetPasswordForm()
    serializer = _get_password_reset_serializer()

    try:
        data = serializer.loads(token, max_age=3600)
        user = db.session.get(User, data.get("user_id"))
    except SignatureExpired:
        flash("This reset link has expired. Please request a new one.", "danger")
        return redirect(url_for("auth.forgot_password"))
    except (BadSignature, TypeError, KeyError):
        flash("Invalid reset link.", "danger")
        return redirect(url_for("auth.forgot_password"))

    if not user:
        flash("Invalid reset link.", "danger")
        return redirect(url_for("auth.forgot_password"))

    if getattr(user, "is_unclaimed", False):
        flash("This account must be claimed through the PESO-assisted process.", "warning")
        return redirect(url_for("auth.login"))

    # Admin reset tokens are issued only after the recovery OTP succeeds.
    # This also rejects older administrator reset links that predate this
    # stronger recovery flow.
    purpose = data.get("purpose")
    if user.role == "admin" and purpose != "admin-password-reset-verified":
        flash("Administrator password reset requires verification. Please start again.", "warning")
        return redirect(url_for("auth.forgot_password"))

    password_state = data.get("password_state")
    # All current password-reset links are bound to the existing password state.
    # Reject legacy tokens without this binding, rather than allowing reuse.
    if not password_state or not hmac.compare_digest(password_state, _password_state_signature(user)):
        flash("This reset link is no longer valid. Please request a new one.", "warning")
        return redirect(url_for("auth.forgot_password"))

    if form.validate_on_submit():
        # A reset must create a different password, not re-save the current one.
        if user.check_password(form.password.data):
            # Report this validation failure once through the existing form-error UI.
            # Do not flash it too: the reset template renders both sources.
            form.password.errors.append("Your new password must be different from your current password.")
            return render_template("auth/reset_password.html", form=form)
        user.set_password(form.password.data)
        # Invalidate all previously issued Flask-Login and remember-me sessions.
        user.auth_version = (user.auth_version or 0) + 1
        db.session.commit()

        # Password-state binding makes this token unusable after this point.
        _clear_password_reset_mfa_session()
        _clear_mfa_session()
        if user.role == "admin":
            flash("Your password has been updated. Please sign in and complete administrator MFA.", "success")
        else:
            flash("Your password has been updated. Please sign in with your new password.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", form=form)


@auth.route("/register/jobseeker", methods=["GET", "POST"])
@limiter.limit("20 per minute")
def register_jobseeker():
    form = RegisterJobSeekerForm()

    if form.validate_on_submit():
        existing = _get_user_by_email(form.email.data)

        if existing:
            flash("Email already exists.", "danger")
            return redirect(url_for("auth.register_jobseeker"))

        try:
            username = _make_unique_username(
                f"{form.first_name.data}.{form.last_name.data}"
            )

            # Jobseekers skip manual admin approval and are active immediately
            user = User(
                username=username,
                email=form.email.data,
                role="jobseeker",
                status="active"
            )

            user.set_password(form.password.data)

            db.session.add(user)
            db.session.flush()

            seeker = JobSeeker(
                user_id=user.user_id,
                first_name=form.first_name.data,
                last_name=form.last_name.data,
                contact_number=form.contact_number.data
            )

            db.session.add(seeker)
            db.session.commit()

            flash("Registration successful! Please log in to complete your JSRF form.", "success")
            return redirect(url_for("auth.login"))

        except Exception as e:
            db.session.rollback()
            flash(f"An unexpected error occurred during registration: {str(e)}", "danger")
            return redirect(url_for("auth.register_jobseeker"))

    else:
        # Surface validation errors to screen
        for field, errors in form.errors.items():
            for error in errors:
                flash(f"{getattr(form, field).label.text}: {error}", "danger")

    return render_template(
        "auth/register_jobseeker.html",
        form=form
    )


@auth.route("/register/employer", methods=["GET", "POST"])
@limiter.limit("15 per minute")
def register_employer():
    form = EmployerRequestForm()

    if form.validate_on_submit():

        # Check if the email is already used by an existing account
        existing_user = User.query.filter(
            db.func.lower(User.email) == form.email.data.lower()
        ).first()

        if existing_user:
            flash(
                "This email address is already registered. Please use a different email address.",
                "warning"
            )
            return redirect(url_for("auth.register_employer"))

        # Check if a registration request with the same email already exists
        existing_email = EmployerRegistrationRequest.query.filter(
            db.func.lower(EmployerRegistrationRequest.email) == form.email.data.lower()
        ).first()

        if existing_email:
            flash(
                "A registration request using this email already exists.",
                "warning"
            )
            return redirect(url_for("auth.register_employer"))

        # Check if the contact number is already used in another request
        existing_number = EmployerRegistrationRequest.query.filter_by(
            contact_number=form.contact_number.data
        ).first()

        if existing_number:
            flash(
                "A registration request using this contact number already exists.",
                "warning"
            )
            return redirect(url_for("auth.register_employer"))

        # Create the registration request
        request_data = EmployerRegistrationRequest(
            email=form.email.data,
            contact_number=form.contact_number.data,
            company_name=form.company_name.data,
            business_type=form.business_type.data,
            registration_number=form.registration_number.data,
            bir_no=form.bir_no.data,
            business_permit_no=form.business_permit_no.data,
            company_address=form.company_address.data,
            representative_name=form.representative_name.data,
            representative_position=form.representative_position.data,
            status="Pending"
        )

        db.session.add(request_data)
        db.session.commit()

        # Send the employer an immediate office-visit and document checklist.
        # This uses the existing background email helper, so SMTP delivery does
        # not block the registration request.
        accreditation_email = f"""Hello,

Thank you for submitting an Employer Registration Request to the Public Employment Service Office (PESO) – City of San Jose del Monte.

Your request has been received and is pending documentary verification.

HOW TO SUBMIT YOUR REQUIREMENTS
You may choose either option:
1. Submit your documents in person at the PESO office within 3–5 working days. Office hours: Monday–Friday, 8:00 AM–5:00 PM, excluding holidays.
2. Email clear, readable copies of your documents to pesoimscsjdm@gmail.com. Please use the subject "Employer Registration Requirements - {request_data.email}" so PESO can identify your request.

For in-person verification, please bring original documents and photocopies, where applicable. If you send documents by email, PESO may still request originals or an office visit to complete verification.

GENERAL REQUIREMENTS (MANDATORY FOR ALL)
1. Letter of Intent addressed to Hon. Florida P. Robes (City Mayor), through Mr. Perfecto Jaime L. Tagalog (Action Officer, PESO).
2. Company Profile.
3. BIR Form 2303 (Certificate of Registration).
4. Duly Accomplished NSRP Form 2.
5. PhilJobNet Registration (screenshot or certificate).

SPECIFIC REQUIREMENTS (BASED ON BUSINESS TYPE)
- Private Direct Hire: DTI/SEC/CDA Registration and Mayor's Permit.
- Private Employment Agency (PEA): Valid PEA Licence and Certificate of No Pending Case (DOLE).
- Contractors/Sub-Contractors: Certificate of Registration under DO 174-17 and Certificate of No Pending Case (DOLE).
- Overseas Recruitment Agency: Valid DMW Licence and Approved Job Order (DMW, formerly POEA).

Submit the general requirements plus the specific requirements applicable to your business type. PESO may ask for additional documents or clarification during verification.

PESO Office: Right Wing, 2nd Floor, New Government Center, Brgy. Dulong Bayan, City of San Jose del Monte, Bulacan.
Documents submission email (PESO-IMS, for now): pesoimscsjdm@gmail.com

After PESO verifies your documents and approves your request, you will receive a separate email with a secure link to complete your employer account registration.

Registered email: {request_data.email}

Thank you,
PESO-IMS Team"""
        try:
            send_email(
                recipient=request_data.email,
                subject="PESO Employer Registration Request Received - Verification Requirements",
                body=accreditation_email,
            )
        except Exception as e:
            # The request itself remains valid even if the email cannot be queued.
            current_app.logger.exception(
                "Employer registration confirmation email could not be queued for request %s: %s",
                request_data.request_id,
                e,
            )

        # Notify every active PESO admin, not only the first admin account.
        notify_role(
            "admin",
            "New Employer Registration Request",
            f"{form.email.data} submitted an employer registration request.",
            "Employer Registration",
        )
        try:
            db.session.commit()
        except Exception as notification_error:
            db.session.rollback()
            current_app.logger.exception(
                "Employer registration request saved, but admin notification failed: %s",
                notification_error,
            )

        flash(
            "Your registration request was submitted. Please check your email for the official document checklist. You may submit your requirements at the PESO office or email them to pesoimscsjdm@gmail.com. PESO will review your documents before approving your account.",
            "success"
        )

        return redirect(url_for("auth.login"))

    return render_template(
        "auth/register_employer_request.html",
        form=form
    )


@auth.route("/complete-employer-registration/<token>", methods=["GET", "POST"])
@limiter.limit("15 per minute")
def complete_employer_registration(token):
    request_data = EmployerRegistrationRequest.query.filter_by(
        registration_token=token
    ).first()

    form = CompleteEmployerRegistrationForm()

    if not request_data or request_data.status != "Approved":
        flash("Invalid registration link.", "danger")
        return render_template("auth/complete_employer_registration.html", form=form, invalid_token=True)

    if request_data.token_expiry < datetime.utcnow():
        flash("Registration link expired.", "danger")
        return render_template("auth/complete_employer_registration.html", form=form, invalid_token=True)

    # Prefill approved request data on GET only; preserve submitted corrections on POST.
    if request.method == "GET":
        form.company_email.data = request_data.email or ""
        form.company_phone.data = request_data.contact_number or ""
        raw_address = (request_data.company_address or "").strip()
        if raw_address:
            parts = [x.strip() for x in raw_address.split(",") if x.strip()]
            if len(parts) >= 4:
                form.province.data = parts[-1]
                form.city.data = parts[-2]
                form.barangay.data = parts[-3]
                form.street.data = ", ".join(parts[:-3])
            elif len(parts) == 3:
                form.province.data = parts[-1]
                form.city.data = parts[-2]
                form.street.data = parts[0]
            else:
                form.street.data = raw_address

    # The legal identity is verified before approval and cannot be changed during setup.
    form.company_name.data = request_data.company_name if request.method == "GET" and request_data.company_name else form.company_name.data
    if request.method == "POST" and request_data.company_name:
        form.company_name.data = request_data.company_name

    if request.method == "POST":
        if form.validate_on_submit():
            try:
                username = _make_unique_username(form.company_name.data)

                user = User(
                    username=username,
                    email=request_data.email,
                    role="employer",
                    status="active"
                )
                user.set_password(form.password.data)
                db.session.add(user)
                db.session.flush()

                employer = Employer(
                    user_id=user.user_id,
                    company_name=form.company_name.data,
                    company_email=form.company_email.data,
                    company_phone=form.company_phone.data,
                    street=form.street.data,
                    barangay=form.barangay.data,
                    city=form.city.data,
                    province=form.province.data,
                    website=form.website.data,
                    company_description=form.company_description.data,
                    company_industry=(form.company_industry_other.data or "").strip() if form.company_industry.data == "Other" else form.company_industry.data,
                    company_size=form.company_size.data,
                    founded_year=form.founded_year.data,
                    facebook_url=form.facebook_url.data,
                    linkedin_url=form.linkedin_url.data,
                    bir_no=request_data.bir_no,
                    business_permit_no=request_data.business_permit_no,
                    mayor_permit_no=request_data.business_permit_no,
                    verified="approved"
                )

                db.session.add(employer)
                request_data.status = "Completed"
                request_data.completed_at = datetime.utcnow()

                notify_user(
                    user.user_id,
                    "Employer Account Ready",
                    "Your company registration is complete. Your PESO-IMS employer account is now active.",
                    "Employer Registration",
                )
                notify_role(
                    "admin",
                    "Employer Registration Completed",
                    f"{form.company_name.data} completed the employer account registration process.",
                    "Employer Registration",
                )

                db.session.commit()
                flash("Employer account created successfully. You may now log in to access your dashboard.", "success")
                return redirect(url_for("auth.login"))

            except Exception as e:
                db.session.rollback()
                if "1062" in str(e) or "Duplicate entry" in str(e):
                    flash("An account with this email already exists.", "warning")
                else:
                    flash(f"An unexpected error occurred: {e}", "danger")
                return redirect(url_for("auth.complete_employer_registration", token=token))
        else:
            for field, errors in form.errors.items():
                for error in errors:
                    flash(f"{getattr(form, field).label.text}: {error}", "danger")

    return render_template(
        "auth/complete_employer_registration.html",
        form=form,
        invalid_token=False,
        verified_company_name=request_data.company_name
    )


def _clear_temporary_station_session():
    """Prevent a station-only browser session from surviving account login/logout.

    A temporary interview station must be reconnected with fresh credentials
    after the browser changes its authenticated account context.
    """
    session.pop("job_fair_station_id", None)
    session.pop("job_fair_temporary_station_mode", None)


@auth.route("/login", methods=["GET", "POST"])
@limiter.limit("15 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        if current_user.role == "admin":
            return redirect(url_for("admin.dashboard"))
        elif current_user.role == "employer":
            return redirect(url_for("employer.dashboard"))
        elif current_user.role == "kiosk":
            return redirect(url_for("kiosk.dashboard"))
        elif current_user.role == "jobseeker":
            seeker = getattr(current_user, "job_seeker", None)

            # If account is active or JSRF was completed, send to dashboard
            if current_user.status == "active" or (seeker and getattr(seeker, "jsrf_completed", False)):
                return redirect(url_for("jobseeker.dashboard"))

            return redirect(url_for("jobseeker.jsrf"))

        return redirect(url_for("auth.login"))

    form = LoginForm()

    if form.validate_on_submit():
        identifier = form.email.data.strip()
        user = _get_user_by_email(identifier) if "@" in identifier else (
            User.query.filter(db.func.lower(User.username) == identifier.lower(),
                              User.role == "jobseeker").first()
        )

        if user:
            # 1. Lockout check
            if user.is_locked():
                time_left = ceil((user.locked_until - datetime.utcnow()).total_seconds() / 60)
                flash(
                    f"Account locked due to multiple failed attempts. Please try again in {max(1, time_left)} minutes.",
                    "danger"
                )
                return render_template("auth/login.html", form=form)

            # 2. Check password
            if user.check_password(form.password.data):
                if user.role in ["employer", "admin", "kiosk"] and user.status != "active":
                    flash("Your account is pending approval or inactive.", "warning")
                    return redirect(url_for("auth.login"))

                # Reset password attempts after valid credentials. Admins must
                # complete MFA before Flask-Login considers them authenticated.
                user.reset_login_attempts()

                if user.role == "admin":
                    _clear_mfa_session()
                    try:
                        _issue_admin_mfa_code(user)
                    except Exception:
                        current_app.logger.exception("Administrator login verification email delivery failed")
                        _clear_mfa_session()
                        flash("Unable to send the verification code. Check SMTP configuration and server logs, then try again.", "danger")
                        return redirect(url_for("auth.login"))
                    sms_note = " and registered mobile number" if session.get("mfa_sms_sent") else ""
                    flash(
                        f"A 6-digit verification code was sent to your administrator email{sms_note}.",
                        "info"
                    )
                    return redirect(url_for("auth.verify_mfa"))

                
                _clear_temporary_station_session()

                # Keep kiosk accounts signed in across browser restarts.
                # Other roles retain the existing session-only login behavior.
                if user.role == "kiosk":
                    login_user(user, remember=True)
                else:
                    login_user(user)
                    session["_dashboard_last_action"] = time.time()

                # Routine authentication audit entries are reserved for admins.
                # Admin sign-ins are recorded after successful MFA verification.
                

                if user.role == "employer":
                    return redirect(url_for("employer.dashboard"))
                elif user.role == "kiosk":
                    return redirect(url_for("kiosk.dashboard"))
                elif user.role == "jobseeker":
                    seeker = getattr(user, "job_seeker", None)

                    # Direct active users or those with completed JSRF to dashboard
                    if user.status == "active" or (seeker and getattr(seeker, "jsrf_completed", False)):
                        return redirect(url_for("jobseeker.dashboard"))

                    flash("Please complete your Jobseeker Registration Form (JSRF) to verify your account.", "info")
                    return redirect(url_for("jobseeker.jsrf"))

        # 3. Failed attempt handling
        if user:
            user.handle_failed_login(max_attempts=5, lockout_minutes=15)
            remaining = 5 - user.failed_login_attempts
            if remaining > 0:
                flash(f"Invalid email/Login ID or password. You have {remaining} attempts remaining.", "danger")
            else:
                flash("Too many failed attempts. Your account has been locked for 15 minutes.", "danger")
        else:
            flash("Invalid email or password.", "danger")

    return render_template(
        "auth/login.html",
        form=form
    )


@auth.route("/verify-mfa", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def verify_mfa():
    if current_user.is_authenticated:
        return redirect(url_for("admin.dashboard") if current_user.role == "admin" else url_for("auth.login"))

    user = _pending_mfa_user()
    if not user:
        flash("Your verification session is no longer valid. Please sign in again.", "warning")
        return redirect(url_for("auth.login"))

    form = MFAForm()

    if form.validate_on_submit():
        now = int(time.time())
        expires_at = int(session.get("mfa_expires_at", 0))

        if now > expires_at:
            _clear_mfa_session()
            flash("Your verification code has expired. Please sign in again.", "warning")
            return redirect(url_for("auth.login"))

        attempts = int(session.get("mfa_attempts", 0))
        max_attempts = int(current_app.config.get("MFA_MAX_ATTEMPTS", 5))
        if attempts >= max_attempts:
            _clear_mfa_session()
            flash("Too many incorrect verification attempts. Please sign in again.", "danger")
            return redirect(url_for("auth.login"))

        code = (form.code.data or "").strip()
        expected = session.get("mfa_code_hash", "")
        supplied = _hash_mfa_code(user.user_id, code)

        if hmac.compare_digest(expected, supplied):
            _clear_mfa_session()
            _clear_temporary_station_session()
            login_user(user, remember=False, fresh=True)
            session["_dashboard_last_action"] = time.time()
            user.last_login = datetime.utcnow()
            record_audit(
                "LOGIN_SUCCESS role=admin mfa=verified",
                user_id=user.user_id,
            )
            db.session.commit()
            return redirect(url_for("admin.dashboard"))

        attempts += 1
        session["mfa_attempts"] = attempts
        session.modified = True
        remaining = max_attempts - attempts

        if remaining <= 0:
            _clear_mfa_session()
            flash("Too many incorrect verification attempts. Please sign in again.", "danger")
            return redirect(url_for("auth.login"))

        flash(f"Invalid verification code. You have {remaining} attempt(s) remaining.", "danger")

    admin_profile = getattr(user, "admin", None)
    phone = normalize_ph_mobile(getattr(admin_profile, "contact_number", None) if admin_profile else None)
    masked_phone = None
    if phone:
        masked_phone = phone[:3] + "••••••" + phone[-3:]

    return render_template(
        "auth/verify_mfa.html",
        form=form,
        masked_email=user.email[:2] + "***@" + user.email.split("@", 1)[1],
        masked_phone=masked_phone,
        sms_sent=bool(session.get("mfa_sms_sent")),
    )


@auth.route("/resend-mfa", methods=["POST"])
@limiter.limit("3 per 10 minutes")
def resend_mfa():
    user = _pending_mfa_user()
    if not user:
        flash("Your verification session is no longer valid. Please sign in again.", "warning")
        return redirect(url_for("auth.login"))

    now = int(time.time())
    cooldown = int(current_app.config.get("MFA_RESEND_COOLDOWN_SECONDS", 60))
    last_sent = int(session.get("mfa_last_sent_at", 0))

    if now - last_sent < cooldown:
        wait = cooldown - (now - last_sent)
        flash(f"Please wait {wait} seconds before requesting another code.", "warning")
        return redirect(url_for("auth.verify_mfa"))

    try:
        _issue_admin_mfa_code(user)
    except Exception:
        current_app.logger.exception("Administrator MFA resend email delivery failed")
        # Clear the failed code; require a fresh sign-in rather than leave a
        # verification session that points to a code that was never delivered.
        _clear_mfa_session()
        flash("Unable to send a verification code right now. Please sign in again to retry.", "danger")
        return redirect(url_for("auth.login"))
    channels = "email and SMS" if session.get("mfa_sms_sent") else "email"
    flash(f"A new verification code was sent by {channels}. The previous code is no longer valid.", "success")
    return redirect(url_for("auth.verify_mfa"))


@auth.route("/cancel-mfa", methods=["POST"])
def cancel_mfa():
    _clear_mfa_session()
    flash("Administrator sign-in was cancelled.", "info")
    return redirect(url_for("auth.login"))


@auth.route("/logout", methods=["POST"])
@login_required
def logout():
    if current_user.role == "admin":
        record_audit(
            "LOGOUT role=admin",
            user_id=current_user.user_id,
            commit=True,
        )
    _clear_mfa_session()
    _clear_password_reset_mfa_session()
    _clear_temporary_station_session()
    logout_user()

    flash(
        "Logged out successfully.",
        "success"
    )

    return redirect(
        url_for("auth.login")
    )

@auth.route("/staff-reset-password", methods=["GET", "POST"])
@limiter.limit("5 per minute")
def staff_reset_password():
    """Applicant resets password using private one-use credential; no staff password access."""
    from website.models import StaffPasswordRecovery, AuditLog
    from flask import make_response
    from sqlalchemy.exc import SQLAlchemyError
    if current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    form = ResetPasswordForm()
    token = (request.values.get("credential") or "").strip()
    if len(token) > 200:
        token = ""
    if form.validate_on_submit():
        if not token:
            flash("Invalid or expired recovery credential.", "danger")
        else:
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
            try:
                # Lock the credential so parallel submissions cannot use it twice.
                recovery = (StaffPasswordRecovery.query.filter_by(token_hash=digest)
                            .with_for_update().first())
                now = datetime.utcnow()
                user = db.session.get(User, recovery.user_id) if recovery else None
                if (not recovery or recovery.used_at or recovery.revoked_at or
                    recovery.expires_at <= now or not user or user.role != "jobseeker" or
                    user.is_unclaimed or user.is_deleted or user.status != "active"):
                    flash("Invalid or expired recovery credential.", "danger")
                elif user.check_password(form.password.data):
                    form.password.errors.append("Your new password must be different from your current password.")
                    flash("Choose a new password that is different from your current password.", "danger")
                else:
                    user.set_password(form.password.data)
                    user.auth_version = (user.auth_version or 0) + 1
                    recovery.used_at = now
                    db.session.add(AuditLog(user_id=user.user_id,
                        action="Staff-assisted password reset completed",
                        ip_address=request.remote_addr))
                    db.session.commit()
                    flash("Password updated. Please sign in using your Applicant Login ID.", "success")
                    return redirect(url_for("auth.login"))
            except Exception:
                db.session.rollback()
                current_app.logger.exception("Staff-assisted password reset failed")
                flash("Unable to reset password. Please try again.", "danger")
    response = make_response(render_template("auth/staff_reset_password.html", form=form, credential=token))
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
