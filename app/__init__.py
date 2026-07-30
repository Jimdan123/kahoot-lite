import os
from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix
from config import config
from app.extensions import db, socketio, login_manager, csrf, limiter
from app.security_headers import register_security_headers


def create_app(config_name='default'):
    app = Flask(__name__)
    app.config.from_object(config[config_name])

    # Fail fast in production if the operator forgot to set SECRET_KEY —
    # a predictable dev key in prod would let anyone forge session cookies.
    if config_name == 'production' and not os.environ.get('SECRET_KEY'):
        raise RuntimeError('SECRET_KEY environment variable is required in production')

    # On Render (and most PaaS) the app sits behind one reverse proxy that sets
    # X-Forwarded-For. Trust exactly that one hop so the rate limiter sees each
    # client's real IP instead of throttling everyone as the shared proxy IP.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.config['UPLOAD_FOLDER'].mkdir(parents=True, exist_ok=True)

    # Rate limiting is on by default; set RATELIMIT_ENABLED=0 to turn it off.
    # The functional test suites need it off (they sign up repeatedly); the
    # security suite leaves it on to assert the throttle actually fires.
    app.config['RATELIMIT_ENABLED'] = os.environ.get('RATELIMIT_ENABLED', '1') != '0'

    db.init_app(app)
    limiter.init_app(app)
    # async_mode auto-detected: threading locally, gevent under the
    # GeventWebSocketWorker in production. See README's deployment section.
    # CORS defaults to '*' for local dev; production should restrict it via
    # CORS_ALLOWED_ORIGINS (e.g. https://your-app.onrender.com).
    cors_origins = os.environ.get('CORS_ALLOWED_ORIGINS', '*')
    socketio.init_app(app, cors_allowed_origins=cors_origins)
    login_manager.init_app(app)
    csrf.init_app(app)
    login_manager.login_view = 'auth.login'

    from app.main import main_bp
    from app.auth import auth_bp
    from app.quiz import quiz_bp
    from app.game import game_bp
    from app.ai import ai_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(quiz_bp, url_prefix='/quiz')
    app.register_blueprint(game_bp, url_prefix='/game')
    app.register_blueprint(ai_bp, url_prefix='/ai')

    register_security_headers(app, is_production=(config_name == 'production'))

    # In-memory game rooms never free themselves otherwise: a finished or
    # abandoned room would sit in the registry until the process restarts,
    # leaking memory. This background greenlet sweeps them periodically.
    from app.game import game_service
    socketio.start_background_task(game_service.reaper_loop, socketio)

    with app.app_context():
        from app import models  # noqa: F401 — register models with SQLAlchemy
        db.create_all()

        import sys
        print(f'[boot] DATABASE={db.engine.dialect.name}', file=sys.stderr, flush=True)

    return app
