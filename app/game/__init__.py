from flask import Blueprint

game_bp = Blueprint('game', __name__)

from app.game import routes, socket_events  # noqa: E402, F401
