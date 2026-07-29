import os
import secrets
from flask import Flask, g
from werkzeug.middleware.proxy_fix import ProxyFix
from config import config
from app.extensions import db, socketio, login_manager, csrf, limiter


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

    # Security response headers. Covers most of what Mozilla Observatory
    # grades: framing, MIME sniffing, referrer leakage, permission grants,
    # CSP for XSS containment, and (in prod) HTTPS enforcement.
    is_production = config_name == 'production'

    @app.before_request
    def _mint_csp_nonce():
        # Fresh per-request nonce; each inline <script nonce="..."> in the
        # templates picks this up via the csp_nonce() context helper.
        g.csp_nonce = secrets.token_urlsafe(16)

    @app.context_processor
    def _inject_csp_nonce():
        return {'csp_nonce': lambda: getattr(g, 'csp_nonce', '')}

    @app.after_request
    def _security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault(
            'Permissions-Policy',
            'geolocation=(), microphone=(), camera=(), payment=()',
        )
        # CSP:
        #   script-src whitelists the two CDNs we load (Bootstrap, Socket.IO)
        #     and requires a per-request nonce on any inline <script>.
        #   style-src still allows 'unsafe-inline' because Bootstrap and
        #     inline style="display:none" toggles need it — the real XSS
        #     surface is scripts, not styles.
        #   connect-src allows same-origin WebSockets for Flask-SocketIO.
        #   frame-ancestors 'none' subsumes X-Frame-Options for modern browsers.
        nonce = getattr(g, 'csp_nonce', '')
        csp = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
            "style-src 'self' https://cdn.jsdelivr.net 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self' https://cdn.jsdelivr.net; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'none'; "
            "form-action 'self'; "
            "base-uri 'self'; "
            "object-src 'none'"
        )
        response.headers.setdefault('Content-Security-Policy', csp)
        if is_production:
            # 1 year; keeps browsers on HTTPS after the first visit.
            response.headers.setdefault(
                'Strict-Transport-Security',
                'max-age=31536000; includeSubDomains',
            )
        return response

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
