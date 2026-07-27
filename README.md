# Kahoot-lite

Real-time multiplayer quiz app for Assignment 4.

## Stack
- **Backend:** Flask + Flask-SocketIO (WebSockets for the live game)
- **Auth:** Flask-Login (host-only accounts; players are anonymous with a nickname)
- **Database:** SQLite via SQLAlchemy
- **Frontend:** Jinja2 templates + Bootstrap 5 (CDN) + vanilla JS
- **AI (Part 1.2, not yet implemented):** LangGraph + Anthropic Claude for PDF → question sets

## Setup

```bash
# 1. Activate the virtual environment
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# then edit .env — at minimum set SECRET_KEY to a random string

# 4. Run
python run.py
```

Open http://localhost:5001

(Port 5000 is used by macOS AirPlay Receiver on newer macOS versions — the app defaults to 5001 to avoid the collision. Override with `PORT=xxxx python run.py` if needed.)

The SQLite database (`instance/kahoot.db`) is created automatically on first run.

## Feature status (Part 1.1)

- [x] Host signup / login / logout
- [x] Create and manage question sets (CRUD)
- [x] Create a game room from a question set
- [x] QR code + PIN for players to join
- [x] Players join with nickname (no account required)
- [x] Real-time synchronous game loop (WebSockets)
- [x] Kahoot-style scoring (speed bonus)
- [x] Live leaderboard

## Part 1.2 (TODO)

- [ ] PDF upload endpoint
- [ ] LangGraph pipeline: extract → chunk → generate → quality-check → dedupe → save
- [ ] Host reviews and edits AI-generated questions before use

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
  ai/                 Part 1.2 stub — PDF → LangGraph pipeline
  templates/          Jinja2 templates, one folder per blueprint
  static/             CSS + JS
config.py             Development / Production config classes
run.py                entry point
instance/             runtime files (SQLite DB, uploads) — gitignored
```

## Deployment (later)

Deploy to [Render](https://render.com/) — free tier supports WebSockets.
Set `FLASK_CONFIG=production` and provide `SECRET_KEY` + `DATABASE_URL` as env vars.

## References
See `RESOURCES.md` for the full curated learning path.
