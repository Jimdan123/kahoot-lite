"""Regression test for the mid-game reconnect flow.

The rejoin-token mechanism only pays off if a player who disconnects
mid-game can actually reclaim their slot with score/answers preserved.

Threat/failure model this guards against:
- The prior on_disconnect deleted the player entirely, so the rejoin token
  had no matching Player to reclaim and the reconnect fell through to the
  "Game already in progress" branch. The whole feature was unreachable.
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
        "email": "dave@example.com", "display_name": "Dave",
        "password": "password123", "password_confirm": "password123", "submit": "Sign Up",
    }, allow_redirects=False)
    r = s.post(f"{BASE}/quiz/new", data={
        "csrf_token": csrf(s, f"{BASE}/quiz/new"),
        "name": "R", "description": "", "submit": "Save",
    }, allow_redirects=False)
    set_id = int(re.search(r"/quiz/(\d+)", r.headers["Location"]).group(1))
    s.post(f"{BASE}/quiz/{set_id}/questions/new", data={
        "csrf_token": csrf(s, f"{BASE}/quiz/{set_id}/questions/new"),
        "text": "Q?", "option_a": "A", "option_b": "B", "option_c": "", "option_d": "",
        "correct_option": "A", "time_limit": "15", "submit": "Save Question",
    }, allow_redirects=False)
    r = s.post(f"{BASE}/game/create/{set_id}",
               data={"csrf_token": csrf(s, f"{BASE}/game/")}, allow_redirects=False)
    pin = re.search(r"/game/host/(\d+)", r.headers["Location"]).group(1)
    return s, pin


def main():
    host_session, pin = bootstrap_room()
    print(f"room PIN: {pin}")

    # Host connects (authenticated)
    host = socketio.Client()
    host.connect(BASE, transports=["websocket", "polling"],
                 headers={"Cookie": "; ".join(f"{c.name}={c.value}" for c in host_session.cookies)})
    host.emit("host_join", {"pin": pin})
    time.sleep(0.3)

    # Bob joins in the lobby, gets his token
    bob1 = socketio.Client()
    bob1_events = {}
    bob1.on("joined", lambda d: bob1_events.setdefault("joined", d))
    bob1.connect(BASE, transports=["websocket", "polling"])
    bob1.emit("player_join", {"pin": pin, "nickname": "Bob"})
    time.sleep(0.4)
    bob_token = bob1_events["joined"]["rejoin_token"]
    print(f"Bob first join, token: {bob_token[:10]}…")
    assert bob_token

    # A second player joins so Bob answering doesn't auto-reveal (the "all
    # players answered" trigger would kick the room out of 'question' state
    # and defeat the point of this reconnect test).
    lurker = socketio.Client()
    lurker.connect(BASE, transports=["websocket", "polling"])
    lurker.emit("player_join", {"pin": pin, "nickname": "Lurker"})
    time.sleep(0.3)

    # Host starts the game — question fires
    host.emit("start_game", {"pin": pin})
    time.sleep(0.3)

    # Bob answers correctly (Lurker deliberately does not)
    bob1_answer = {}
    bob1.on("answer_ack", lambda d: bob1_answer.setdefault("ack", d))
    bob1.emit("submit_answer", {"pin": pin, "choice": "A"})
    time.sleep(0.4)
    assert bob1_answer["ack"]["correct"], "Bob should have got it right"
    bob_score_before = bob1_answer["ack"]["total"]
    print(f"Bob answered A correctly, score = {bob_score_before}")

    # Bob's laptop dies — hard disconnect mid-game
    bob1.disconnect()
    time.sleep(0.3)

    # Attacker without token tries to slip into Bob's slot
    attacker = socketio.Client()
    attacker_events = {}
    attacker.on("joined", lambda d: attacker_events.setdefault("joined", d))
    attacker.on("error", lambda d: attacker_events.setdefault("error", d))
    attacker.connect(BASE, transports=["websocket", "polling"])
    attacker.emit("player_join", {"pin": pin, "nickname": "Bob"})
    time.sleep(0.4)
    print(f"attacker attempt (no token): {attacker_events}")
    assert "error" in attacker_events, "Attacker must be rejected even against an orphan"
    assert "joined" not in attacker_events
    attacker.disconnect()

    # Bob comes back on his phone, still holds the token
    bob2 = socketio.Client()
    bob2_events = {}
    bob2_resume = {}
    bob2.on("joined", lambda d: bob2_events.setdefault("joined", d))
    bob2.on("question_start", lambda d: bob2_resume.setdefault("question_start", d))
    bob2.connect(BASE, transports=["websocket", "polling"])
    bob2.emit("player_join", {"pin": pin, "nickname": "Bob", "rejoin_token": bob_token})
    time.sleep(0.5)

    print(f"Bob rejoined: {bob2_events}")
    print(f"Bob got resume state: {bob2_resume}")
    assert "joined" in bob2_events, "Bob should have reclaimed his slot"
    assert "question_start" in bob2_resume, "Reconnect must catch the socket up with a question_start"
    assert bob2_resume["question_start"]["resumed_answer"] == "A", "Client must be told they already picked A"

    # After host reveals + next, Bob's score should still be the same as before disconnect
    host.emit("reveal_answer", {"pin": pin})
    time.sleep(0.3)
    # Ask for the leaderboard via game_over path — advance past all questions
    host.emit("next_question", {"pin": pin})
    time.sleep(0.3)

    bob2.disconnect(); host.disconnect(); lurker.disconnect()
    print(f"\n✓ Reconnect preserved Bob's score ({bob_score_before}) and answer choice")
    print("=== ALL CHECKS PASSED ===")


if __name__ == "__main__":
    main()
