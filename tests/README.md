# Tests

Standalone scripts that drive the running app end-to-end (HTTP + WebSocket).
Each script exits non-zero on failure so it can be wired into CI or a pre-push hook later.

## Running

1. Start the server in one shell:

   ```bash
   rm -f instance/kahoot.db      # ensure a clean signup namespace
   SECRET_KEY=dev-secret PORT=5001 python run.py
   ```

2. In another shell (still inside the venv):

   ```bash
   BASE=http://localhost:5001 python tests/smoke_test.py
   BASE=http://localhost:5001 python tests/test_impersonation.py
   BASE=http://localhost:5001 python tests/test_reconnect.py
   ```

Each test file is self-contained. `smoke_test.py` covers the happy path plus
the Vuln-1 regression check (unauthenticated `host_join` must receive `error`).
`test_impersonation.py` covers the Vuln-3 regression (nickname impersonation
via public `player_list` must be blocked by the rejoin-token check).
`test_reconnect.py` covers the follow-up fix (a mid-game disconnect + rejoin
with the real token restores score/answer state and catches the socket up).

## Why standalone scripts instead of pytest

Historical — these grew from ad-hoc smoke checks during development. Converting
to pytest fixtures (server lifecycle, per-test DB reset) is a straightforward
future improvement but not required for the current scope.
