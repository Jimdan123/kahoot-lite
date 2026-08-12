"""Security regression suite for Kahoot-lite.

Drives the running app over HTTP + WebSocket and asserts the hardening holds:

  1. Rate limiting   — POST /auth/login is throttled (429 after the cap).
  2. Open redirect   — a crafted ?next= cannot bounce a user off-origin.
  3. SQL injection   — classic payloads in the login form never authenticate.
  4. Answer deadline — a submission after time_limit + grace earns no points.

IMPORTANT: run this suite against a server with rate limiting ENABLED (the
default). Do NOT set RATELIMIT_ENABLED=0 here — test 1 needs the throttle live.
Because the login throttle is stateful per-IP, start from a fresh server (or
wait ~60s between runs) so earlier logins don't pre-exhaust the window.

    rm -f instance/kahoot.db
    SECRET_KEY=dev-secret PORT=5001 python run.py        # limits ON by default
    BASE=http://localhost:5001 python tests/test_security.py

Exits non-zero on the first failed assertion.
"""
import os
import re
import sys
import time
from urllib.parse import urlparse

import requests
import socketio

BASE = os.environ.get("BASE", "http://localhost:5001")
HOST_USERNAME = "sechost"
HOST_PW = "password123"


# --- small helpers (kept local so this file is self-contained) --------------

def get_csrf(session, url):
    r = session.get(url)
    r.raise_for_status()
    m = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text)
    if not m:
        raise RuntimeError(f"No CSRF token on {url}")
    return m.group(1)


def signup(session, username, password):
    r = session.post(
        f"{BASE}/auth/signup",
        data={
            "csrf_token": get_csrf(session, f"{BASE}/auth/signup"),
            "username": username,
            "password": password, "submit": "Sign Up",
        },
        allow_redirects=False,
    )
    # 302 = created + auto-logged-in; 200 = validation error (e.g. already exists)
    return r.status_code


def login(session, username, password, next_param=None):
    url = f"{BASE}/auth/login"
    if next_param is not None:
        url += "?next=" + next_param
    return session.post(
        url,
        data={"csrf_token": get_csrf(session, f"{BASE}/auth/login"),
              "username": username, "password": password, "submit": "Log In"},
        allow_redirects=False,
    )


def create_set(session, name):
    r = session.post(
        f"{BASE}/quiz/new",
        data={"csrf_token": get_csrf(session, f"{BASE}/quiz/new"),
              "name": name, "description": "sec", "submit": "Save"},
        allow_redirects=False,
    )
    return int(re.search(r"/quiz/(\d+)", r.headers.get("Location", "")).group(1))


def add_question(session, set_id, text, options, correct, time_limit):
    session.post(
        f"{BASE}/quiz/{set_id}/questions/new",
        data={
            "csrf_token": get_csrf(session, f"{BASE}/quiz/{set_id}/questions/new"),
            "text": text, "option_a": options[0], "option_b": options[1],
            "option_c": "", "option_d": "", "correct_option": correct,
            "time_limit": str(time_limit), "submit": "Save Question",
        },
        allow_redirects=False,
    )


def create_room(session, set_id):
    r = session.post(f"{BASE}/game/create/{set_id}",
                     data={"csrf_token": get_csrf(session, f"{BASE}/game/")},
                     allow_redirects=False)
    return re.search(r"/game/host/(\d+)", r.headers.get("Location", "")).group(1)


def cookie_header(session):
    return "; ".join(f"{c.name}={c.value}" for c in session.cookies)


# --- security checks --------------------------------------------------------

def check_open_redirect():
    """A rejected next must land on '/', never on an attacker host."""
    print("\n--- Open-redirect check ---")
    # These all parse to an empty netloc yet browsers resolve them off-origin.
    hostile = ["////evil.com", "https:/evil.com", "/\\evil.com", "//evil.com"]
    for payload in hostile:
        s = requests.Session()  # fresh, unauthenticated session per attempt
        r = login(s, HOST_USERNAME, HOST_PW, next_param=payload)
        location = r.headers.get("Location", "")
        host = urlparse(location).netloc
        print(f"  next={payload!r:16} -> {r.status_code} Location={location!r}")
        assert r.status_code == 302, f"expected redirect after login, got {r.status_code}"
        assert "evil.com" not in location and host == "", \
            f"open redirect! {payload!r} bounced to {location!r}"
    print("  ✓ all hostile next= values were neutralized to same-origin")


def check_sql_injection():
    """Classic auth-bypass payloads must not authenticate."""
    print("\n--- SQL-injection check (login form) ---")
    payloads = [
        ("' OR '1'='1", "' OR '1'='1"),
        ("admin'--", "anything"),
        ("sechost' --", "wrong"),
    ]
    for username, pw in payloads:
        s = requests.Session()
        r = login(s, username, pw)
        # A successful login 302-redirects; a failure re-renders the form (200).
        print(f"  username={username!r:22} -> HTTP {r.status_code}")
        assert r.status_code == 200, f"injection may have authenticated: {username!r}"
        # And the session must not be able to reach an authenticated page.
        guard = s.get(f"{BASE}/quiz/", allow_redirects=False)
        assert guard.status_code in (301, 302), "injected session reached /quiz/"
    print("  ✓ no injection payload authenticated")


def check_answer_deadline(host_session):
    """A submission after time_limit + grace earns no points."""
    print("\n--- Answer-deadline check (Vuln: client-timer bypass) ---")
    set_id = create_set(host_session, "Deadline set")
    add_question(host_session, set_id, "2+2?", ["3", "4", "5", "6"], "B", time_limit=5)
    pin = create_room(host_session, set_id)

    host = socketio.Client()
    host.connect(BASE, transports=["websocket", "polling"],
                 headers={"Cookie": cookie_header(host_session)})
    host.emit("host_join", {"pin": pin})

    player = socketio.Client()
    events = []
    player.on("*", lambda event, *a: events.append(event))
    player.connect(BASE, transports=["websocket", "polling"])
    player.emit("player_join", {"pin": pin, "nickname": "Latecomer"})
    time.sleep(0.3)

    host.emit("start_game", {"pin": pin})
    time.sleep(0.3)

    # Wait past the 5s limit + 1s grace before answering.
    wait = 5 + 1 + 1.5
    print(f"  waiting {wait}s (past time_limit 5 + grace 1) then submitting…")
    time.sleep(wait)
    events.clear()
    player.emit("submit_answer", {"pin": pin, "choice": "B"})  # correct, but late
    time.sleep(0.5)

    host.disconnect()
    player.disconnect()
    print(f"  events after late submit: {events}")
    assert "answer_ack" not in events, "late answer was scored — deadline not enforced"
    print("  ✓ late submission earned no answer_ack (rejected server-side)")


def check_rate_limit():
    """POST /auth/login must start returning 429 once the cap is exceeded.

    Runs LAST because it exhausts the per-IP login window for ~60s.
    """
    print("\n--- Rate-limit check (login throttle) ---")
    codes = []
    for _ in range(15):
        s = requests.Session()
        codes.append(login(s, "nobody", "wrong").status_code)
    print(f"  login POST codes: {codes}")
    if 429 not in codes:
        raise AssertionError(
            "no 429 seen — is rate limiting enabled? Do NOT run this suite with "
            "RATELIMIT_ENABLED=0, and start from a fresh server so the window "
            "isn't already exhausted."
        )
    print(f"  ✓ throttle fired (first 429 at request #{codes.index(429) + 1})")


def main():
    print("=== SECURITY SUITE ===  (server must have rate limiting ENABLED)")
    host_session = requests.Session()
    status = signup(host_session, HOST_USERNAME, HOST_PW)
    if status == 200:
        # Already exists from a prior run — log in instead to get an authed session.
        login(host_session, HOST_USERNAME, HOST_PW)
    print(f"  host ready (signup HTTP {status})")

    # Login-consuming checks run before the throttle test (which locks login).
    check_open_redirect()
    check_sql_injection()
    check_answer_deadline(host_session)
    check_rate_limit()  # must be last

    print("\n=== ALL SECURITY CHECKS PASSED ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
