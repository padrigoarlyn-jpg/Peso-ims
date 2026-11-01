"""
Scheduled tasks for PESO-IMS

Handles automatic status updates for:
- Job Fair statuses
- Job deadline expiration
- Automatic conversion of expired Job Fair jobs
  into Referral jobs when vacancies remain
"""

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from website.extensions import db

from website.models import (
    JobFair,
    Job,
    User,
    Application
)

from website.utils.notifications import notify_role
from website.utils.email import send_email


# ============================================================
# PHILIPPINE TIMEZONE
# ============================================================

PHILIPPINE_TZ = ZoneInfo("Asia/Manila")


# ============================================================
# SCHEDULER WRAPPER
# ============================================================

def _interview_reminders_task(app):
    """Create interview reminders outside normal page requests."""

    try:
        with app.app_context():
            # Local import avoids coupling module import order while the Flask
            # application is still being constructed.
            from website.utils.notifications import create_upcoming_interview_reminders

            create_upcoming_interview_reminders()

    except Exception as error:
        app.logger.exception(
            "Scheduled interview reminder task failed: %s",
            error,
        )


def _update_job_fair_statuses_task(app):
    """
    Wrapper function called by APScheduler.

    Executes the scheduled task using the passed Flask
    application instance context.
    """

    try:

        with app.app_context():

            _perform_job_fair_status_update()

    except Exception as e:

        print(
            f"[Scheduler Error] "
            f"Failed to update job/job fair statuses: {e}"
        )


# ============================================================
# MAIN STATUS UPDATE TASK
# ============================================================

def _perform_job_fair_status_update():
    """
    Performs automatic status updates for:

    JOB FAIRS
    1. Upcoming → Ongoing
    2. Ongoing fairs remain Ongoing after end time until formally finished by Admin.

    JOBS
    3. Expired Job Fair jobs:
       - If vacancies remain:
         Job Fair → Referral
         Closed/Open → Open
         Deadline extended by 7 days

       - If no vacancies remain:
         Closed

    4. Expired Referral jobs:
       Open → Closed

    This function must be called inside a Flask
    application context.
    """

    try:

        # ========================================================
        # CURRENT TIME
        # ========================================================

        # Job Fair datetimes are entered and stored as Philippine local wall time.
        # Compare them in Asia/Manila instead of incorrectly treating naive DB
        # datetimes as UTC (which shifts lifecycle changes by 8 hours).
        now = datetime.now(PHILIPPINE_TZ)

        print(
            "[Scheduler] ========================================"
        )

        print(
            f"[Scheduler] Status check started at {now}"
        )

        print(
            "[Scheduler] ========================================"
        )


        # ========================================================
        # PHILIPPINE DATE
        # ========================================================

        today = datetime.now(
            PHILIPPINE_TZ
        ).date()

        print(
            f"[Scheduler] Philippine date: {today}"
        )


        # ========================================================
        # JOB FAIR
        #
        # Upcoming → Ongoing
        # ========================================================

        upcoming_fairs = JobFair.query.filter_by(
            status="Upcoming"
        ).all()

        for fair in upcoming_fairs:

            if not fair.start_datetime:
                continue

            # ----------------------------------------------------
            # Make datetime timezone-aware
            # ----------------------------------------------------

            start_dt = (
                fair.start_datetime
                if fair.start_datetime.tzinfo
                else fair.start_datetime.replace(
                    tzinfo=PHILIPPINE_TZ
                )
            )

            # ----------------------------------------------------
            # Start job fair
            # ----------------------------------------------------

            if start_dt <= now:

                fair.status = "Ongoing"

                print(
                    f"[Scheduler] Job Fair '{fair.title}' "
                    f"changed from Upcoming → Ongoing"
                )


                # =================================================
                # NOTIFICATION INFORMATION
                # =================================================

                job_fair_date = (
                    fair.start_datetime.strftime(
                        "%B %d, %Y at %I:%M %p"
                    )
                )

                notification_title = (
                    f"Job Fair Now Ongoing: {fair.title}"
                )

                notification_message = (
                    f"The job fair '{fair.title}' is now ongoing! "
                    f"Date: {job_fair_date}. "
                    f"Visit the venue or register if you "
                    f"haven't already."
                )

                email_subject = (
                    f"Job Fair Is Now Ongoing: {fair.title}"
                )

                email_message = (
                    f"Hello,\n\n"
                    f"The job fair '{fair.title}' is now ongoing!\n\n"
                    f"Date: {job_fair_date}\n"
                    f"Venue: {fair.venue or 'TBA'}\n\n"
                    f"Visit the venue or register on the "
                    f"PESO IMS system if you haven't already.\n\n"
                    f"Best regards,\n"
                    f"PESO-IMS Team"
                )


                # =================================================
                # NOTIFY JOBSEEKERS
                # =================================================

                try:

                    notify_role(
                        "jobseeker",
                        notification_title,
                        notification_message,
                        "Job Fair"
                    )

                except Exception as e:

                    print(
                        f"[Scheduler Error] Failed to notify "
                        f"jobseekers about job fair "
                        f"'{fair.title}': {e}"
                    )


                # =================================================
                # NOTIFY EMPLOYERS
                # =================================================

                try:

                    notify_role(
                        "employer",
                        notification_title,
                        notification_message,
                        "Job Fair"
                    )

                except Exception as e:

                    print(
                        f"[Scheduler Error] Failed to notify "
                        f"employers about job fair "
                        f"'{fair.title}': {e}"
                    )


                # =================================================
                # EMAIL JOBSEEKERS
                # =================================================

                jobseekers = User.query.filter_by(
                    role="jobseeker",
                    is_deleted=False,
                    status="active"
                ).all()

                for js in jobseekers:

                    try:

                        send_email(
                            js.email,
                            email_subject,
                            email_message
                        )

                    except Exception as e:

                        print(
                            f"[Scheduler Error] Failed to send "
                            f"job fair ongoing email to "
                            f"{js.email}: {e}"
                        )


                # =================================================
                # EMAIL EMPLOYERS
                # =================================================

                employers = User.query.filter_by(
                    role="employer",
                    is_deleted=False,
                    status="active"
                ).all()

                for emp in employers:

                    try:

                        send_email(
                            emp.email,
                            email_subject,
                            email_message
                        )

                    except Exception as e:

                        print(
                            f"[Scheduler Error] Failed to send "
                            f"job fair ongoing email to "
                            f"{emp.email}: {e}"
                        )


        # ========================================================
        # JOB FAIR
        #
        # Ongoing past end time → remains Ongoing until Admin finishes
        # ========================================================

        ongoing_fairs = JobFair.query.filter_by(
            status="Ongoing"
        ).all()

        for fair in ongoing_fairs:

            if not fair.end_datetime:
                continue

            # ----------------------------------------------------
            # Make datetime timezone-aware
            # ----------------------------------------------------

            end_dt = (
                fair.end_datetime
                if fair.end_datetime.tzinfo
                else fair.end_datetime.replace(
                    tzinfo=PHILIPPINE_TZ
                )
            )

            # ----------------------------------------------------
            # Finish job fair
            # ----------------------------------------------------

            if end_dt <= now:

                # Phase 1D: reaching the scheduled end no longer formally
                # finishes the fair. PESO may continue draining existing
                # queues until an Admin explicitly finishes the event.
                print(
                    f"[Scheduler] Job Fair '{fair.title}' reached its scheduled "
                    f"end and remains Ongoing while existing queues are processed."
                )
                continue


        # ========================================================
        # JOB DEADLINE PROCESSING
        # ========================================================
        #
        # There are now TWO different expiration paths:
        #
        # 1. Job Fair jobs
        #    Open OR Closed + expired
        #
        #    If vacancies remain:
        #        Job Fair → Referral
        #        Status → Open
        #        Deadline → +7 days
        #
        #    If no vacancies remain:
        #        Status → Closed
        #
        # 2. Referral jobs
        #    Open + expired
        #
        #    Status → Closed
        #
        # ========================================================


        # ========================================================
        # EXPIRED JOB FAIR JOBS
        #
        # IMPORTANT:
        #
        # This includes BOTH Open and Closed jobs.
        #
        # This is the fix for the issue where a Job Fair job
        # was already changed to Closed and could no longer
        # be detected by the scheduler.
        # ========================================================

        expired_job_fair_jobs = Job.query.filter(
            Job.job_type == "Job Fair",
            Job.status.in_(["Open", "Closed"]),
            Job.deadline.isnot(None),
            Job.deadline < today
        ).all()

        print(
            f"[Scheduler] Expired Job Fair jobs found: "
            f"{len(expired_job_fair_jobs)}"
        )


        # ========================================================
        # PROCESS EXPIRED JOB FAIR JOBS
        # ========================================================

        converted_job_fair_count = 0
        closed_job_fair_count = 0

        for job in expired_job_fair_jobs:

            print(
                "[Scheduler] ----------------------------------------"
            )

            print(
                "[Scheduler] Processing expired Job Fair job"
            )

            print(
                f"    Job ID: {job.job_id}"
            )

            print(
                f"    Job Title: {job.job_title}"
            )

            print(
                f"    Job Type: {job.job_type}"
            )

            print(
                f"    Previous Status: {job.status}"
            )

            print(
                f"    Deadline: {job.deadline}"
            )

            print(
                f"    Total Vacancies: {job.vacancies}"
            )


            # ====================================================
            # COUNT ACCEPTED APPLICATIONS
            # ====================================================

            accepted_applications = Application.query.filter_by(
                job_id=job.job_id,
                status="Accepted"
            ).count()


            # ====================================================
            # CALCULATE REMAINING VACANCIES
            # ====================================================

            total_vacancies = job.vacancies or 0

            remaining_vacancies = max(
                total_vacancies - accepted_applications,
                0
            )

            print(
                f"    Accepted Applications: "
                f"{accepted_applications}"
            )

            print(
                f"    Remaining Vacancies: "
                f"{remaining_vacancies}"
            )


            # ====================================================
            # VACANCIES REMAIN
            #
            # Job Fair → Referral
            # Closed/Open → Open
            # Deadline → +7 days
            # ====================================================

            if remaining_vacancies > 0:

                old_type = job.job_type
                old_status = job.status
                old_deadline = job.deadline

                job.job_type = "Referral"

                job.status = "Open"

                job.deadline = (
                    today + timedelta(days=7)
                )

                converted_job_fair_count += 1


                print(
                    "[Scheduler] Job Fair job still has "
                    "available vacancies."
                )

                print(
                    f"    Job ID: {job.job_id}"
                )

                print(
                    f"    Accepted Applications: "
                    f"{accepted_applications}"
                )

                print(
                    f"    Remaining Vacancies: "
                    f"{remaining_vacancies}"
                )

                print(
                    f"    Job Type: "
                    f"{old_type} → {job.job_type}"
                )

                print(
                    f"    Status: "
                    f"{old_status} → {job.status}"
                )

                print(
                    f"    Deadline: "
                    f"{old_deadline} → {job.deadline}"
                )


            # ====================================================
            # NO VACANCIES REMAIN
            #
            # Keep Job Fair job Closed
            # ====================================================

            else:

                job.status = "Closed"

                closed_job_fair_count += 1

                print(
                    "[Scheduler] Job Fair job has no "
                    "remaining vacancies."
                )

                print(
                    f"    Job ID: {job.job_id}"
                )

                print(
                    f"    Accepted Applications: "
                    f"{accepted_applications}"
                )

                print(
                    f"    Remaining Vacancies: "
                    f"{remaining_vacancies}"
                )

                print(
                    f"    Status: {job.status}"
                )


            print(
                f"    Final Job Type: {job.job_type}"
            )

            print(
                f"    Final Status: {job.status}"
            )

            print(
                f"    Final Deadline: {job.deadline}"
            )


        # ========================================================
        # EXPIRED REFERRAL JOBS
        #
        # Only OPEN Referral jobs need to be closed.
        #
        # Closed Referral jobs are ignored.
        # ========================================================

        expired_referral_jobs = Job.query.filter(
            Job.job_type == "Referral",
            Job.status == "Open",
            Job.deadline.isnot(None),
            Job.deadline < today
        ).all()

        print(
            f"[Scheduler] Expired Referral jobs found: "
            f"{len(expired_referral_jobs)}"
        )


        # ========================================================
        # CLOSE EXPIRED REFERRAL JOBS
        # ========================================================

        closed_referral_count = 0

        for job in expired_referral_jobs:

            print(
                "[Scheduler] ----------------------------------------"
            )

            print(
                "[Scheduler] Processing expired Referral job"
            )

            print(
                f"    Job ID: {job.job_id}"
            )

            print(
                f"    Job Title: {job.job_title}"
            )

            print(
                f"    Job Type: {job.job_type}"
            )

            print(
                f"    Deadline: {job.deadline}"
            )

            print(
                f"    Previous Status: {job.status}"
            )


            job.status = "Closed"

            closed_referral_count += 1


            print(
                f"    New Status: {job.status}"
            )


        # Restore assigned kiosks to standby after their Job Fair ends.
        # This runs in the existing scheduler transaction and needs no schema change.
        from website.models import Kiosk
        from website.utils.job_fair_lifecycle import is_past_scheduled_end

        assigned_kiosks = Kiosk.query.filter(
            Kiosk.mode.in_(("job_fair", "queue_display")),
            Kiosk.job_fair_id.isnot(None),
        ).all()
        restored_kiosks = 0
        for kiosk in assigned_kiosks:
            fair = db.session.get(JobFair, kiosk.job_fair_id)
            if fair is None or fair.status == "Finished" or is_past_scheduled_end(fair):
                kiosk.mode = "standby"
                kiosk.job_fair_id = None
                restored_kiosks += 1
        if restored_kiosks:
            print(f"[Scheduler] Restored {restored_kiosks} kiosk(s) to standby")

        # ========================================================
        # COMMIT ALL CHANGES
        # ========================================================

        db.session.commit()


        # ========================================================
        # COMPLETION LOG
        # ========================================================

        print(
            "[Scheduler] ========================================"
        )

        print(
            f"[Scheduler] Status update completed at {now}"
        )

        print(
            f"[Scheduler] Expired Job Fair jobs processed: "
            f"{len(expired_job_fair_jobs)}"
        )

        print(
            f"[Scheduler] Job Fair jobs converted to Referral: "
            f"{converted_job_fair_count}"
        )

        print(
            f"[Scheduler] Job Fair jobs remaining Closed: "
            f"{closed_job_fair_count}"
        )

        print(
            f"[Scheduler] Referral jobs closed: "
            f"{closed_referral_count}"
        )

        print(
            "[Scheduler] ========================================"
        )


    # ============================================================
    # ERROR HANDLING
    # ============================================================

    except Exception as e:

        db.session.rollback()

        print(
            "[Scheduler Error] "
            f"Failed to update job/job fair statuses: {e}"
        )