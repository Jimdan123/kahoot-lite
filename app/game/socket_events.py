import secrets
import time
from flask import request
from flask_login import current_user
from flask_socketio import emit, join_room
from app.extensions import socketio
from app.game import game_service


# Grace period (seconds) beyond a question's time_limit within which the server
# still accepts an answer, to absorb network jitter. Late answers are dropped.
ANSWER_GRACE_SECONDS = 1.0

# How long to hold a batch of joins open before broadcasting them as one
# event. player_joined broadcasts to every socket in the room regardless of
# payload size (O(n) fanout per call), so a burst of N simultaneous joins is
# still O(n) broadcasts x O(n) recipients = O(n^2) deliveries even with a
# tiny per-join payload. Batching turns that into O(bursts) broadcasts
# instead of O(joins) — true O(n) total, not just a smaller constant. Cost:
# a joining player's chip can take up to this long to show up on the host's
# screen, which is imperceptible at this window and worth it for the burst
# case; see _flush_pending_joins.
JOIN_BATCH_WINDOW_SECONDS = 0.15

# pin -> nicknames that joined since the last flush.
_pending_joins: dict[str, list[str]] = {}
# pin -> whether a flush is already scheduled, so a burst of joins schedules
# exactly one background task instead of one per join.
_flush_scheduled: dict[str, bool] = {}


@socketio.on('host_join')
def on_host_join(data):
    room = game_service.get_room(data.get('pin'))
    if not room:
        emit('error', {'message': 'Room not found'})
        return
    if not current_user.is_authenticated or current_user.id != room.owner_id:
        emit('error', {'message': 'Not authorized to host this room'})
        return
    if request.sid in room.players:
        # A socket that has joined as a player cannot also claim host,
        # which would give it both roles.
        emit('error', {'message': 'This socket is already a player in the room'})
        return
    room.host_sid = request.sid
    join_room(room.pin)
    emit('room_state', _room_state(room))
    emit('player_list', _player_list(room))
    # Catch a reconnecting host up to the live phase — without this a host
    # whose tab refreshes mid-game falls back to the static lobby markup and
    # can accidentally hit "Start Game" again, which used to restart the
    # entire game from question 0 (see on_start_game's state guard below).
    _resume_state_for_host(room)


def _token_matches(supplied, expected) -> bool:
    """Timing-safe compare that tolerates non-string junk from the wire.
    secrets.compare_digest raises TypeError on non-ASCII strings or non-strings,
    so we normalize before calling it — otherwise a malformed payload becomes
    an unhandled exception instead of a clean rejection."""
    if not isinstance(supplied, str) or not isinstance(expected, str) or not expected:
        return False
    try:
        return secrets.compare_digest(supplied, expected)
    except TypeError:
        return False


@socketio.on('player_join')
def on_player_join(data):
    pin = data.get('pin')
    nickname = (data.get('nickname') or '').strip()[:20]
    supplied_token = data.get('rejoin_token')
    room = game_service.get_room(pin)
    if not room or not nickname:
        emit('error', {'message': 'Cannot join this room'})
        return

    # Three legitimate paths:
    #   1. Same-tab reload while still active → active-nickname match, needs token
    #   2. Reconnect after a mid-game disconnect → orphan match, needs token
    #   3. Brand-new player during lobby → mint a fresh token
    active = room.find_active_by_nickname(nickname)
    orphan = None if active else room.orphans.get(nickname)

    if active:
        if not _token_matches(supplied_token, active.rejoin_token):
            emit('error', {'message': 'Nickname is already taken in this room'})
            return
        room.remove_player(active.sid)
        player = room.add_player(request.sid, nickname)
        player.rejoin_token = active.rejoin_token
        player.score = active.score
        player.last_answer = active.last_answer
        player.last_answer_correct = active.last_answer_correct
    elif orphan:
        if not _token_matches(supplied_token, orphan.rejoin_token):
            emit('error', {'message': 'Nickname is already taken in this room'})
            return
        room.take_orphan(nickname)
        player = room.add_player(request.sid, nickname)
        player.rejoin_token = orphan.rejoin_token
        player.score = orphan.score
        player.last_answer = orphan.last_answer
        player.last_answer_correct = orphan.last_answer_correct
    elif room.state != 'lobby':
        emit('error', {'message': 'Game already in progress'})
        return
    else:
        player = room.add_player(request.sid, nickname)
        player.rejoin_token = secrets.token_urlsafe(24)

    room.touch()
    join_room(pin)
    # rejoin_token goes only to this socket, never broadcast.
    emit('joined', {'nickname': nickname, 'rejoin_token': player.rejoin_token})
    # Queue this join to go out in the next batch rather than broadcasting
    # immediately (see JOIN_BATCH_WINDOW_SECONDS above for why). This used to
    # be `socketio.emit('player_list', _player_list(room), room=pin)` —
    # resending and re-rendering the whole roster on every single join.
    # (Separate issue from find_active_by_nickname in game_service.py — that
    # one was an O(n) in-memory scan; this one is network I/O to every
    # connected player, and was the bigger of the two.) The host's full
    # player_list snapshot is still sent once, in on_host_join above — that's
    # the only resync point a (re)connecting host needs; everyone else just
    # needs to eventually hear who joined. The client dedupes by nickname,
    # so the rare case where a still-active reconnect queues this for a
    # nickname it never saw leave (the `active` branch above) can't produce
    # a duplicate chip.
    _queue_player_joined(pin, nickname)

    # If this was a reconnect into a game-in-progress room, catch this socket
    # up to the current state so it isn't stranded on the waiting screen.
    if active or orphan:
        _resume_state_for_player(room, player)


def _queue_player_joined(pin: str, nickname: str) -> None:
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


@socketio.on('start_game')
def on_start_game(data):
    room = game_service.get_room(data.get('pin'))
    # Only valid from the lobby. Without this guard, a reconnecting host
    # whose UI fell back to the lobby screen (see on_host_join) could hit
    # "Start Game" mid-game and silently reset the whole room to question 0.
    if not room or request.sid != room.host_sid or room.state != 'lobby':
        return
    _advance_to_question(room, 0)


@socketio.on('submit_answer')
def on_submit_answer(data):
    room = game_service.get_room(data.get('pin'))
    if not room or room.state != 'question':
        return
    player = room.players.get(request.sid)
    if not player or player.last_answer is not None:
        return
    q = room.current_question()
    if q is None:
        return
    # Optional (backward-compatible) guard: a client that buffered this
    # submit_answer while disconnected (Socket.IO queues emits made while
    # offline and flushes them on reconnect) can otherwise have it land on
    # a *different* question than the one it was answering, since the room
    # may have advanced by the time the packet arrives. Older/other clients
    # that don't send question_index are unaffected.
    q_index = data.get('question_index')
    if q_index is not None and q_index != room.current_index:
        return
    choice = data.get('choice')
    elapsed = time.time() - (room.question_start_time or time.time())
    if elapsed > q['time_limit'] + ANSWER_GRACE_SECONDS:
        # Server-side deadline: reject late submissions so the client-side
        # timer cannot be bypassed for free points. Emitted as a distinct
        # event (not answer_ack) so a laggy player's UI can tell them what
        # happened instead of hanging on "Waiting for results..." forever;
        # test_security.py's late-answer regression check specifically
        # asserts no answer_ack is emitted for this path.
        emit('answer_late', {'message': 'Too late — the deadline already passed.'})
        return
    is_correct = (choice == q['correct_option'])
    earned = game_service.calculate_score(is_correct, elapsed, q['time_limit'])
    player.last_answer = choice
    player.last_answer_correct = is_correct
    player.score += earned
    emit('answer_ack', {'correct': is_correct, 'earned': earned, 'total': player.score})
    if all(p.last_answer is not None for p in room.players.values()):
        _reveal(room)


@socketio.on('reveal_answer')
def on_reveal(data):
    room = game_service.get_room(data.get('pin'))
    if not room or request.sid != room.host_sid or room.state != 'question':
        return
    _reveal(room)


@socketio.on('next_question')
def on_next(data):
    room = game_service.get_room(data.get('pin'))
    # Only valid from 'reveal'. Without this guard, two next_question events
    # arriving close together (an eager double-click, or duplicate delivery
    # on a flaky connection — no button is disabled after click) would each
    # advance current_index, skipping a question.
    if not room or request.sid != room.host_sid or room.state != 'reveal':
        return
    next_idx = room.current_index + 1
    if next_idx >= len(room.questions):
        _end_game(room)
    else:
        _advance_to_question(room, next_idx)


@socketio.on('disconnect')
def on_disconnect():
    for room in list(game_service.all_rooms().values()):
        if request.sid == room.host_sid:
            room.host_sid = None
        elif request.sid in room.players:
            nickname = room.players[request.sid].nickname
            # In the lobby a disconnect is a genuine leave — no score to
            # preserve, so drop the player outright. Mid-game, move them to
            # the orphan pool so a reconnect presenting the correct
            # rejoin_token can restore their state.
            if room.state == 'lobby':
                room.remove_player(request.sid)
            else:
                room.orphan_player(request.sid)
            # Same delta-not-snapshot reasoning as on_player_join.
            socketio.emit('player_left', {'nickname': nickname}, room=room.pin)


def _advance_to_question(room, index):
    room.touch()  # keep a live game from being swept as idle
    room.current_index = index
    room.state = 'question'
    room.question_start_time = time.time()
    for p in room.players.values():
        p.last_answer = None
        p.last_answer_correct = None
    q = room.current_question()
    socketio.emit('question_start', _question_payload(room, q, index), room=room.pin)
    # Server-side deadline enforcement for the *state transition*, not just
    # for scoring: previously only a host clicking "Reveal Answer" or every
    # player answering could end a question, so a question with an
    # unanswered player and an inattentive host would hang forever even
    # after every client's countdown hit zero.
    socketio.start_background_task(
        _auto_reveal_watchdog, room.pin, index, q['time_limit'] + ANSWER_GRACE_SECONDS
    )


def _auto_reveal_watchdog(pin, index, delay):
    socketio.sleep(delay)
    room = game_service.get_room(pin)
    # Re-check state + index: the room may have been revealed manually,
    # swept, or advanced again by the time this fires.
    if room and room.state == 'question' and room.current_index == index:
        _reveal(room)


def _reveal(room):
    q = room.current_question()
    if q is None:
        return  # nothing to reveal (e.g. host called reveal in the lobby)
    room.state = 'reveal'
    socketio.emit(
        'question_reveal',
        {'correct_option': q['correct_option'], 'leaderboard': room.leaderboard()},
        room=room.pin,
    )


def _end_game(room):
    room.state = 'done'
    room.finished_at = time.time()  # starts the grace clock before the reaper sweeps it
    socketio.emit('game_over', {'leaderboard': room.leaderboard()}, room=room.pin)


def _room_state(room):
    return {
        'pin': room.pin,
        'state': room.state,
        'player_count': len(room.players),
    }


def _player_list(room):
    return [{'nickname': p.nickname} for p in room.players.values()]


def _question_payload(room, q, index):
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


def _resume_state_for_player(room, player):
    """Emit just to the current socket enough state to render the current
    game phase, so a reconnected player lands on the right screen instead of
    the initial waiting spinner."""
    if room.state == 'question':
        q = room.current_question()
        if q is not None:
            payload = _question_payload(room, q, room.current_index)
            # None if they hadn't answered yet; their previous choice
            # otherwise, so the client can lock the buttons.
            payload['resumed_answer'] = player.last_answer
            emit('question_start', payload)
    elif room.state == 'reveal':
        q = room.current_question()
        if q is not None:
            emit('question_reveal', {
                'correct_option': q['correct_option'],
                'leaderboard': room.leaderboard(),
            })
    elif room.state == 'done':
        emit('game_over', {'leaderboard': room.leaderboard()})


def _resume_state_for_host(room):
    """Same idea as _resume_state_for_player, but for a reconnecting host.
    Without this the host's UI falls back to the static lobby markup on any
    reconnect (refresh, sleep/wake, network blip) with no indication the
    game is actually mid-question or mid-reveal."""
    if room.state == 'question':
        q = room.current_question()
        if q is not None:
            emit('question_start', _question_payload(room, q, room.current_index))
    elif room.state == 'reveal':
        q = room.current_question()
        if q is not None:
            emit('question_reveal', {
                'correct_option': q['correct_option'],
                'leaderboard': room.leaderboard(),
            })
    elif room.state == 'done':
        emit('game_over', {'leaderboard': room.leaderboard()})
