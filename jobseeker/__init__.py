from flask import Blueprint

jobseeker = Blueprint(
    "jobseeker", 
    __name__, 
    url_prefix="/jobseeker"
)

from website.jobseeker import routes
from website.jobseeker import joblist_routes
