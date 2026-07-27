import io
import base64
import qrcode
from flask import render_template, redirect, url_for, flash, request, abort
from flask_login import login_required, current_user
from app.game import game_bp
from app.game import game_service
from app.models import QuestionSet


@game_bp.route('/')
@login_required
def host_dashboard():
    sets = current_user.question_sets.all()
    return render_template('game/host_dashboard.html', question_sets=sets)


@game_bp.route('/create/<int:set_id>', methods=['POST'])
@login_required
def create_room(set_id):
    qs = QuestionSet.query.get_or_404(set_id)
    if qs.owner_id != current_user.id:
        abort(403)
    if qs.questions.count() == 0:
        flash('Add questions before hosting a game.', 'warning')
        return redirect(url_for('quiz.detail', set_id=qs.id))
    questions_data = [_serialize_question(q) for q in qs.questions]
    room = game_service.create_room(qs.id, current_user.id, questions_data)
    return redirect(url_for('game.host_view', pin=room.pin))


@game_bp.route('/host/<pin>')
@login_required
def host_view(pin):
    room = game_service.get_room(pin)
    if not room:
        flash('Room not found (may have expired).', 'warning')
        return redirect(url_for('game.host_dashboard'))
    if room.owner_id != current_user.id:
        abort(403)
    join_url = url_for('game.join', pin=pin, _external=True)
    qr_data_uri = _make_qr_data_uri(join_url)
    return render_template('game/host_view.html', room=room, join_url=join_url, qr_data_uri=qr_data_uri)


@game_bp.route('/join', methods=['GET', 'POST'])
def join():
    prefill_pin = request.args.get('pin', '')
    if request.method == 'POST':
        pin = request.form.get('pin', '').strip()
        nickname = request.form.get('nickname', '').strip()
        if not pin or not nickname:
            flash('PIN and nickname are required.', 'warning')
            return render_template('game/join.html', pin=pin, nickname=nickname)
        room = game_service.get_room(pin)
        if not room:
            flash('Invalid PIN.', 'danger')
            return render_template('game/join.html', pin=pin, nickname=nickname)
        if room.state != 'lobby':
            flash('Game already in progress.', 'warning')
            return render_template('game/join.html', pin=pin, nickname=nickname)
        return redirect(url_for('game.player_view', pin=pin, nickname=nickname))
    return render_template('game/join.html', pin=prefill_pin, nickname='')


@game_bp.route('/play/<pin>')
def player_view(pin):
    nickname = request.args.get('nickname', '').strip()
    if not nickname:
        return redirect(url_for('game.join', pin=pin))
    room = game_service.get_room(pin)
    if not room:
        flash('Room not found.', 'danger')
        return redirect(url_for('game.join'))
    return render_template('game/player_view.html', pin=pin, nickname=nickname)


def _serialize_question(q):
    return {
        'id': q.id,
        'position': q.position,
        'text': q.text,
        'options': {k: v for k, v in q.options().items() if v},
        'correct_option': q.correct_option,
        'time_limit': q.time_limit,
    }


def _make_qr_data_uri(url):
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return f'data:image/png;base64,{base64.b64encode(buf.getvalue()).decode("ascii")}'
