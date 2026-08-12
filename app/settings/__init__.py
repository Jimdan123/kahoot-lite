from flask import Blueprint

settings_bp = Blueprint('settings', __name__)

from app.settings import routes  # noqa: E402, F401
