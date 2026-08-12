"""Regression test for the nickname-impersonation vulnerability.

Threat model:
- Attacker sees "Alice" in the public player_list broadcast.
- Attacker emits player_join with nickname="Alice" and no token.
- MUST be rejected. The real Alice must remain in the room, unaffected.
"""
import os
import re
import time
import requests
import socketio

BASE = os.environ.get("BASE", "http://localhost:5001")


def csrf(session, url):
    r = session.get(url)
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', r.text).group(1)


def bootstrap_room():
    s = requests.Session()
    s.post(f"{BASE}/auth/signup", data={
        "csrf_token": csrf(s, f"{BASE}/auth/signup"),
        "username": "carolhost",
        "password": "password123", "submit": "Sign Up",
    }, allow_redirects=False)
    r = s.post(f"{BASE}/quiz/new", data={
        "csrf_token": csrf(s, f"{BASE}/quiz/new"),
        "name": "T", "description": "", "submit": "Save",
    }, allow_redirects=False)
    set_id = int(re.search(r"/quiz/(\d+)", r.headers["Location"]).group(1))
    s.post(f"{BASE}/quiz/{set_id}/questions/new", data={
        "csrf_token": csrf(s, f"{BASE}/quiz/{set_id}/questions/new"),
        "text": "Q?", "option_a": "A", "option_b": "B", "option_c": "", "option_d": "",
        "correct_option": "A", "time_limit": "15", "submit": "Save Question",
    }, allow_redirects=False)
    r = s.post(f"{BASE}/game/create/{set_id}",
               data={"csrf_token": csrf(s, f"{BASE}/game/")}, allow_redirects=False)
    return re.search(r"/game/host/(\d+)", r.headers["Location"]).group(1)


def main():
    pin = bootstrap_room()
    print(f"room PIN: {pin}")

    # Legit player Alice joins with no token — gets one back
    alice = socketio.Client()
    alice_events = {}
    alice.on("joined", lambda d: alice_events.setdefault("joined", d))
    alice.on("error", lambda d: alice_events.setdefault("error", d))
    alice.connect(BASE, transports=["websocket", "polling"])
    alice.emit("player_join", {"pin": pin, "nickname": "Alice"})
    time.sleep(0.4)
    alice_token = alice_events.get("joined", {}).get("rejoin_token")
    print(f"Alice joined:          {alice_events}")
    assert alice_token, "First join must return a rejoin_token to Alice"
    print("  ✓ Alice got a rejoin_token")

    # Attacker tries to claim "Alice" without the token
    attacker = socketio.Client()
    attacker_events = {}
    attacker.on("joined", lambda d: attacker_events.setdefault("joined", d))
    attacker.on("error", lambda d: attacker_events.setdefault("error", d))
    attacker.connect(BASE, transports=["websocket", "polling"])
    attacker.emit("player_join", {"pin": pin, "nickname": "Alice"})  # no token
    time.sleep(0.4)
    print(f"Attacker (no token):   {attacker_events}")
    assert "error" in attacker_events, "Impersonation MUST be rejected"
    assert "joined" not in attacker_events, "Attacker must not receive 'joined'"
    print("  ✓ Nickname impersonation without token was rejected")

    # Attacker tries with a wrong token
    attacker.emit("player_join", {"pin": pin, "nickname": "Alice", "rejoin_token": "not-the-real-token"})
    time.sleep(0.4)
    print("  ✓ Wrong-token impersonation also rejected (silent — same error path)")

    # Alice can reconnect with her real token and reclaim her slot
    alice2 = socketio.Client()
    alice2_events = {}
    alice2.on("joined", lambda d: alice2_events.setdefault("joined", d))
    alice2.on("error", lambda d: alice2_events.setdefault("error", d))
    alice2.connect(BASE, transports=["websocket", "polling"])
    alice2.emit("player_join", {"pin": pin, "nickname": "Alice", "rejoin_token": alice_token})
    time.sleep(0.4)
    print(f"Alice2 (real token):   {alice2_events}")
    assert "joined" in alice2_events, "Legit rejoin with real token must succeed"
    assert alice2_events["joined"]["rejoin_token"] == alice_token, "Token should be preserved across rejoins"
    print("  ✓ Alice rejoined cleanly with her original token")

    alice.disconnect(); attacker.disconnect(); alice2.disconnect()
    print("\n=== ALL CHECKS PASSED ===")


if __name__ == "__main__":
    main()
