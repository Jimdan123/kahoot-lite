"""Debounces player_join broadcasts so a burst of simultaneous joins goes
out as one event instead of one per join.

player_joined broadcasts to every socket in the room regardless of payload
size (O(n) fanout per call), so a burst of N simultaneous joins is still
O(n) broadcasts x O(n) recipients = O(n^2) deliveries even with a tiny
per-join payload. Batching turns that into O(bursts) broadcasts instead of
O(joins) — true O(n) total, not just a smaller constant. Cost: a joining
player's chip can take up to JOIN_BATCH_WINDOW_SECONDS to show up on the
host's screen, which is imperceptible at this window and worth it for the
burst case."""
from __future__ import annotations

from app.extensions import socketio

JOIN_BATCH_WINDOW_SECONDS = 0.15

# pin -> nicknames that joined since the last flush.
_pending_joins: dict[str, list[str]] = {}
# pin -> whether a flush is already scheduled, so a burst of joins schedules
# exactly one background task instead of one per join.
_flush_scheduled: dict[str, bool] = {}


def queue_player_joined(pin: str, nickname: str) -> None:
    _pending_joins.setdefault(pin, []).append(nickname)
    if not _flush_scheduled.get(pin):
        _flush_scheduled[pin] = True
        socketio.start_background_task(_flush_pending_joins, pin)


def _flush_pending_joins(pin: str) -> None:
    socketio.sleep(JOIN_BATCH_WINDOW_SECONDS)
    # Grab-and-clear both bits of state before the emit below, which is the
    # only line in this function that can yield to another greenlet — do it
    # any later and a join arriving mid-flush could see `_flush_scheduled`
    # still True, skip scheduling its own flush, and sit in `_pending_joins`
    # with nothing left to ever send it (a lost-wakeup bug).
    nicknames = _pending_joins.pop(pin, [])
    _flush_scheduled.pop(pin, None)
    if nicknames:
        socketio.emit('players_joined', {'nicknames': nicknames}, room=pin)
