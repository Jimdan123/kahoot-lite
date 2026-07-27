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


@socketio.on('player_join')
def on_player_join(data):
    pin = data.get('pin')
    nickname = (data.get('nickname') or '').strip()[:20]
    supplied_token = data.get('rejoin_token') or ''
    room = game_service.get_room(pin)
    if not room or not nickname:
        emit('error', {'message': 'Cannot join this room'})
        return

    existing = next((p for p in room.players.values() if p.nickname == nickname), None)
    if existing:
        # Nickname taken. Only the socket that got the original rejoin_token
        # can reclaim it — nicknames are public (broadcast in player_list),
        # so we cannot treat name-match as identity proof.
        if not supplied_token or not secrets.compare_digest(supplied_token, existing.rejoin_token):
            emit('error', {'message': 'Nickname is already taken in this room'})
            return
        room.remove_player(existing.sid)
        player = room.add_player(request.sid, nickname)
        player.rejoin_token = existing.rejoin_token  # keep the same secret across reconnects
        player.score = existing.score
        player.last_answer = existing.last_answer
        player.last_answer_correct = existing.last_answer_correct
    elif room.state != 'lobby':
        emit('error', {'message': 'Game already in progress'})
        return
    else:
        player = room.add_player(request.sid, nickname)
        player.rejoin_token = secrets.token_urlsafe(24)

    join_room(pin)
    # rejoin_token is emitted only to this socket (default target), never
    # included in the broadcast player_list.
    emit('joined', {'nickname': nickname, 'rejoin_token': player.rejoin_token})
    emit('player_list', _player_list(room), to=pin)


@socketio.on('start_game')
def on_start_game(data):
    room = game_service.get_room(data.get('pin'))
    if not room or request.sid != room.host_sid:
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
    choice = data.get('choice')
    elapsed = time.time() - (room.question_start_time or time.time())
    if elapsed > q['time_limit'] + ANSWER_GRACE_SECONDS:
        # Server-side deadline: reject late submissions so the client-side
        # timer cannot be bypassed for free points.
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
    if room and request.sid == room.host_sid:
        _reveal(room)


@socketio.on('next_question')
def on_next(data):
    room = game_service.get_room(data.get('pin'))
    if not room or request.sid != room.host_sid:
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
            room.remove_player(request.sid)
            emit('player_list', _player_list(room), to=room.pin)


def _advance_to_question(room, index):
    room.current_index = index
    room.state = 'question'
    room.question_start_time = time.time()
    for p in room.players.values():
        p.last_answer = None
        p.last_answer_correct = None
    q = room.current_question()
    public_q = {
        'index': index,
        'total': len(room.questions),
        'text': q['text'],
        'options': q['options'],
        'time_limit': q['time_limit'],
    }
    emit('question_start', public_q, to=room.pin)


def _reveal(room):
    q = room.current_question()
    if q is None:
        return  # nothing to reveal (e.g. host called reveal in the lobby)
    room.state = 'reveal'
    emit(
        'question_reveal',
        {'correct_option': q['correct_option'], 'leaderboard': room.leaderboard()},
        to=room.pin,
    )


def _end_game(room):
    room.state = 'done'
    emit('game_over', {'leaderboard': room.leaderboard()}, to=room.pin)


def _room_state(room):
    return {
        'pin': room.pin,
        'state': room.state,
        'player_count': len(room.players),
    }


def _player_list(room):
    return [{'nickname': p.nickname} for p in room.players.values()]
