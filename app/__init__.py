from flask import Flask
from config import config
from app.extensions import db, socketio, login_manager, csrf


def create_app(config_name='default'):
    app = Flask(__name__)
    app.config.from_object(config[config_name])

    app.config['UPLOAD_FOLDER'].mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    # async_mode is auto-detected: 'threading' locally, 'eventlet' under gunicorn's
    # eventlet worker in production. See README's deployment section.
    socketio.init_app(app, cors_allowed_origins='*')
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

    with app.app_context():
        from app import models  # noqa: F401 — register models with SQLAlchemy
        db.create_all()

    return app
