from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()
socketio = SocketIO()
login_manager = LoginManager()
csrf = CSRFProtect()

# Throttles abusive request rates per client IP. Storage is in-process
# (fine for our single gunicorn worker); a multi-worker deploy would point
# storage_uri at Redis so the counters are shared. key_func uses the client
# IP, which is only trustworthy because create_app wraps the app in ProxyFix.
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=['200 per hour'],
)
