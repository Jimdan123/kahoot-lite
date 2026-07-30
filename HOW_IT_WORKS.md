# How Kahoot-lite Works

A guided tour of the whole app: what happens when a player scans the QR
code, how the game loop stays in sync across everyone's screens, and where
each piece of code lives. This is the "why does the code look like that"
companion to the `README.md` (which is "how to run it").

> Written 2026-07 for a working knowledge of Flask + Flask-SocketIO. If a term
> is new to you, `RESOURCES.md` has curated links.

---

## Table of contents

1. [The big picture](#1-the-big-picture)
2. [Two kinds of requests: HTTP and WebSocket](#2-two-kinds-of-requests-http-and-websocket)
3. [Authentication: hosts vs players](#3-authentication-hosts-vs-players)
4. [Data model: what's in the database, what's in RAM](#4-data-model-whats-in-the-database-whats-in-ram)
5. [The game loop as a state machine](#5-the-game-loop-as-a-state-machine)
6. [Scoring](#6-scoring)
7. [Reconnect + duplicate prevention](#7-reconnect--duplicate-prevention)
8. [Security, layer by layer](#8-security-layer-by-layer)
9. [Rate limiting](#9-rate-limiting)
10. [Code organization](#10-code-organization)
11. [How it runs in production](#11-how-it-runs-in-production)
12. [Where AI fits in (Part 1.2)](#12-where-ai-fits-in-part-12)
13. [Testing](#13-testing)

---

## 1. The big picture

```
                    ┌───────────────┐
                    │ host's laptop │  Browser, logged in as owner.
                    └───────┬───────┘
                            │ HTTP + one WebSocket
                            ▼
   ┌──────────────────────────────────────────────────┐
   │  Render's edge (TLS termination, HTTPS, HSTS)    │
   └──────────────────────┬───────────────────────────┘
                          ▼
   ┌──────────────────────────────────────────────────┐
   │  gunicorn (one process)                          │
   │    └─ GeventWebSocketWorker (one worker)         │
   │        └─ Flask app                              │
   │            ├─ HTTP routes  (auth, quiz, game …)  │
   │            ├─ Socket.IO events  (game loop)      │
   │            ├─ in-memory _rooms dict  (live state)│
   │            └─ Postgres (persistent state)        │
   └──────────────────────────────────────────────────┘
                          ▲
                          │ HTTP + one WebSocket each
        ┌─────────────────┼──────────────────┐
        │                 │                  │
┌───────┴──────┐   ┌──────┴───────┐  ┌───────┴───────┐
│ player phone │   │ player phone │  │ player laptop │  … up to N players
└──────────────┘   └──────────────┘  └───────────────┘
```

Two things share the same server:

- **Long-lived state** (accounts, question sets) → **Postgres**, required
  everywhere (local dev included — no SQLite fallback). Managed by
  SQLAlchemy; `DATABASE_URL` must be set or the app refuses to start.
- **Ephemeral live-game state** (which players are in a room right now, what
  question is on screen, the countdown) → a plain Python `dict` in the
  running process. Fine for one worker; would need Redis to scale out.

---

## 2. Two kinds of requests: HTTP and WebSocket

Every browser–server interaction in the app falls into one of two shapes.

### HTTP (request/response)

The classic web pattern. Browser asks, server answers, connection closes.

```
GET /quiz/                           ─────►
                                      routes.py picks the handler
                                     ◄─────  HTML page (Jinja template)
POST /auth/login  {email, password}  ─────►
                                      set session cookie
                                     ◄─────  302 redirect to /
```

Used for: page loads, form submissions, signup/login, question set CRUD,
creating a room, generating the QR code image.

### WebSocket (persistent, bidirectional)

Opened once, kept alive for the entire game session. Either side can push
data at any moment without waiting for a request.

```
[first HTTP request with Upgrade: websocket]  ────►
                                            ◄──── 101 Switching Protocols

           ...connection stays open indefinitely...

client emit 'submit_answer' {choice:'B'}  ─────►
                                          ◄───── emit 'answer_ack' {correct:true, earned:850}
                                          ◄───── (later) emit 'question_start' (broadcast)
                                          ◄───── (later) emit 'question_reveal' (broadcast)
```

Used for: everything in the live game loop — lobby, questions, timers, answer
submissions, reveals, leaderboards.

**Why we need both.** HTTP is stateless — the server can't push. If all 30
players had to keep asking "is there a new question yet?" every second, we'd
have 30 useless requests per second and updates would lag by up to a second.
WebSockets let the host click "Next", and the server pushes the new question
to all 30 players in one round trip.

---

## 3. Authentication: hosts vs players

Two different identity models, on purpose. Kahoot works the same way.

### Host — logged-in account

A **host** is a real user with an email and a password. They own question
sets, create rooms, and drive the game. They authenticate via:

1. Fill in login form → `POST /auth/login`
2. Flask-Login verifies the password (bcrypt via `werkzeug.security`)
3. Server creates a session, sends a cookie back
4. Every subsequent request carries the cookie → `current_user` resolves to
   the host inside route handlers **and** inside Socket.IO handlers

Password never leaves the browser as plain text once stored — `User.set_password`
runs it through `generate_password_hash` (bcrypt). Even a full DB dump doesn't
reveal passwords.

### Player — anonymous nickname

A **player** never signs up. They join with:

1. PIN (6 digits, printed on the host's screen)
2. Nickname (any string ≤ 20 chars)

Their identity is enforced entirely by a **rejoin token** — a random secret
the server mints on first join and sends back **only to that player's
socket**. If they reload the tab or drop off the network, their browser hands
the token back to reclaim the same slot with score intact. Someone else
guessing the nickname doesn't have the token, so they can't impersonate.

The token lives in the player's `localStorage`, keyed by `pin + nickname`.

---

## 4. Data model: what's in the database, what's in RAM

### Persistent (Postgres, via SQLAlchemy)

```
users                            question_sets                   questions
├─ id                            ├─ id                          ├─ id
├─ email (unique)                ├─ name                        ├─ question_set_id ──► question_sets.id
├─ password_hash (bcrypt)        ├─ description                 ├─ position
├─ display_name                  ├─ owner_id ──► users.id       ├─ text
└─ created_at                    └─ created_at                  ├─ option_a, option_b, option_c, option_d
                                                                ├─ correct_option ('A'|'B'|'C'|'D')
                                                                └─ time_limit (seconds)
```

Relations: a `User` owns many `QuestionSet`s; each `QuestionSet` has many
`Question`s. Cascade deletes are configured so removing a host or a set
tears down its dependent rows automatically.

Models: `app/models.py`.

### Ephemeral (in-process Python dict)

```
_rooms: Dict[str, Room]          keyed by 6-digit PIN

Room
├─ pin                           string, unique across active rooms
├─ question_set_id               which set is being played
├─ owner_id                      the User.id of the host — enforced on host_join
├─ questions: List[dict]         cached snapshot of the question set
├─ host_sid                      the socket id of the current host connection
├─ players: Dict[sid, Player]    active sockets in the room
├─ orphans: Dict[nick, Player]   disconnected mid-game, waiting for a rejoin
├─ current_index                 which question we're on
├─ state                         'lobby' | 'question' | 'reveal' | 'done'
├─ question_start_time           for elapsed-time scoring
├─ last_activity, finished_at    used by the background reaper

Player
├─ sid                           socket id
├─ nickname                      display name
├─ rejoin_token                  24-byte urlsafe secret, sent only to this sid
├─ score
├─ last_answer                   'A' | 'B' | 'C' | 'D' | None
└─ last_answer_correct           bool | None
```

Kept in `app/game/game_service.py`. A background greenlet (`reaper_loop`)
sweeps stale rooms every 5 minutes so an abandoned lobby doesn't leak memory
forever.

---

## 5. The game loop as a state machine

Each room walks through four states. Each transition is triggered by a
specific Socket.IO event. State lives on the server; clients react to the
events they receive.

```
                     ┌─────────┐
                     │  LOBBY  │  ← create_room()
                     └────┬────┘
                          │
                          │ host emits: start_game
                          ▼
              ┌────────────────────────┐
              │       QUESTION         │◄──────────────────┐
              │  (timer counting down) │                   │
              └──────────┬─────────────┘                   │
                         │                                 │
                         │ host: reveal_answer             │
                         │   OR                            │
                         │ all players submitted            │
                         │   OR                            │
                         │ time_limit + grace expired      │
                         ▼                                 │
                    ┌─────────┐    host: next_question ────┘
                    │ REVEAL  │
                    └────┬────┘
                         │
                         │ host: next_question (last Q)
                         ▼
                    ┌─────────┐
                    │  DONE   │
                    └─────────┘
```

Server-side transitions live in `app/game/socket_events.py`:

| From → To | Trigger | Handler |
|---|---|---|
| `lobby` → `question` | `start_game` | `_advance_to_question(0)` |
| `question` → `reveal` | `reveal_answer` or auto | `_reveal(room)` |
| `reveal` → `question` | `next_question` (not last) | `_advance_to_question(next_index)` |
| `reveal` → `done` | `next_question` (was last) | `_end_game(room)` |

Broadcast events (all sockets in the room receive):

- `question_start` — sent when a question begins
- `question_reveal` — sent when the answer is revealed + updated leaderboard
- `game_over` — sent on the final transition
- `player_list` — updated on any join/leave

Private events (only to one socket):

- `joined` — includes `rejoin_token`, only sent to the player who just joined
- `answer_ack` — includes `correct` and `earned`, only to the answerer
- `error` — rejection reasons (used by host_join, player_join)

---

## 6. Scoring

Kahoot's speed-bonus formula, in `game_service.calculate_score`:

```
if answer is wrong:      score += 0
if answer is right:
    fraction_left = 1 - (time_taken / time_limit)   # clamped to [0, 1]
    score += 500 + 500 * fraction_left              # 500..1000
```

500 is the "you got it right" floor; up to 500 more for answering fast. Total
per question: 0, or 500–1000. Displayed with the leaderboard after each
reveal.

**Server-side deadline.** The client shows a countdown, but the client can be
tampered with. So `on_submit_answer` also computes `elapsed = time.time() -
room.question_start_time` and rejects any submission where
`elapsed > time_limit + ANSWER_GRACE_SECONDS` (default 1s). The client-side
timer is UX; the server-side deadline is truth.

---

## 7. Reconnect + duplicate prevention

The trickiest area, because it has to distinguish:

- **Legitimate rejoin** — same player, page reload / tab close / laptop woke up
- **Duplicate window** — same player opens a second tab in the same browser
- **Impersonation attempt** — someone else typing your nickname on purpose

The mechanism:

1. First join → `on_player_join` runs the "else" branch, mints a fresh
   `rejoin_token` with `secrets.token_urlsafe(24)`, stores it on the
   `Player`, and emits it to that socket only in the `joined` event.
2. Client stores it in `localStorage` under `kahoot_rejoin_<PIN>_<NICKNAME>`.
3. On any subsequent `connect` (reload, network reconnect), the client
   re-emits `player_join` with the token.
4. Server checks two places for the nickname:
   - `room.players` (active) — same-tab reload; token check swaps the sid.
   - `room.orphans` (disconnected mid-game) — token check restores full state.
5. If either exists **and** the token matches (`_token_matches` uses
   `secrets.compare_digest` with a type-safe wrapper), the slot is reclaimed
   with score/answers intact. Wrong or missing token → `error: Nickname is
   already taken in this room`.
6. If neither exists and the room is in `lobby`, a brand-new player is
   admitted. Otherwise, `error: Game already in progress`.

**Orphans.** On `disconnect`, an active player is moved to `orphans` (keyed
by nickname) instead of being deleted outright, so their score/answers can
be reclaimed by a returning socket. Only if the room is still in `lobby`
does the disconnect actually remove them (nothing worth preserving).

**Catching up on rejoin.** After a rejoin succeeds mid-game, `_resume_state_for`
emits the current phase's event (`question_start` / `question_reveal` /
`game_over`) *only* to the rejoining socket, so they land on the right screen
instead of the initial waiting spinner. `question_start` carries a
`resumed_answer` field so the client can lock the buttons if they'd already
submitted.

---

## 8. Security, layer by layer

Multiple independent defenses. Each one only has to hold for its own thing.

### Session / auth

- Passwords hashed with bcrypt (`werkzeug.security.generate_password_hash`).
- Session cookies signed with `SECRET_KEY`. Production `create_app` refuses
  to boot if `SECRET_KEY` isn't set.
- Cookies flagged `HttpOnly`, `Secure`, `SameSite=Lax` in production
  (`ProductionConfig`).

### Cross-Site Request Forgery (CSRF)

Flask-WTF adds a hidden `csrf_token` to every rendered form. The server
rejects any POST whose token is missing or wrong. Applies to signup, login,
question CRUD, room creation, delete actions.

### XSS containment

- Jinja2 auto-escapes template output. `|safe` is not used on any
  user-controlled content.
- Client-side JS uses `.textContent` (not `.innerHTML`) when rendering
  nicknames and question text, so a nickname like `<script>alert(1)</script>`
  displays as literal text.
- **Content-Security-Policy** header with per-request nonces on inline
  `<script>` blocks. Any XSS payload that manages to inject a `<script>`
  won't execute because it doesn't carry the current request's nonce.

### Response headers (set by `_security_headers` after_request hook)

| Header | Effect |
|---|---|
| `Content-Security-Policy` | see CSP above; blocks unnonced inline scripts |
| `X-Content-Type-Options: nosniff` | browser can't second-guess a Content-Type |
| `X-Frame-Options: DENY` | classic clickjacking guard |
| `frame-ancestors 'none'` (via CSP) | modern clickjacking guard |
| `Referrer-Policy: strict-origin-when-cross-origin` | don't leak URLs off-origin |
| `Permissions-Policy: geolocation=(), microphone=(), camera=(), payment=()` | denies powerful APIs we don't use |
| `Strict-Transport-Security: max-age=31536000; includeSubDomains` (prod only) | pin HTTPS in browsers |

### WebSocket-side auth

Flask-Login's `current_user` works inside Socket.IO handlers because the
socket handshake carries the session cookie. Handlers that care:

- `host_join` — requires `current_user.is_authenticated` **and**
  `current_user.id == room.owner_id`. Also rejects a socket already
  registered as a player (no dual role).
- `start_game`, `reveal_answer`, `next_question` — silently do nothing
  unless `request.sid == room.host_sid`, which can only be true if the
  socket passed the `host_join` check.

### Open redirect

`_is_safe_next` in `app/auth/routes.py` refuses anything that doesn't start
with a single `/`. Blocks `//evil.com`, `////evil.com`, `https:/evil.com`,
`/\evil.com`, absolute URLs, and `javascript:` URIs.

### SQL injection

Every DB call goes through SQLAlchemy's parameterized query API. No raw
SQL, no f-string interpolation into queries.

### Dependency supply chain

`.github/dependabot.yml` opens weekly PRs to bump vulnerable Python
packages and monthly PRs for GitHub Actions. `pip-audit` was run against
the venv and came back clean.

---

## 9. Rate limiting

Flask-Limiter (`app/extensions.py`), keyed by client IP (`get_remote_address`
+ `ProxyFix(x_for=1)` so we see the real IP behind Render's proxy, not the
proxy's own IP).

| Route | Limit | Reason |
|---|---|---|
| `POST /auth/signup` | 10/hour AND 3/minute | account spam |
| `POST /auth/login` | 10/minute | password brute-force |
| everything else | 200/hour (global default) | catch-all |

Storage is in-process; a multi-worker deploy would need to point
`storage_uri` at Redis so counters are shared. WebSocket handlers aren't
throttled by Flask-Limiter — they're self-throttled by state
(one answer per question per player, host-only actions gated on `host_sid`).

Turn off for local testing with `RATELIMIT_ENABLED=0`.

---

## 10. Code organization

```
app/
├── __init__.py          Application factory. Wires extensions, blueprints,
│                        security headers, ProxyFix, rate limiter, reaper.
├── extensions.py        Singleton instances (db, socketio, login_manager, csrf, limiter).
├── models.py            SQLAlchemy models: User, QuestionSet, Question.
│
├── main/                Landing page (/).
├── auth/                Host signup / login / logout.
├── quiz/                Question set CRUD (host-facing).
├── game/                The live game itself:
│   ├── routes.py            HTTP endpoints: /game/, /game/create/<id>,
│   │                        /game/host/<pin>, /game/join, /game/play/<pin>
│   ├── socket_events.py     WebSocket handlers: host_join, player_join,
│   │                        start_game, submit_answer, reveal_answer,
│   │                        next_question, disconnect
│   └── game_service.py      In-memory Room + Player state, scoring formula,
│                            reaper loop for stale rooms.
├── ai/                  Part 1.2: PDF → question set via LangGraph.
│   ├── routes.py            HTTP endpoints: /ai/upload, /ai/processing/<id>,
│   │                        /ai/status/<id>
│   ├── jobs.py              In-memory job registry polled by /ai/status
│   └── langgraph_flow/      The pipeline itself, one file per graph stage
│       ├── graph.py             Node wiring + run_pipeline() entrypoint
│       ├── state.py             PipelineState (the dict threaded through the graph)
│       ├── config.py            Tunables + provider selection
│       ├── llm_utils.py         Groq client construction
│       ├── json_utils.py        LLM response → parsed JSON
│       ├── progress.py          Progress-callback plumbing
│       └── nodes/               extract, chunk, comprehend, generate, critic, save
├── templates/           Jinja2 templates, one folder per blueprint.
└── static/              CSS + client-side JS.

config.py                Development / Production config classes.
run.py                   Entry point (used only for `python run.py` locally).
Procfile / render.yaml   Production entrypoint on Render.
requirements.txt         Pinned Python deps.
tests/                   End-to-end regression tests.
instance/                Runtime files (uploads) — gitignored.
```

Two principles worth noting:

1. **Routes are thin, services do the work.** Route handlers parse input,
   call a service function, return a response. Business logic (scoring,
   room state, PIN generation) lives in `game_service.py`, not in route
   handlers.
2. **HTTP and WebSocket are separate modules.** `routes.py` never touches
   Socket.IO; `socket_events.py` never renders templates. Clear seam
   between the two transport types.

---

## 11. How it runs in production

### Locally

```
python run.py
    ↓
run.py loads dotenv, creates app via create_app('default')
    ↓
socketio.run(app, port=5001, debug=True)
    ↓
Flask-SocketIO auto-picks an async mode (gevent if installed)
```

Fine for development. Not fine for production because Werkzeug's dev server
isn't hardened for public traffic.

### On Render

```
Render runs:
    gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker \
             -w 1 --bind 0.0.0.0:$PORT run:app
```

- `gunicorn` is a production WSGI server
- The `GeventWebSocketWorker` upgrades HTTP requests to WebSocket cleanly
- `-w 1` (one worker) — required because our room state lives in a per-process
  dict; multi-worker would need Redis for shared state
- `run:app` imports `run.py` and exposes the `app` object (the `if __name__
  == '__main__'` block never runs under gunicorn)

Render also provides:

- TLS termination and HTTPS
- A reverse proxy in front of gunicorn
- A managed Postgres (free tier) providing `DATABASE_URL` via env var
- Automatic redeploy on every `git push` to `main`

The full trip of one player answering a question:

```
1. Player taps 'B' in the browser.
2. Client JS calls socket.emit('submit_answer', {pin, choice:'B'}).
3. Frame travels WebSocket → Render's proxy → gunicorn → gevent worker.
4. socket_events.on_submit_answer runs:
   - looks up Room in _rooms dict
   - checks player exists, hasn't already answered, elapsed is within deadline
   - computes score, mutates player.score
   - emits 'answer_ack' back to just this socket
   - if all players have now answered, calls _reveal(room)
5. Reveal emits 'question_reveal' to the whole room; every socket updates its screen.
```

No DB writes in the hot loop — question sets are loaded once when the room
is created and cached in `room.questions`.

---

## 12. Where AI fits in (Part 1.2)

The pipeline is live at `/ai/upload`. Hosts upload a PDF; the server runs an
8-stage LangGraph agent (`app/ai/langgraph_flow/`, one module per stage)
that extracts text, builds a structured understanding of it, drafts MCQs
against *that* understanding rather than raw text, checks each question
isn't answerable without the document, retries once or twice if too few
survive, and saves the survivors as a new `QuestionSet` in the same table as
manually-created ones — so the game loop doesn't know or care where the
questions came from.

The extra structure exists to fight a specific failure mode: a single
"read this paragraph, write a quiz question" prompt reliably produces recall
trivia ("What is X called?"). Forcing the model to extract facts first, find
connections *between* chunks second, and only then write a question that
needs two of those facts together, produces something closer to a real
comprehension check.

### The graph

```
  extract_text ──► chunk_by_topic ──► comprehend_chunks ──► merge_comprehension
                                                                    │
                                                                    ▼
                                                       generate_practice_questions
                                                          (separate track, see below)
                                                                    │
                                                                    ▼
                                                          generate_questions ◄──┐
                                                                    │           │
                                                                    ▼           │
                                                            closed_book_check   │
                                                                    │           │
                                                                    ▼           │
                                                              quality_check     │
                                                               │        │       │
                                                 enough pass  ▼        ▼ too few│
                                                             save   bump_retry ─┘
                                                              │      (loops back
                                                              ▼       to generate,
                                                             END      capped at 2)
```

Each node is a function `PipelineState → dict-of-updates`. A retry only
re-enters at `generate_questions` — the comprehension pass is deterministic
per document and expensive to redo, so a bad question-writing attempt gets
another shot at the same (unchanged) comprehension record rather than
re-reading the PDF. The conditional edge after `quality_check` inspects
`len(validated_questions) < MIN` and `retry_count < 2` to decide whether to
loop or save.

### Nodes, in order

1. **extract_text** (`nodes/extract.py`) — `pdfplumber.open(...)`,
   concatenate pages. Falls back to local Tesseract OCR (`pytesseract`,
   `eng+vie` language data) for scanned PDFs with no text layer; fails
   cleanly with `state.error` if neither produces text. Tesseract runs
   entirely on the server's own CPU — no external API, no rate limit, no
   cost per page. (An earlier version of this fallback asked a Groq vision
   LLM to transcribe each page image; dropped after testing against a real
   scanned textbook found it both hit Groq's free-tier rate limit almost
   immediately and occasionally hallucinated text that wasn't in the image.)
2. **chunk_by_topic** (`nodes/chunk.py`) — split by ~500 words. Drop chunks
   with fewer than 40 words (too thin to reason about).
3. **comprehend_chunks** (`nodes/comprehend.py`) — per chunk, call the LLM
   (`temperature=0.0`) to extract *claims*, *definitions*, *mechanisms*, and
   *quantities*, each anchored to a verbatim quote (`span`) from the chunk.
   No questions are written yet — this stage only builds understanding.
4. **merge_comprehension** (`nodes/comprehend.py`) — one LLM call over all
   per-chunk records together: dedupes repeated facts, flags terms used but
   never defined anywhere (`undefined_terms`), and — the important part —
   finds **cross-chunk links**: pairs of facts in *different* chunks that
   actually connect (a mechanism's triggering condition quantified
   elsewhere, a term defined in one chunk and relied on by a claim in
   another). Also collects a short `topic` phrase and up to 8
   `existing_exercises` excerpts, both consumed by the practice track next.
   Falls back to a naive concatenation with no links if the LLM call fails,
   so a bad merge doesn't kill the run.
5. **generate_practice_questions** (`nodes/practice.py`) — a track separate
   from the document-grounded questions below: writes extra EASY/MEDIUM/HARD
   practice problems on the document's `topic`, meant to be answerable from
   the model's own knowledge rather than requiring the document (the
   opposite intent of `generate_questions`' two-hop rule). How many of each
   difficulty is **chosen by the host on the upload form**
   (`practice_count`, 1-5, defaulting to `PRACTICE_QUESTIONS_PER_DIFFICULTY`
   in `config.py`) and flows through as `state['practice_questions_per_difficulty']`.
   If the document already has its own exercises (from `existing_exercises`
   above), the prompt requires genuinely new problems, not reworded copies.
   Additive and best-effort: any failure here yields an empty list rather
   than touching `state['error']`, since the main pipeline already produces
   a usable question set without it.
6. **generate_questions** (`nodes/generate.py`) — for each chunk, call the
   LLM (`temperature=0.4`) with the chunk's local comprehension items *and*
   any cross-chunk links touching it. The system prompt enforces a two-hop
   rule (a question must combine two distinct facts — preferably a
   cross-chunk link, otherwise two local items), forbids pure recall and
   passage-quoting phrasing, and requires the JSON schema to name the two
   hops (`hop_a`/`hop_b`) and what a one-hop reader would get wrong
   (`tests`) *before* the `question` field itself — field order matters
   because a model filling JSON top-to-bottom that writes `question` first
   will happily rationalize a hop after the fact instead of requiring one.
7. **closed_book_check** (`nodes/critic.py`) — a *separate*, context-free
   LLM call (`temperature=0.0`) tries to answer every draft using only
   general knowledge — it never sees the PDF or the comprehension record.
   Answer + confidence per question are recorded for the next stage.
8. **quality_check** (`nodes/critic.py`) — another LLM call grades the
   batch, rejecting on (in order): a closed-book leak — the context-free
   attempt matched the correct answer at medium/high confidence, meaning
   the question doesn't need the document at all — then structural issues
   (multiple correct options, option not in A–D, verbatim-repeated answer),
   then a decorative second hop, then ambiguity. Falls back to structural
   checks only if the critic call itself fails. Duplicates (by question
   text, case-insensitive) collapse.
9. **_should_retry** (conditional edge) — if fewer than 5 questions
   survived and we've retried less than twice, jump to `bump_retry` →
   `generate_questions` again (reusing the existing comprehension record).
   Otherwise proceed to `save`.
10. **save** (`nodes/save.py`) — write a new `QuestionSet` + `Question` rows
    (document questions and practice questions together) in one
    transaction. Returns `question_set_id` to the caller.

Every LLM call above — not just `generate_questions` — goes through
`invoke_json` (see **Model rotation & fallback** below), so a bad or
truncated response from one model retries the same request on the next
model in the chain instead of just failing that node's try/except and
falling through to its own fallback (naive merge, structural-only grading,
etc.). The per-node fallbacks still exist as the last resort if *every*
model in the whole chain fails or returns unusable JSON.

Cost/latency trade-off worth knowing: this pipeline makes roughly 2x the LLM
calls of a plain "chunk → generate → grade" version (one comprehension call
and one closed-book call per chunk, on top of generate/quality), in exchange
for meaningfully harder, less-guessable questions.

### Async execution

The pipeline takes 20-60 seconds — too long to hold an HTTP request open
on Render's free tier (which caps requests at 30s). So:

The upload form (`templates/ai/upload.html`) also lets the host pick how
many EASY/MEDIUM/HARD practice questions to generate (1-5 each, default
`PRACTICE_QUESTIONS_PER_DIFFICULTY` from `config.py`) via a `practice_count`
field — `routes.py` clamps it to
`[MIN_PRACTICE_QUESTIONS_PER_DIFFICULTY, MAX_PRACTICE_QUESTIONS_PER_DIFFICULTY]`
before it ever reaches the pipeline, so a tampered or malformed form value
can't blow up the LLM call.

```
POST /ai/upload  ─► validate PDF (magic bytes, size, extension)
                 ─► save under a UUID filename in instance/uploads/
                 ─► clamp practice_count to [MIN, MAX]
                 ─► jobs.create(owner_id=current_user.id) → job_id
                 ─► socketio.start_background_task(_worker)
                 ─► 302 to /ai/processing/<job_id>

background _worker:
    with app.app_context():
        run_pipeline(pdf_path, owner_id,
                     practice_questions_per_difficulty=practice_count,
                     progress_cb=lambda msg, pct: jobs.update(...))
        jobs.mark_done(job_id, question_set_id)
    then: delete the uploaded PDF from disk

/ai/processing/<job_id>   ─► HTML page that polls…
/ai/status/<job_id>       ─► JSON: {status, message, progress, redirect?}
                             every 1.5s. On done → JS window.location = redirect.
```

`jobs` is a simple in-memory registry (`app/ai/jobs.py`), one dict, TTL of
30 minutes after completion. A restart drops in-flight jobs — acceptable
for a class demo; a real deployment would move this to Redis / Celery.

### PDF security

Upload is behind the same layered defenses as the rest of the app, plus:

- **Magic-byte check** — reject anything whose first 5 bytes aren't `%PDF-`,
  regardless of extension or Content-Type
- **10 MB size cap** (`MAX_PDF_BYTES`) enforced both via
  `MAX_CONTENT_LENGTH` in config and a manual size check for a friendlier
  error than a 413
- **Server-generated filename** — never trust the browser's, defeats
  directory traversal
- **Rate limited** — 5 uploads per hour, 2 per minute per IP (LLM calls are
  expensive; this is more of a wallet defense than a security defense)
- **Auto-cleanup** — the saved PDF is deleted from disk after the pipeline
  finishes, whether success or failure

### LLM provider

The pipeline needs `GROQ_API_KEY` to run at all — `which_provider()` returns
`None` and `run_pipeline` raises `RuntimeError` early if it isn't set, so the
`_worker` surfaces a clean error via `jobs.mark_failed` instead of a 500.
Groq's free tier needs no credit card (get a key at
https://console.groq.com/keys).

Beyond that single required key, `make_llm()` (`llm_utils.py`) builds a
**3-tier fallback chain**, each tier optional beyond the first:

| Tier | Env var | Client | Default models |
|---|---|---|---|
| 1. Groq (required) | `GROQ_API_KEY` | `ChatGroq` | `llama-3.3-70b-versatile`, `qwen/qwen3.6-27b` |
| 2. NVIDIA NIM (optional) | `NVIDIA_API_KEY` | `ChatOpenAI` (OpenAI-compatible) | `nvidia/llama-3.3-nemotron-super-49b-v1`, `meta/llama-3.1-8b-instruct` |
| 3. OpenRouter (optional) | `OPENROUTER_API_KEY` | `ChatOpenAI` (OpenAI-compatible) | `openai/gpt-oss-20b:free`, `nvidia/nemotron-3-nano-30b-a3b:free`, `inclusionai/ling-3.0-flash:free` |

A tier is skipped entirely if its API key isn't set — only `GROQ_API_KEY` is
required. Every `*_MODEL_CHAIN` in `config.py` can be overridden via the
matching env var (comma-separated) without a code change; a single Groq
model can also be pinned with `GROQ_MODEL`.

Check https://console.groq.com/docs/models (and the NVIDIA/OpenRouter
catalogs) before changing a default — model lineups turn over, and a
hardcoded model that gets deprecated fails the same way a dated Gemini
model once did here (404, "no longer available").

Scanned-PDF OCR does not go through any of these — it runs on local
Tesseract (`pytesseract`, see the `extract_text` node above), so it has no
LLM provider, model, or rate limit of its own.

### Model rotation & fallback

`_RotatingLLM` (`llm_utils.py`) is what every node's `llm = make_llm()`
actually returns (unless it's the only tier configured, in which case it's
just a plain `ChatGroq`). It walks the 3-tier chain above and rotates past
an entry for two different reasons, handled two different ways:

1. **The provider itself is unavailable** — rate limit / daily quota
   exhausted, the model isn't enabled on this account, bad/missing API key,
   timed out, or errored on its end. `_transient_errors()` lists the exact
   exception types (`RateLimitError`, `PermissionDeniedError`,
   `AuthenticationError`, `APIConnectionError` — which covers
   `APITimeoutError`, its subclass — and `InternalServerError`, matched
   across both `groq.*` and `openai.*` since NVIDIA/OpenRouter are
   OpenAI-compatible). `invoke()` catches these, logs
   `llm rotation: <provider>:<model> unavailable (...), trying next in
   chain`, and moves on. Once it lands on a working entry, it **commits** —
   `self._idx` moves forward permanently for the rest of this
   `_RotatingLLM` instance's life, so a node processing many chunks doesn't
   re-hit an exhausted model on every single chunk. (A fresh node still
   starts a fresh `make_llm()` back at the front of the chain, since
   per-minute limits may have reset by then.)
2. **The provider responded, but the response is useless** — empty or
   truncated JSON. This one is easy to miss: a reasoning model like
   `qwen/qwen3.6-27b` writes a hidden `<think>...</think>` scratchpad before
   its real answer, sharing the same `max_tokens` budget as the answer
   itself. Under load it can spend the *entire* budget thinking and never
   reach the answer — no exception is raised, `invoke()` alone can't see
   anything wrong, but `parse_llm_json` correctly recognizes the unclosed
   `<think>` tag and returns `[]` rather than guessing at the fragment. For
   this reason **every node calls `invoke_json()` instead of
   `invoke()`**: it parses the response internally, and if parsing comes
   back empty, retries the *same request* on the next model in the chain
   rather than accepting the empty result as this chunk's final answer. As
   a targeted fix for the qwen case specifically, `_client_for` also passes
   `reasoning_effort='none'` to any Groq model with `qwen` in its name,
   which turns off the scratchpad entirely (verified live: same prompt went
   from an empty response to clean JSON in under a second). Unlike a
   transient API error, one bad parse doesn't mean the model is
   exhausted — `invoke_json()` does **not** permanently advance `self._idx`
   for that reason, so the next unrelated call still gives that model a
   fresh try.

Both paths log through the same `llm rotation: ...` prefix, so `grep
"llm rotation"` on the server log shows the full story of what a run
actually used.

---

## 13. Testing

Three end-to-end regression suites in `tests/`, each driving a running
server via real HTTP + real Socket.IO clients:

| File | What it guards |
|---|---|
| `smoke_test.py` | Happy path (signup, create set, add questions, host, join, play). Also asserts unauthenticated `host_join` is rejected (Vuln-1 regression). |
| `test_impersonation.py` | Someone else typing your nickname must not steal your slot without your rejoin token. |
| `test_reconnect.py` | Disconnecting mid-game and coming back with the token restores score + answer state, and the client is caught up with the current question. |
| `test_security.py` | Login throttle returns 429, `?next=` cannot redirect off-origin, SQL-injection payloads don't authenticate, late answers earn no points. |

Run each against a running dev server:

```bash
# For the functional suites, turn the throttle off (they sign up a lot):
RATELIMIT_ENABLED=0 SECRET_KEY=dev PORT=5001 python run.py

# Then, in another shell:
BASE=http://localhost:5001 python tests/smoke_test.py
BASE=http://localhost:5001 python tests/test_impersonation.py
BASE=http://localhost:5001 python tests/test_reconnect.py

# For the security suite, run the server with the throttle ON (default):
SECRET_KEY=dev PORT=5001 python run.py
BASE=http://localhost:5001 python tests/test_security.py
```

Details in `tests/README.md`.
