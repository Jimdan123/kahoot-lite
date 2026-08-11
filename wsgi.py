"""
Production WSGI entrypoint. Loaded by gunicorn as `wsgi:app`.

CRITICAL: gevent's monkey.patch_all() MUST run before any other import,
otherwise stdlib modules like ssl (and everything that imports it —
urllib3, anyio, httpx, langchain's HTTP client) end up holding references
to the un-patched blocking versions. Symptom is the MonkeyPatchWarning
you'll see in gunicorn logs, and long HTTPS calls (Groq API!) that
freeze the WebSocket event loop.

Local dev (`python run.py`) needs the identical patch for the identical
reason — Flask-SocketIO auto-picks gevent as its async_mode whenever it's
importable, and gevent is a hard dependency here (see requirements.txt),
not an optional production-only extra.
"""
from gevent import monkey
monkey.patch_all()

import os                            # noqa: E402
from dotenv import load_dotenv       # noqa: E402

load_dotenv()

from app import create_app           # noqa: E402

app = create_app(os.environ.get('FLASK_CONFIG', 'default'))
