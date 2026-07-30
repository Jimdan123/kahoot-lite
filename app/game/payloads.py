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
    fresh full countdown — the client turns this into a local deadline
    (now + time_left) and re-derives "remaining" from that deadline on every
    tick, so a backgrounded/throttled tab self-corrects instead of drifting."""
    elapsed = time.time() - (room.question_start_time or time.time())
    time_left = max(0.0, q['time_limit'] - elapsed)
    return {
        'index': index,
        'total': len(room.questions),
        'text': q['text'],
        'options': q['options'],
        'time_limit': q['time_limit'],
        'time_left': round(time_left, 1),
    }
