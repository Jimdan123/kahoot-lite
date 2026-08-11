"""Minimal Flask app-context fixture for evals/'s pytest suite. evals/
reads real data via app.models/SQLAlchemy, so tests need an app context
the same way app/ai/routes.py's background worker does. No dedicated
test-DB/app-context helper exists yet — tests/ today is standalone
HTTP/WS scripts, not pytest — so this is new but intentionally small and
scoped to evals/ only.
"""
from __future__ import annotations

import pytest
from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402


@pytest.fixture(scope='session')
def app():
    flask_app = create_app()
    with flask_app.app_context():
        yield flask_app
