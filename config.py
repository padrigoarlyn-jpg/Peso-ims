import os

from dotenv import load_dotenv
from datetime import timedelta


load_dotenv()


class Config:
    # Keep secrets outside source code. Set these in a local .env file.
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-this-secret-key")
    if os.getenv("APP_ENV", "development").lower() == "production" and SECRET_KEY == "dev-only-change-this-secret-key":
        raise RuntimeError("Set a secure SECRET_KEY before starting production")
    PREFERRED_URL_SCHEME = "https" if os.getenv("APP_ENV", "development").lower() == "production" else "http"
    TRUSTED_HOSTS = [h.strip() for h in os.getenv("TRUSTED_HOSTS", "").split(",") if h.strip()] or None

    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "mysql+pymysql://root:@localhost/peso_ims",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # In-memory rate limits are not shared between multiple production workers.
    RATELIMIT_STORAGE_URI = os.getenv("RATELIMIT_STORAGE_URI", "memory://")
    if os.getenv("APP_ENV", "development").lower() == "production" and RATELIMIT_STORAGE_URI == "memory://":
        raise RuntimeError("Production requires RATELIMIT_STORAGE_URI (e.g., redis://host:6379/0)")

    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

    MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))
    MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "true").lower() == "true"
    MAIL_USE_SSL = os.getenv("MAIL_USE_SSL", "false").lower() == "true"
    MAIL_USERNAME = os.getenv("MAIL_USERNAME")
    MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
    MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER") or MAIL_USERNAME

    # Production should set SESSION_COOKIE_SECURE=true only when served over HTTPS.
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"

    MAX_CONTENT_LENGTH = int(
        os.getenv(
            "MAX_CONTENT_LENGTH",
            str(12 * 1024 * 1024),
        )
    )

    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_DURATION = timedelta(days=365)

    # Administrator email MFA. Codes are never stored in plaintext.
    MFA_CODE_TTL_SECONDS = int(os.getenv("MFA_CODE_TTL_SECONDS", "600"))
    MFA_MAX_ATTEMPTS = int(os.getenv("MFA_MAX_ATTEMPTS", "5"))
    MFA_RESEND_COOLDOWN_SECONDS = int(os.getenv("MFA_RESEND_COOLDOWN_SECONDS", "60"))

    # QZ Tray signing assets. Runtime defaults match the existing qz_peso_certs folder.
    QZ_CERTIFICATE_PATH = os.getenv("QZ_CERTIFICATE_PATH")
    QZ_PRIVATE_KEY_PATH = os.getenv("QZ_PRIVATE_KEY_PATH")

    # Optional SMS delivery for administrator MFA (Twilio Programmable Messaging).
    # If disabled or unavailable, email MFA still works so administrators are not locked out.
    MFA_SMS_ENABLED = os.getenv("MFA_SMS_ENABLED", "false").lower() == "true"
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
    TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
