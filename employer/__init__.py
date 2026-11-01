from flask import Blueprint

employer = Blueprint(
    "employer",
    __name__,
    url_prefix="/employer"
)

from . import routes
from . import job_fair_routes
from . import joblist_routes
from . import queue_management_route