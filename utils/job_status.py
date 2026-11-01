from datetime import date

from website.extensions import db
from website.models import Job


def update_expired_jobs():
    """
    Automatically closes jobs whose deadline has passed.
    """

    today = date.today()

    expired_jobs = Job.query.filter(
        Job.status == "Open",
        Job.deadline.isnot(None),
        Job.deadline < today
    ).all()

    if not expired_jobs:
        return 0

    for job in expired_jobs:
        job.status = "Closed"

    db.session.commit()

    return len(expired_jobs)