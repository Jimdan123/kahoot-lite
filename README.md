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

**Caveats on the free tier:**
- App **sleeps after 15 min of inactivity** (~30s cold start on next request)
- SQLite lives on the container filesystem, which is **ephemeral** — the DB resets on every redeploy. Fine for a demo; for anything permanent, upgrade to Render's free Postgres and set `DATABASE_URL` accordingly.
- One worker only. For a class demo this is plenty; scaling requires Redis for shared room state.

## References
See `RESOURCES.md` for the full curated learning path.
