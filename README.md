# Kahoot-lite

Real-time multiplayer quiz app for Assignment 4.

## Stack
- **Backend:** Flask + Flask-SocketIO (WebSockets for the live game)
- **Auth:** Flask-Login (host-only accounts; players are anonymous with a nickname)
- **Database:** Postgres via SQLAlchemy (required — no SQLite fallback)
- **Frontend:** Jinja2 templates + Bootstrap 5 (CDN) + vanilla JS
- **AI (Part 1.2):** LangGraph + Groq for PDF → question sets; local Tesseract OCR for scanned PDFs with no text layer

## Setup

```bash
# 1. Activate the virtual environment
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Local Postgres (one-time setup)
brew install postgresql@14   # if not already installed
brew services start postgresql@14
psql -U "$(whoami)" -d postgres -c "CREATE ROLE kahoot_lite LOGIN PASSWORD 'kahoot_lite_dev';"
psql -U "$(whoami)" -d postgres -c "CREATE DATABASE kahoot_lite OWNER kahoot_lite;"

# 3b. Local Tesseract (one-time setup, needed for scanned-PDF uploads in Part 1.2)
brew install tesseract tesseract-lang   # tesseract-lang adds non-English language data (e.g. Vietnamese)

# 4. Configure environment
cp .env.example .env
# then edit .env — at minimum set SECRET_KEY to a random string;
# DATABASE_URL already points at the local Postgres role/db created above

# 5. Run
python run.py
```

Open http://localhost:5001

(Port 5000 is used by macOS AirPlay Receiver on newer macOS versions — the app defaults to 5001 to avoid the collision. Override with `PORT=xxxx python run.py` if needed.)

Tables are created automatically on first run via `db.create_all()`. `DATABASE_URL` is required — the app raises at startup if it's missing (no SQLite fallback).

## Feature status (Part 1.1)

- [x] Host signup / login / logout
- [x] Create and manage question sets (CRUD)
- [x] Create a game room from a question set
- [x] QR code + PIN for players to join
- [x] Players join with nickname (no account required)
- [x] Real-time synchronous game loop (WebSockets)
- [x] Kahoot-style scoring (speed bonus)
- [x] Live leaderboard

## Feature status (Part 1.2)

- [x] PDF upload endpoint (`/ai/upload`), including scanned-PDF OCR via local Tesseract
- [x] LangGraph pipeline: extract → chunk → comprehend → merge → practice → generate → closed-book-check → quality-check → save (`app/ai/langgraph_flow/`, see `HOW_IT_WORKS.md` §12 for the full node-by-node tour)
- [x] 3-tier LLM fallback (Groq → NVIDIA → OpenRouter), with automatic retry-on-next-model for both API errors and unparseable responses
- [x] Host picks how many practice questions per difficulty to generate (upload form)
- [x] Host reviews AI-generated questions (question set detail page) and can add more or delete the whole set
- [ ] Per-question edit/delete for an existing set (currently whole-set delete only)

## Project layout

```
app/
  __init__.py         application factory
  extensions.py       db, socketio, login_manager, csrf singletons
  models.py           User, QuestionSet, Question models
  main/               landing page
  auth/               signup, login, logout (host only)
  quiz/               question set CRUD
  game/               room creation, live game (HTTP + WebSocket)
  ai/                 Part 1.2 — PDF → LangGraph pipeline (question generation)
  templates/          Jinja2 templates, one folder per blueprint
  static/             CSS + JS
config.py             Development / Production config classes
run.py                entry point
instance/             runtime files (uploads) — gitignored
```

## Deploy to Render (public URL, free tier)

Prereqs: a free GitHub account and a free Render account.

**1. Push this repo to GitHub**

```bash
gh repo create kahoot-lite --public --source=. --push
# or if you don't have gh CLI: create a repo on github.com, then:
#   git remote add origin https://github.com/<you>/kahoot-lite.git
#   git branch -M main && git push -u origin main
```

**2. Deploy on Render**

- Log in at https://render.com
- Click **New +** → **Blueprint**
- Connect your GitHub repo — Render will detect `render.yaml` and provision everything
- Wait ~2 minutes for the first build
- Your app is live at `https://kahoot-lite-<random>.onrender.com`

The QR codes in `/game/host/<pin>` will automatically use that public URL, so players anywhere in the world can scan them and join.

**Python version:** pinned to 3.12.7 via `.python-version` and `runtime.txt` for broadest compatibility with the WebSocket stack.

**WebSocket worker:** production uses `gevent-websocket` (`geventwebsocket.gunicorn.workers.GeventWebSocketWorker`). This replaced the previous `eventlet` worker, which had install issues on newer Python versions.

**Caveats on the free tier:**
- App **sleeps after 15 min of inactivity** (~30s cold start on next request)
- The Blueprint provisions a free Postgres database and wires its `DATABASE_URL` into the web service automatically. If a service was ever created *without* the Blueprint (manual Web Service) or `DATABASE_URL` is otherwise missing, the app now fails to start rather than silently falling back to ephemeral storage — provision a Postgres database in the Render dashboard and set its `DATABASE_URL` on the web service.
- Render's free Postgres expires after 90 days — you'll need to snapshot/migrate or accept data loss at that point.
- One worker only. For a class demo this is plenty; scaling requires Redis for shared room state.

## References
See `RESOURCES.md` for the full curated learning path.
