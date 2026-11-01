from flask import Blueprint

kiosk = Blueprint("kiosk", __name__)

# Import route modules after creating the Blueprint.
from . import routes
from . import qr
from . import kiosk_queue_routes
