# Tests

Standalone scripts that drive the running app end-to-end (HTTP + WebSocket).
Each script exits non-zero on failure so it can be wired into CI or a pre-push hook later.

## Running

1. Start the server in one shell (Postgres only — no SQLite fallback; `DATABASE_URL` must already point at your local Postgres, see the top-level README's Setup section):

   ```bash
   psql "$DATABASE_URL" -c "DELETE FROM users WHERE username IN ('alicehost','sechost','carolhost','davehost','crudhost','crudother');"  # clean signup namespace
   SECRET_KEY=dev-secret PORT=5001 python run.py
   ```

2. In another shell (still inside the venv):

   ```bash
   BASE=http://localhost:5001 python tests/smoke_test.py
   BASE=http://localhost:5001 python tests/test_impersonation.py
   BASE=http://localhost:5001 python tests/test_reconnect.py
   BASE=http://localhost:5001 python tests/test_quiz_crud.py
   ```

Each test file is self-contained. `smoke_test.py` covers the happy path plus
the Vuln-1 regression check (unauthenticated `host_join` must receive `error`).
`test_impersonation.py` covers the Vuln-3 regression (nickname impersonation
via public `player_list` must be blocked by the rejoin-token check).
`test_reconnect.py` covers the follow-up fix (a mid-game disconnect + rejoin
with the real token restores score/answer state and catches the socket up).
`test_quiz_crud.py` covers question-set/question editing: an edit form must
pre-fill the existing text/options/correct-answer, and a non-owner must get
403 (not 404) on `edit_set`/`edit_question`/`delete_question`.

## Rate limiting and the functional suites

Auth routes are rate-limited (login 10/min, signup 3/min & 10/hour). The three
suites above sign up repeatedly, so run the SERVER with the throttle off for
them:

```bash
RATELIMIT_ENABLED=0 SECRET_KEY=dev-secret PORT=5001 python run.py
```

## Security suite

`test_security.py` verifies the hardening itself and must run against a server
with rate limiting ON (the default — do **not** set `RATELIMIT_ENABLED=0`):

```bash
psql "$DATABASE_URL" -c "DELETE FROM users WHERE username = 'sechost';"  # fresh signup for the host fixture
SECRET_KEY=dev-secret PORT=5001 python run.py         # limits ON by default
BASE=http://localhost:5001 python tests/test_security.py
```

It checks four properties: the login throttle returns 429 past the cap; a
crafted `?next=` cannot redirect off-origin; SQL-injection payloads in the
login form never authenticate; and an answer submitted after `time_limit` +
grace earns no points. The login throttle is stateful per-IP, so start from a
fresh server (or wait ~60s between runs) to avoid a pre-exhausted window.

## Why standalone scripts instead of pytest

Historical — these grew from ad-hoc smoke checks during development. Converting
to pytest fixtures (server lifecycle, per-test DB reset) is a straightforward
future improvement but not required for the current scope.
