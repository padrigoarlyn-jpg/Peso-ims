from flask import Blueprint
supervised_registration = Blueprint("supervised_registration", __name__, url_prefix="/supervised-registration")
from . import routes
