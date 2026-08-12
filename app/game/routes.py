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
    # The QR/link uses room.qr_token, not the pin — see game_service.Room's
    # qr_token field for why. This keeps the shareable URL from being just
    # a guessable 6-digit number sitting in an address bar/history/log.
    join_url = url_for('game.join_via_token', token=room.qr_token, _external=True)
    qr_data_uri = _make_qr_data_uri(join_url)
    return render_template('game/host_view.html', room=room, join_url=join_url, qr_data_uri=qr_data_uri)


@game_bp.route('/join', methods=['GET', 'POST'])
def join():
    prefill_pin = request.args.get('pin', '')
    if request.method == 'POST':
        pin = request.form.get('pin', '').strip()
        # Truncate here to match the cap socket_events.on_player_join applies —
        # otherwise the waiting-screen greeting can show a longer name than
        # what actually gets registered as the player's nickname.
        nickname = request.form.get('nickname', '').strip()[:20]
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


@game_bp.route('/join/<token>')
def join_via_token(token):
    """QR-code / "copy link" entry point — resolves the opaque qr_token to
    a room and renders the normal join form with the pin prefilled, WITHOUT
    ever redirecting to a pin-bearing URL (that would just relocate the
    leak from the QR link to the next address-bar entry). An invalid/
    expired token falls back to the plain PIN-entry form rather than
    revealing whether the token ever existed."""
    room = game_service.get_room_by_token(token)
    if not room:
        flash('This join link has expired or is invalid — enter the PIN instead.', 'warning')
        return render_template('game/join.html', pin='', nickname='')
    return render_template('game/join.html', pin=room.pin, nickname='')


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
