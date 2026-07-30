"""Pure payload-shaping helpers for game Socket.IO events — no I/O, just
dict construction from Room/Player state. Split out of socket_events.py so
transport/event routing stays separate from response shaping."""
from __future__ import annotations

import time


def room_state(room):
    return {
        'pin': room.pin,
        'state': room.state,
        'player_count': len(room.players),
    }


def player_list(room):
    return [{'nickname': p.nickname} for p in room.players.values()]


def question_payload(room, q, index):
    """Shared shape for question_start, sent both on a fresh advance and on
    a reconnect resume. time_left is computed server-side from the real
    question_start_time (not just q['time_limit']) so a client that
    reconnects mid-question is told the actual remaining time instead of a
    fresh full countdown.

    deadline_ms is the same deadline expressed as an absolute server-clock
    timestamp rather than a relative "seconds from now" — a client that has
    calibrated its clock offset against the server (see socket_events.py's
    sync_time / the templates' calibrateClock()) converts this straight to
    an equivalent local timestamp and counts down against that. Without it,
    each client would instead compute "now + time_left" using whatever
    moment IT happened to receive this particular message, so two clients
    whose delivery latency for this one broadcast differs by e.g. 300ms
    would disagree by 300ms about when the countdown hits zero — even
    though the server means the same real-world instant for everyone.
    time_left is kept as the fallback for a client that hasn't calibrated
    yet."""
    start = room.question_start_time or time.time()
    elapsed = time.time() - start
    time_left = max(0.0, q['time_limit'] - elapsed)
    return {
        'index': index,
        'total': len(room.questions),
        'text': q['text'],
        'options': q['options'],
        'time_limit': q['time_limit'],
        'time_left': round(time_left, 1),
        'deadline_ms': (start + q['time_limit']) * 1000,
    }
