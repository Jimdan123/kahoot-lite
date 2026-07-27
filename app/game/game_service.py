"""
In-memory registry of live game rooms.

Room state is kept in a plain Python dict — perfectly fine for a single-process
dev/demo deployment. If we ever scale to multiple workers we would need to move
this state to Redis (see RESOURCES.md for the sticky-sessions discussion).
"""
import random
import string
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# How long a finished game lingers before it is swept, so players can still
# read the final leaderboard after the last question.
DONE_GRACE_SECONDS = 5 * 60
# How long a room with no activity (abandoned lobby, stalled game) survives.
MAX_IDLE_SECONDS = 60 * 60
# How often the background reaper wakes up to sweep.
REAP_INTERVAL_SECONDS = 5 * 60


@dataclass
class Player:
    sid: str
    nickname: str
    # Random per-player secret returned only to this player's socket on first
    # join. Required to reclaim the nickname on reconnect. Blocks nickname
    # impersonation since nicknames are broadcast publicly.
    rejoin_token: str = ''
    score: int = 0
    last_answer: Optional[str] = None
    last_answer_correct: Optional[bool] = None


@dataclass
class Room:
    pin: str
    question_set_id: int
    owner_id: int  # id of the User who created the room; only they can host
    questions: List[dict]
    host_sid: Optional[str] = None
    # Active sockets keyed by socket id.
    players: Dict[str, Player] = field(default_factory=dict)
    # Players that disconnected mid-game (keyed by nickname). Their score,
    # last_answer, and rejoin_token are preserved so a reconnect presenting
    # the matching token can restore them without data loss.
    orphans: Dict[str, Player] = field(default_factory=dict)
    current_index: int = -1
    state: str = 'lobby'  # lobby | question | reveal | done
    question_start_time: Optional[float] = None
    last_activity: float = field(default_factory=time.time)
    finished_at: Optional[float] = None

    def touch(self) -> None:
        """Mark the room as active so the reaper doesn't sweep it."""
        self.last_activity = time.time()

    def add_player(self, sid: str, nickname: str) -> Player:
        p = Player(sid=sid, nickname=nickname)
        self.players[sid] = p
        return p

    def remove_player(self, sid: str) -> None:
        self.players.pop(sid, None)

    def orphan_player(self, sid: str) -> Optional[Player]:
        """Move an active player to the orphan pool (called on disconnect)."""
        p = self.players.pop(sid, None)
        if p is not None:
            self.orphans[p.nickname] = p
        return p

    def take_orphan(self, nickname: str) -> Optional[Player]:
        """Remove and return an orphan by nickname, for a rejoining player."""
        return self.orphans.pop(nickname, None)

    def find_active_by_nickname(self, nickname: str) -> Optional[Player]:
        for p in self.players.values():
            if p.nickname == nickname:
                return p
        return None

    def current_question(self) -> Optional[dict]:
        if 0 <= self.current_index < len(self.questions):
            return self.questions[self.current_index]
        return None

    def leaderboard(self) -> List[dict]:
        # Include orphans so a briefly-disconnected player's score still shows.
        combined = list(self.players.values()) + list(self.orphans.values())
        ranked = sorted(combined, key=lambda p: p.score, reverse=True)
        return [{'nickname': p.nickname, 'score': p.score} for p in ranked]


_rooms: Dict[str, Room] = {}


def create_room(question_set_id: int, owner_id: int, questions: List[dict]) -> Room:
    pin = _generate_unique_pin()
    room = Room(pin=pin, question_set_id=question_set_id, owner_id=owner_id, questions=questions)
    _rooms[pin] = room
    return room


def get_room(pin: str) -> Optional[Room]:
    return _rooms.get(pin)


def delete_room(pin: str) -> None:
    _rooms.pop(pin, None)


def all_rooms() -> Dict[str, Room]:
    return _rooms


def sweep_stale_rooms(now: Optional[float] = None) -> int:
    """
    Remove finished rooms past their grace period and any room that has been
    idle too long. Returns the number of rooms removed. Safe to call anytime.
    """
    if now is None:
        now = time.time()
    stale = []
    for pin, room in _rooms.items():
        if room.state == 'done' and room.finished_at is not None:
            if now - room.finished_at > DONE_GRACE_SECONDS:
                stale.append(pin)
        elif now - room.last_activity > MAX_IDLE_SECONDS:
            stale.append(pin)
    for pin in stale:
        _rooms.pop(pin, None)
    return len(stale)


def reaper_loop(socketio) -> None:
    """
    Background greenlet: periodically sweep stale rooms for the life of the
    process. Uses socketio.sleep so it cooperates with the gevent worker.
    """
    while True:
        socketio.sleep(REAP_INTERVAL_SECONDS)
        sweep_stale_rooms()


def _generate_unique_pin() -> str:
    while True:
        pin = ''.join(random.choices(string.digits, k=6))
        if pin not in _rooms:
            return pin


def calculate_score(is_correct: bool, time_taken: float, time_limit: float) -> int:
    """Kahoot-style scoring: correct answers get 500 + up to 500 for speed."""
    if not is_correct:
        return 0
    if time_limit <= 0:
        return 500
    fraction_remaining = max(0.0, 1.0 - (time_taken / time_limit))
    return int(500 + 500 * fraction_remaining)
