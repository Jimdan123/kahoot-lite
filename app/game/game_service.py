"""
In-memory registry of live game rooms.

Room state is kept in a plain Python dict — perfectly fine for a single-process
dev/demo deployment. If we ever scale to multiple workers we would need to move
this state to Redis (see RESOURCES.md for the sticky-sessions discussion).
"""
import random
import string
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Player:
    sid: str
    nickname: str
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
    players: Dict[str, Player] = field(default_factory=dict)
    current_index: int = -1
    state: str = 'lobby'  # lobby | question | reveal | done
    question_start_time: Optional[float] = None

    def add_player(self, sid: str, nickname: str) -> Player:
        p = Player(sid=sid, nickname=nickname)
        self.players[sid] = p
        return p

    def remove_player(self, sid: str) -> None:
        self.players.pop(sid, None)

    def current_question(self) -> Optional[dict]:
        if 0 <= self.current_index < len(self.questions):
            return self.questions[self.current_index]
        return None

    def leaderboard(self) -> List[dict]:
        ranked = sorted(self.players.values(), key=lambda p: p.score, reverse=True)
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
