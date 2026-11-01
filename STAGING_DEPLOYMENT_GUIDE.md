# PESO-IMS October 12.10 — Private Staging Deployment

## Findings from supplied ZIP
- `production_server.py` already serves Flask with Waitress, behind a reverse proxy.
- `website/config.py` requires a non-default `SECRET_KEY` and a Redis rate-limit store when `APP_ENV=production`.
- `requirements-hosting.txt` lists `redis` and `rq`; `hosting_audit.py` performs a basic preflight.
- The uploaded ZIP contains `.env` and potentially sensitive uploads/signing assets. **Never upload this ZIP as-is to a public Git repository.** If any secrets have already been shared publicly, rotate them.
- MySQL and Redis must be reachable from the hosting environment. Persistent applicant uploads and signing files need a separate storage strategy.

## Recommended first step: staging on a Windows computer you control
This is easiest for verifying existing MySQL, local printer/QZ Tray, and frequent source edits before a cloud migration.

1. Back up the MySQL database and your entire project. Extract the project into a private folder.
2. Apply the supplied `.gitignore` before initializing a new private Git repository. **Note:** `.gitignore` does not untrack previously committed files; remove sensitive tracked files from Git history if applicable.
3. Use Python virtual environment and install dependencies:
   ```powershell
   py -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   pip install -r requirements-hosting.txt
   ```
4. Keep your original `.env` private. For an internet-facing staging server, configure `APP_ENV=production`, a new long random `SECRET_KEY`, `DATABASE_URL`, `RATELIMIT_STORAGE_URI=redis://...`, `SESSION_COOKIE_SECURE=true`, `PUBLIC_BASE_URL=https://YOUR-STAGING-HOST`, `TRUSTED_HOSTS=YOUR-STAGING-HOST`, and `ENABLE_SCHEDULER=false`. Set mail, queue, and QZ paths as needed. Set `BIND_HOST=127.0.0.1` and keep the application behind an HTTPS reverse proxy or authenticated tunnel.
5. Run `python hosting_audit.py` in the configured environment. Fix any failures; the audit is not a guarantee of security.
6. Start the web process: `python production_server.py`. Use a separately managed worker/scheduler only after verifying their configuration and effects.
7. Connect via a secure HTTPS reverse proxy or access-controlled tunnel. Do not expose raw MySQL, Redis, or Waitress ports publicly.
8. Test sign-in/MFA, Admin/Employer/Jobseeker role isolation, email, job fair registration, queue calls, and applicant file access with dummy data.
9. For edits: update code in the private working copy, run regression tests, back up the database, apply migrations deliberately, and restart the web process. Do not edit a live production database schema casually.

## Cloud hosting later
Use a host with Python web services plus managed MySQL and Redis, persistent private file storage, and HTTPS. A typical deployment command is `python production_server.py` with `BIND_HOST=0.0.0.0` **only inside a platform-controlled private service network**, or use an approved WSGI command suitable for the host. Check reverse-proxy trust settings carefully: `TRUSTED_PROXY` must identify the actual trusted proxy, not arbitrary internet clients. Configure web, email worker, and scheduler as separate processes; never run multiple schedulers by accident.

## Important hardware limitation
QZ Tray and physical thermal printers usually need a trusted workstation/kiosk-side bridge and correct signing certificate/key configuration. Hosting Flask remotely does **not** automatically make a local USB printer accessible to the cloud server. Test kiosk printing from the actual kiosk device.

## Phase 20 queue recovery
Queue skip/no-show/requeue/disconnection handling remains **pending**. This package focuses on safe staging prerequisites and does not change queue behavior.

## Safe deployment checks
- No `.env`, private keys, resumes, or database exports in the deployment repository.
- Use separate staging and production databases.
- HTTPS cookies and trusted host settings are configured.
- Redis rate limiting is available for production mode.
- Database backups and migration rollback steps are documented.
- Sessions and job fair flows tested with at least two separate accounts.
