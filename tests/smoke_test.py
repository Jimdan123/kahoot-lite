"""End-to-end smoke test for the Kahoot-lite scaffold.

Exercises HTTP signup/room-creation + WebSocket lobby/question/answer/reveal/next.
Host socket now passes the session cookie so the server-side auth check
(current_user must own the room) passes.
"""
import os
import re
import sys
import time
import requests
import socketio

BASE = os.environ.get("BASE", "http://localhost:5001")


def get_csrf(session, url):
    r = session.get(url)
    r.raise_for_status()
    match = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text)
    if not match:
        raise RuntimeError(f"No CSRF token on {url}\n{r.text[:400]}")
    return match.group(1)


def signup(session, username, password):
    token = get_csrf(session, f"{BASE}/auth/signup")
    r = session.post(
        f"{BASE}/auth/signup",
        data={
            "csrf_token": token,
            "username": username,
            "password": password,
            "submit": "Sign Up",
        },
        allow_redirects=False,
    )
    print(f"  signup -> HTTP {r.status_code}")
    if r.status_code not in (200, 302):
        raise RuntimeError(f"signup failed: {r.text[:400]}")


def create_set(session, name, description):
    token = get_csrf(session, f"{BASE}/quiz/new")
    r = session.post(
        f"{BASE}/quiz/new",
        data={"csrf_token": token, "name": name, "description": description, "submit": "Save"},
        allow_redirects=False,
    )
    print(f"  create_set -> HTTP {r.status_code} Location={r.headers.get('Location')}")
    m = re.search(r"/quiz/(\d+)", r.headers.get("Location", ""))
    return int(m.group(1))


def add_question(session, set_id, text, options, correct, time_limit=15):
    token = get_csrf(session, f"{BASE}/quiz/{set_id}/questions/new")
    r = session.post(
        f"{BASE}/quiz/{set_id}/questions/new",
        data={
            "csrf_token": token,
            "text": text,
            "option_a": options[0],
            "option_b": options[1],
            "option_c": options[2] if len(options) > 2 else "",
            "option_d": options[3] if len(options) > 3 else "",
            "correct_option": correct,
            "time_limit": str(time_limit),
            "submit": "Save Question",
        },
        allow_redirects=False,
    )
    print(f"  add_question '{text[:30]}' -> HTTP {r.status_code}")


def create_room(session, set_id):
    token = get_csrf(session, f"{BASE}/game/")
    r = session.post(
        f"{BASE}/game/create/{set_id}",
        data={"csrf_token": token},
        allow_redirects=False,
    )
    print(f"  create_room -> HTTP {r.status_code} Location={r.headers.get('Location')}")
    m = re.search(r"/game/host/(\d+)", r.headers.get("Location", ""))
    return m.group(1)


def check_host_view(session, pin):
    r = session.get(f"{BASE}/game/host/{pin}")
    print(f"  GET host_view -> HTTP {r.status_code}")
    assert pin in r.text, "PIN not in host page"
    assert "data:image/png;base64" in r.text, "QR code not in host page"
    print("  ✓ host page has PIN and QR")


def check_join_page():
    r = requests.get(f"{BASE}/game/join?pin=00000")
    print(f"  GET join -> HTTP {r.status_code}")
    assert "Join a Game" in r.text


def cookie_header(session):
    """Serialize the requests session cookies into a raw Cookie header value."""
    return "; ".join(f"{c.name}={c.value}" for c in session.cookies)


def websocket_smoke(pin, host_session):
    print("\n--- WebSocket smoke test ---")

    # Host connects, authenticated via the same session cookie set at login
    host = socketio.Client()
    host_events = []
    host.on("*", lambda event, *args: host_events.append((event, args)))
    host.connect(
        BASE,
        transports=["websocket", "polling"],
        headers={"Cookie": cookie_header(host_session)},
    )
    host.emit("host_join", {"pin": pin})
    time.sleep(0.3)

    # Player connects anonymously
    player = socketio.Client()
    player_events = []
    player.on("*", lambda event, *args: player_events.append((event, args)))
    player.connect(BASE, transports=["websocket", "polling"])
    player.emit("player_join", {"pin": pin, "nickname": "TestPlayer"})
    time.sleep(0.3)

    host.emit("start_game", {"pin": pin})
    time.sleep(0.3)

    player.emit("submit_answer", {"pin": pin, "choice": "B"})
    time.sleep(0.3)

    host.emit("next_question", {"pin": pin})
    time.sleep(0.3)

    host.disconnect()
    player.disconnect()

    print(f"  host events:   {[e[0] for e in host_events]}")
    print(f"  player events: {[e[0] for e in player_events]}")

    host_names = [e[0] for e in host_events]
    player_names = [e[0] for e in player_events]
    assert "player_list" in host_names, "host never got player_list"
    assert "question_start" in player_names, "player never got question_start"
    assert "answer_ack" in player_names, "player never got answer_ack"
    # Any auth error would have surfaced here
    assert "error" not in host_names, f"host received error: {host_events}"
    print("  ✓ WebSocket flow works end-to-end")


def websocket_auth_bypass_check(pin):
    """
    Regression test for Vuln 1: an unauthenticated socket must be refused
    when it tries to claim the host role.
    """
    print("\n--- WebSocket auth-bypass check (Vuln 1 regression) ---")
    attacker = socketio.Client()
    events = []
    attacker.on("*", lambda event, *args: events.append((event, args)))
    attacker.connect(BASE, transports=["websocket", "polling"])
    attacker.emit("host_join", {"pin": pin})
    time.sleep(0.3)
    attacker.disconnect()
    names = [e[0] for e in events]
    print(f"  attacker events: {names}")
    assert "error" in names, "Expected 'error' event rejecting unauthorized host_join"
    print("  ✓ Unauthorized host_join is rejected")


def main():
    print("=== HTTP flow ===")
    s = requests.Session()

    print("\n1) Signup + login as host")
    signup(s, "alicehost", "password123")

    print("\n2) Create a question set")
    set_id = create_set(s, "Vietnamese History", "Smoke test set")

    print("\n3) Add three questions")
    add_question(s, set_id, "Capital of Vietnam?", ["Hanoi", "Saigon", "Hue", "Danang"], "A")
    add_question(s, set_id, "Year Vietnam reunified?", ["1954", "1968", "1975", "1986"], "C")
    add_question(s, set_id, "Ho Chi Minh's real name?", ["Nguyen Sinh Cung", "Le Duan", "Vo Nguyen Giap", "Pham Van Dong"], "A")

    print("\n4) Create a game room")
    pin = create_room(s, set_id)
    print(f"  room PIN: {pin}")

    print("\n5) Verify host view has PIN + QR")
    check_host_view(s, pin)

    print("\n6) Verify /game/join loads anonymously")
    check_join_page()

    websocket_smoke(pin, s)
    websocket_auth_bypass_check(pin)

    print("\n=== ALL CHECKS PASSED ===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
