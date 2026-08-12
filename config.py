import os
from pathlib import Path

basedir = Path(__file__).resolve().parent


def _normalize_db_url(url):
    """
    Render and Heroku hand out URLs starting with 'postgres://' but modern
    SQLAlchemy (2.x) only accepts 'postgresql://'. Translate on the way in
    so DATABASE_URL works verbatim from either provider.
    """
    if url and url.startswith('postgres://'):
        return url.replace('postgres://', 'postgresql://', 1)
    return url


def _require_database_url():
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError(
            'DATABASE_URL environment variable is required (Postgres only — '
            'no SQLite fallback). See .env.example / README for local setup.'
        )
    return _normalize_db_url(url)


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-only-change-me')
    # Separate secret from SECRET_KEY on purpose — rotating a session-signing
    # key and rotating a data-encryption key are different concerns with
    # different blast radii (see app/crypto_utils.py). Used to encrypt
    # user-saved BYOK provider API keys (app/models.py's UserApiKey) at rest.
    API_KEY_ENCRYPTION_KEY = os.environ.get('API_KEY_ENCRYPTION_KEY', 'dev-only-change-me-too')
    SQLALCHEMY_DATABASE_URI = _require_database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = True
    UPLOAD_FOLDER = basedir / 'instance' / 'uploads'
    MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10 MB


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig,
}
