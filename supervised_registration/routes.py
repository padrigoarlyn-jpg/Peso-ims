"""Staff-authorized station and independent 60-minute applicant session.

Phase 1C.2C-4C: transactional supervised applicant registration.
"""
import hashlib
import hmac
import re
import secrets
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from flask import abort, current_app, make_response, redirect, render_template, request, url_for, flash
from flask_login import current_user
from website.models import (SupervisedRegistrationSession, SupervisedAccountClaim, JobFair, User, JobSeeker,
    JobSeekerLanguage, JobSeekerTVET, JobSeekerLicense, JobSeekerWorkHistory, JobFairRegistration)
from website.extensions import db, limiter
from . import supervised_registration
from website.jobseeker.forms import SupervisedNSRPRegistrationForm
from website.utils.job_fair_lifecycle import is_registration_open

STAFF_COOKIE = "peso_supervised_station"
APPLICANT_COOKIE = "peso_supervised_applicant"
APPLICANT_SECONDS = 60 * 60
SUCCESS_COOKIE = "peso_supervised_success"


def _signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="peso-supervised-applicant-v1")


def _station(token, require_staff_valid=True):
    if not token or len(token) > 256:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    station = SupervisedRegistrationSession.query.filter_by(token_hash=digest).first()
    if not station or station.revoked_at is not None:
        return None
    if require_staff_valid and not station.is_usable:
        return None
    return station


def _applicant_station():
    signed = request.cookies.get(APPLICANT_COOKIE)
    if not signed:
        return None
    try:
        data = _signer().loads(signed, max_age=APPLICANT_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or type(data.get("id")) is not int:
        return None
    station = db.session.get(SupervisedRegistrationSession, data["id"])
    if not station or station.revoked_at is not None or station.used_at is None:
        return None
    # Require the exact one-time session activation, not just a valid signature.
    if station.used_at > datetime.utcnow() or (datetime.utcnow() - station.used_at).total_seconds() >= APPLICANT_SECONDS:
        return None
    # One session per station. A stolen/old signed cookie cannot reopen another session.
    if data.get("started") != station.used_at.isoformat():
        return None
    return station


@supervised_registration.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    # Flask-WTF requires a same-origin Referer on HTTPS form POSTs.
    # A blanket no-referrer policy breaks /supervised-registration/start.
    # Send the origin for same-origin requests, while retaining no-referrer
    # for sensitive account-claim responses.
    if request.endpoint in {
        "supervised_registration.claim_account",
        "supervised_registration.registration_complete",
    }:
        response.headers["Referrer-Policy"] = "no-referrer"
    else:
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@supervised_registration.get("/open/<token>")
def open_station(token):
    if current_user.is_authenticated:
        abort(403)
    station = _station(token)
    if not station:
        abort(404)
    response = make_response(redirect(url_for("supervised_registration.station_home")))
    response.set_cookie(STAFF_COOKIE, token, httponly=True, secure=request.is_secure,
                        samesite="Strict", max_age=15 * 60, path="/supervised-registration")
    return response


@supervised_registration.get("/")
def station_home():
    if current_user.is_authenticated:
        abort(403)
    station = _applicant_station()
    if station:
        fair = db.session.get(JobFair, station.job_fair_id)
        if not fair:
            abort(404)
        return render_template("supervised_registration/station_home.html", fair=fair,
                               applicant_active=True, expires_at=None)
    station = _station(request.cookies.get(STAFF_COOKIE))
    if not station:
        abort(403)
    fair = db.session.get(JobFair, station.job_fair_id)
    if not fair:
        abort(404)
    return render_template("supervised_registration/station_home.html", fair=fair,
                           applicant_active=False,
                           expires_at_pht=station.expires_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("Asia/Manila")))


@supervised_registration.post("/start")
def start_applicant():
    if current_user.is_authenticated:
        abort(403)
    station = _station(request.cookies.get(STAFF_COOKIE))
    if not station:
        abort(403)
    # Atomic claim prevents two applicant sessions being started on the same station.
    changed = (SupervisedRegistrationSession.query
               .filter_by(session_id=station.session_id, used_at=None, revoked_at=None)
               .filter(SupervisedRegistrationSession.expires_at > datetime.utcnow())
               .update({"used_at": datetime.utcnow()}, synchronize_session=False))
    if changed != 1:
        db.session.rollback()
        abort(409)
    db.session.commit()
    db.session.refresh(station)
    signed = _signer().dumps({"id": station.session_id,
                              "started": station.used_at.isoformat()})
    response = make_response(redirect(url_for("supervised_registration.station_home")))
    response.set_cookie(APPLICANT_COOKIE, signed, httponly=True, secure=request.is_secure,
                        samesite="Strict", max_age=APPLICANT_SECONDS,
                        path="/supervised-registration")
    response.delete_cookie(STAFF_COOKIE, path="/supervised-registration")
    return response


@supervised_registration.post("/end")
def end_station():
    if current_user.is_authenticated:
        abort(403)
    station = _applicant_station() or _station(request.cookies.get(STAFF_COOKIE))
    if not station:
        abort(403)
    station.revoked_at = datetime.utcnow()
    db.session.commit()
    response = make_response(redirect(url_for("supervised_registration.station_ended")))
    response.delete_cookie(STAFF_COOKIE, path="/supervised-registration")
    response.delete_cookie(APPLICANT_COOKIE, path="/supervised-registration")
    return response


@supervised_registration.get("/ended")
def station_ended():
    return render_template("supervised_registration/station_ended.html")


@supervised_registration.route("/jsrf", methods=["GET", "POST"])
def jsrf_preview():
    """Applicant-only form; save the complete NSRP in one transaction."""
    if current_user.is_authenticated:
        abort(403)
    station = _applicant_station()
    if not station:
        abort(403)
    fair = db.session.get(JobFair, station.job_fair_id)
    if not fair:
        abort(404)
    try:
        _, issued_at = _signer().loads(request.cookies.get(APPLICANT_COOKIE),
            max_age=APPLICANT_SECONDS, return_timestamp=True)
    except (BadSignature, SignatureExpired):
        abort(403)
    if not is_registration_open(fair):
        abort(409, description="Job Fair registration has closed. Please ask PESO staff for assistance.")
    # Reject oversized requests BEFORE parsing form data or constructing WTForms.
    # request.content_length may be absent (e.g. chunked requests); Flask's
    # max_content_length also bounds parsing when the size is not advertised.
    if request.method == "POST":
        if request.content_length is not None and request.content_length > 256 * 1024:
            abort(413, description="Registration form is too large")
        request.max_content_length = 256 * 1024
    # Unique to this activated applicant session, not merely its expiry time.
    draft_session = hashlib.sha256(
        f"{station.session_id}:{station.used_at.isoformat()}".encode("utf-8")
    ).hexdigest()
    form = SupervisedNSRPRegistrationForm()
    if request.method == "POST":
        # WTForms generally reads the first value of duplicate scalar fields.
        # Refuse ambiguous payloads instead of accepting attacker-controlled
        # alternate values. Repeatable-row fields have their own strict checks.
        # WTForms SelectMultipleField intentionally sends the same name once
        # per selected checkbox. Reject duplicates only for scalar controls.
        # Dynamic repeating rows use indexed names and are checked separately.
        from wtforms import SelectMultipleField
        for name, field in form._fields.items():
            if isinstance(field, SelectMultipleField):
                continue
            if len(request.form.getlist(name)) > 1:
                abort(400, description="Duplicate registration field")
        if not form.validate_on_submit():
            return render_template("supervised_registration/jsrf_preview.html", form=form,
                fair=fair, expires_at=(issued_at + timedelta(seconds=APPLICANT_SECONDS)).isoformat(),
                draft_session=draft_session), 400
        email = (form.email.data or "").strip().lower()
        if email and User.query.filter(db.func.lower(User.email) == email).first():
            form.email.errors.append("This email already has an account. Please ask PESO staff for assistance.")
            return render_template("supervised_registration/jsrf_preview.html", form=form,
                fair=fair, expires_at=(issued_at + timedelta(seconds=APPLICANT_SECONDS)).isoformat(),
                draft_session=draft_session), 409
        try:
            # Recheck the applicant session after form validation, before any writes.
            if not _applicant_station():
                abort(403, description="Registration session has expired")
            # Revalidate and atomically claim the station within the same transaction.
            updated = (SupervisedRegistrationSession.query
                .filter_by(session_id=station.session_id, revoked_at=None)
                .filter(SupervisedRegistrationSession.used_at > datetime.utcnow() - timedelta(seconds=APPLICANT_SECONDS))
                .update({"revoked_at": datetime.utcnow()}, synchronize_session=False))
            if updated != 1:
                db.session.rollback()
                abort(409)
            # Serialize fair capacity checks to prevent concurrent overbooking.
            locked_fair = (JobFair.query.filter_by(job_fair_id=fair.job_fair_id)
                           .with_for_update().first())
            if not locked_fair or not is_registration_open(locked_fair):
                raise ValueError("Registration is closed")
            if (JobFairRegistration.query
                    .filter_by(job_fair_id=locked_fair.job_fair_id).count() >= locked_fair.capacity):
                raise ValueError("Job Fair capacity reached")
            # Database email is non-nullable; reserve an undeliverable internal value
            # for applicants without email, never expose it as a contact address.
            login_id = "PESO-" + secrets.token_hex(8).upper()
            stored_email = email or ("noemail-" + secrets.token_hex(16) + "@peso.invalid")
            user = User(username=login_id, email=stored_email,
                password_hash=generate_password_hash(secrets.token_urlsafe(48)),
                role="jobseeker", status="pending", email_verified=False,
                is_unclaimed=True)
            db.session.add(user)
            db.session.flush()
            seeker = JobSeeker(user_id=user.user_id)
            # Only model-backed fields are copied; email belongs to User.
            excluded = {"email", "submit", "csrf_token", "no_license", "fresh_grad_bypass"}
            for name, field in form._fields.items():
                if name in excluded or not hasattr(JobSeeker, name):
                    continue
                value = field.data
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value)
                if name == "fourps":
                    continue
                if value is not None:
                    setattr(seeker, name, value)
            seeker.fourps_beneficiary = form.fourps.data == "yes"
            seeker.legal_consent = bool(form.legal_consent.data)
            seeker.privacy_act_consent = bool(form.privacy_act_consent.data)
            seeker.certified_at = datetime.utcnow()
            seeker.jsrf_completed = True
            seeker.verification_status = "Unverified"
            db.session.add(seeker)
            db.session.flush()
            _save_repeating(seeker.job_seeker_id)
            # Unique database index on reference_id protects against collisions.
            reference_id = secrets.token_hex(8).upper()
            registration = JobFairRegistration(
                job_fair_id=locked_fair.job_fair_id,
                job_seeker_id=seeker.job_seeker_id,
                registration_source="On-site",
                attendance_status="Registered",
                reference_id=reference_id,
            )
            db.session.add(registration)
            db.session.flush()
            # Generate the private claim secret in the same transaction as registration.
            claim_code = secrets.token_hex(16).upper()
            db.session.add(SupervisedAccountClaim(
                user_id=user.user_id, reference_id=reference_id,
                token_hash=hashlib.sha256(claim_code.encode("ascii")).hexdigest(),
                expires_at=datetime.utcnow() + timedelta(days=30)))
            db.session.commit()
        except (ValueError, IntegrityError) as exc:
            db.session.rollback()
            # Repeating-row validation failures are form-level errors, not email errors.
            # Keep database errors generic; never expose internal SQL details.
            if isinstance(exc, ValueError):
                message = str(exc)
                safe_messages = {
                    "Unexpected repeatable-row field", "Duplicate repeatable-row field",
                    "Invalid language proficiency value", "Invalid numeric field",
                    "Field too long", "Work end date cannot precede start date",
                    "Eligibility date cannot be in the future",
                }
                if message not in safe_messages and not message.startswith("Incomplete repeatable row:"):
                    message = "Please check the repeating entries and try again."
            else:
                message = "Unable to complete registration. Please ask PESO staff for assistance."
            form.submit.errors.append(message)
            return render_template("supervised_registration/jsrf_preview.html", form=form,
                fair=fair, expires_at=(issued_at + timedelta(seconds=APPLICANT_SECONDS)).isoformat(),
                draft_session=draft_session), 400
        # PRG: display the reference once, without leaving personal details in URL/history.
        success_token = _success_signer().dumps({"ref": reference_id, "fair": fair.title, "claim": claim_code, "login_id": login_id})
        response = make_response(redirect(url_for("supervised_registration.registration_complete")))
        response.set_cookie(SUCCESS_COOKIE, success_token, httponly=True,
                            secure=request.is_secure, samesite="Strict", max_age=300,
                            path="/supervised-registration")
        response.delete_cookie(APPLICANT_COOKIE, path="/supervised-registration")
        response.delete_cookie(STAFF_COOKIE, path="/supervised-registration")
        return response
    return render_template("supervised_registration/jsrf_preview.html", form=form,
        fair=fair, expires_at=(issued_at + timedelta(seconds=APPLICANT_SECONDS)).isoformat(),
        draft_session=draft_session)


def _success_signer():
    return URLSafeTimedSerializer(current_app.secret_key, salt="peso-supervised-success-v1")


@supervised_registration.get("/complete")
def registration_complete():
    if current_user.is_authenticated:
        abort(403)
    token = request.cookies.get(SUCCESS_COOKIE)
    if not token:
        abort(403)
    try:
        result = _success_signer().loads(token, max_age=300)
    except (BadSignature, SignatureExpired):
        abort(403)
    if not isinstance(result, dict) or not re.fullmatch(r"[A-F0-9]{16}", result.get("ref", "")):
        abort(403)
    response = make_response(render_template(
        "supervised_registration/registration_complete.html",
        reference_id=result["ref"], claim_code=result.get("claim"),
        login_id=result.get("login_id"),
        fair_title=result.get("fair", "Job Fair")))
    response.delete_cookie(SUCCESS_COOKIE, path="/supervised-registration")
    # The one-time claim secret is displayed here. Prevent caching or referrer leakage.
    response.headers["Cache-Control"] = "no-store, no-cache, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@supervised_registration.route("/claim", methods=["GET", "POST"])
@limiter.limit("8 per hour", methods=["POST"])
def claim_account():
    """Self-service activation with private one-time secret; never use kiosk ref alone."""
    if current_user.is_authenticated:
        return redirect(url_for("auth.login"))
    error = None
    if request.method == "POST":
        ref = (request.form.get("reference_id") or "").strip().upper()
        code = re.sub(r"[\s-]", "", request.form.get("claim_code") or "").upper()
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm_password") or ""
        if not re.fullmatch(r"[A-F0-9]{16}", ref) or not re.fullmatch(r"[A-F0-9]{32}", code):
            error = "Invalid or expired claim credentials. Contact PESO if you need a replacement."
        elif (len(password) < 8 or len(password) > 128
              or not re.search(r"[A-Z]", password)
              or not re.search(r"[a-z]", password)
              or not re.search(r"[0-9]", password)
              or not re.search(r'[!@#$%^&*(),.?":{}|<>]', password)):
            error = "Use 8–128 characters with uppercase and lowercase letters, a number, and a special character."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            try:
                claim = (SupervisedAccountClaim.query
                         .filter_by(reference_id=ref).with_for_update().first())
                user = db.session.get(User, claim.user_id) if claim else None
                digest = hashlib.sha256(code.encode("ascii")).hexdigest()
                valid = (claim is not None and user is not None and user.role == "jobseeker"
                         and bool(user.is_unclaimed) and not user.is_deleted
                          and user.status == "pending" and claim.used_at is None
                         and claim.revoked_at is None and claim.expires_at > datetime.utcnow()
                         and hmac.compare_digest(claim.token_hash, digest))
                if not valid:
                    error = "Invalid or expired claim credentials. Contact PESO if you need a replacement."
                    db.session.rollback()
                else:
                    user.set_password(password)
                    user.is_unclaimed = False
                    # Supervised accounts start as pending; Flask-Login's user_loader
                    # accepts only active accounts. Activate login access after
                    # successful one-time claiming, without marking PESO identity
                    # verification or the email address as verified.
                    user.status = "active"
                    # Match normal jobseeker JSRF completion: a complete, certified
                    # supervised JSRF qualifies for the same application-level
                    # verification once the applicant has claimed the account.
                    # Claiming alone never verifies an incomplete JSRF.
                    seeker = JobSeeker.query.filter_by(user_id=user.user_id).first()
                    if seeker and seeker.jsrf_completed and seeker.certified_at:
                        seeker.is_verified = True
                        seeker.verification_status = "Verified"
                        seeker.verified_at = seeker.verified_at or datetime.utcnow()
                    # Email remains unverified. Do not infer ownership of an email address.
                    claim.used_at = datetime.utcnow()
                    db.session.commit()
                    flash("Account claimed successfully. You can now sign in.", "success")
                    return redirect(url_for("auth.login"))
            except Exception:
                db.session.rollback()
                current_app.logger.exception("Account claim failed")
                error = "Unable to activate the account right now. Please try again later."
    response = make_response(render_template("supervised_registration/claim_account.html", error=error))
    response.headers["Cache-Control"] = "no-store, no-cache, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


# Explicit allowlists prevent mass assignment and unexpected row data.
_REPEAT_GROUPS = (
    ("language", JobSeekerLanguage, ("language_name", "can_read", "can_write", "can_speak", "can_understand"), "language_name"),
    ("tvet", JobSeekerTVET, ("training_course", "hours", "institution", "skills_acquired", "certificate_received"), "training_course"),
    ("license", JobSeekerLicense, ("eligibility_name", "eligibility_date", "license_name", "license_validity"), None),
    ("work", JobSeekerWorkHistory, ("company", "company_address", "position", "number_of_months", "work_from", "work_to", "appointment_status"), "company"),
)


def _save_repeating(seeker_id):
    """Validate repeatable records before flush; caller owns the transaction."""
    for prefix, model, fields, required_key in _REPEAT_GROUPS:
        pattern = re.compile(r"^" + re.escape(prefix) + r"_(\d+)_([a-z_]+)$")
        for key in request.form.keys():
            if not key.startswith(prefix + "_"):
                continue
            match = pattern.fullmatch(key)
            if not match or int(match.group(1)) >= 10 or match.group(2) not in fields:
                raise ValueError("Unexpected repeatable-row field")
            if len(request.form.getlist(key)) != 1:
                raise ValueError("Duplicate repeatable-row field")
        for i in range(10):
            values = {}
            for key in fields:
                raw = request.form.get(f"{prefix}_{i}_{key}", "").strip()
                if key.startswith("can_"):
                    if raw not in ("", "on"):
                        raise ValueError("Invalid language proficiency value")
                    values[key] = raw == "on"
                elif key in ("hours", "number_of_months"):
                    if raw:
                        if not raw.isdecimal() or int(raw) > 100000:
                            raise ValueError("Invalid numeric field")
                        values[key] = int(raw)
                elif key in ("eligibility_date", "license_validity", "work_from", "work_to"):
                    if raw:
                        values[key] = date.fromisoformat(raw)
                elif raw:
                    column = model.__table__.columns.get(key)
                    if column is not None and getattr(column.type, "length", None) and len(raw) > column.type.length:
                        raise ValueError("Field too long")
                    values[key] = raw
            has_content = any(bool(value) for value in values.values())
            if not has_content:
                continue
            if required_key and not values.get(required_key):
                raise ValueError("Incomplete repeatable row: " + prefix)
            if prefix == "license" and not (values.get("eligibility_name") or values.get("license_name")):
                raise ValueError("Eligibility or license name required")
            if prefix == "license" and values.get("eligibility_date") and values["eligibility_date"] > date.today():
                raise ValueError("Eligibility date cannot be in the future")
            if prefix == "work" and values.get("work_from") and values.get("work_to") and values["work_to"] < values["work_from"]:
                raise ValueError("Work end date cannot precede start date")
            db.session.add(model(job_seeker_id=seeker_id, **values))
