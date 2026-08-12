"""
In-memory registry of live game rooms.

Room state is kept in a plain Python dict — perfectly fine for a single-process
dev/demo deployment. If we ever scale to multiple workers we would need to move
this state to Redis (see RESOURCES.md for the sticky-sessions discussion).
"""
import secrets
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
    # High-entropy token for the QR code / "copy link" convenience join path
    # — deliberately NOT the pin. The pin is a 6-digit human-typeable code
    # (verbally readable, shown big on the host's screen); putting it
    # straight into a shareable URL means the URL itself is just a
    # guessable 6-digit number sitting in the browser address bar/history/
    # server logs. This token is unrelated to the pin and effectively
    # unguessable, so a leaked/forwarded link doesn't reduce join-guessing
    # to "read the screen" the way a pin-bearing URL would. Multi-use for
    # the room's lifetime, same as the pin — many players join off one QR.
    qr_token: str = ''
    host_sid: Optional[str] = None
    # Active sockets keyed by socket id.
    players: Dict[str, Player] = field(default_factory=dict)
    # Secondary index (nickname -> Player) mirroring `players`, so
    # find_active_by_nickname is O(1) instead of scanning every player on
    # every join. Kept in sync by add_player/remove_player/orphan_player —
    # never write to this directly, go through those methods.
    _by_nickname: Dict[str, Player] = field(default_factory=dict, repr=False)
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
        self._by_nickname[nickname] = p
        return p

    def remove_player(self, sid: str) -> None:
        p = self.players.pop(sid, None)
        if p is not None:
            self._by_nickname.pop(p.nickname, None)

    def orphan_player(self, sid: str) -> Optional[Player]:
        """Move an active player to the orphan pool (called on disconnect)."""
        p = self.players.pop(sid, None)
        if p is not None:
            self._by_nickname.pop(p.nickname, None)
            self.orphans[p.nickname] = p
        return p

    def take_orphan(self, nickname: str) -> Optional[Player]:
        """Remove and return an orphan by nickname, for a rejoining player."""
        return self.orphans.pop(nickname, None)

    def find_active_by_nickname(self, nickname: str) -> Optional[Player]:
        return self._by_nickname.get(nickname)

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
# Reverse index: qr_token -> pin, for O(1) lookup from the QR/direct-link
# join path without scanning every room. Kept in sync by create_room/
# delete_room/sweep_stale_rooms — never write to this directly.
_rooms_by_token: Dict[str, str] = {}


def create_room(question_set_id: int, owner_id: int, questions: List[dict]) -> Room:
    pin = _generate_unique_pin()
    qr_token = _generate_unique_qr_token()
    room = Room(pin=pin, question_set_id=question_set_id, owner_id=owner_id,
                questions=questions, qr_token=qr_token)
    _rooms[pin] = room
    _rooms_by_token[qr_token] = pin
    return room


def get_room(pin: str) -> Optional[Room]:
    return _rooms.get(pin)


def get_room_by_token(qr_token: str) -> Optional[Room]:
    pin = _rooms_by_token.get(qr_token)
    return _rooms.get(pin) if pin else None


def delete_room(pin: str) -> None:
    room = _rooms.pop(pin, None)
    if room is not None:
        _rooms_by_token.pop(room.qr_token, None)


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
        room = _rooms.pop(pin, None)
        if room is not None:
            _rooms_by_token.pop(room.qr_token, None)
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
    # secrets.choice, not random.choice — this app already uses `secrets`
    # elsewhere for security-sensitive randomness (job IDs, uploaded
    # filenames); the pin gates room access the same way, so it should use
    # the same non-predictable source rather than the plain `random` module
    # (Mersenne Twister — reconstructible from enough observed output).
    while True:
        pin = ''.join(secrets.choice(string.digits) for _ in range(6))
        if pin not in _rooms:
            return pin


def _generate_unique_qr_token() -> str:
    while True:
        token = secrets.token_urlsafe(16)
        if token not in _rooms_by_token:
            return token


def calculate_score(is_correct: bool, time_taken: float, time_limit: float) -> int:
    """Kahoot-style scoring: correct answers get 500 + up to 500 for speed."""
    if not is_correct:
        return 0
    if time_limit <= 0:
        return 500
    fraction_remaining = max(0.0, 1.0 - (time_taken / time_limit))
    return int(500 + 500 * fraction_remaining)
