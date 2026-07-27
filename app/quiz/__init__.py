from flask import Blueprint

quiz_bp = Blueprint('quiz', __name__)

from app.quiz import routes  # noqa: E402, F401
