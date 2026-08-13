from flask import render_template, redirect, url_for, flash, abort
from flask_login import login_required, current_user
from app.quiz import quiz_bp
from app.quiz.forms import QuestionSetForm, QuestionForm
from app.extensions import db
from app.models import QuestionSet, Question


@quiz_bp.route('/')
@login_required
def index():
    sets = current_user.question_sets.order_by(QuestionSet.created_at.desc()).all()
    return render_template('quiz/index.html', question_sets=sets)


@quiz_bp.route('/new', methods=['GET', 'POST'])
@login_required
def new_set():
    form = QuestionSetForm()
    if form.validate_on_submit():
        qs = QuestionSet(name=form.name.data, description=form.description.data, owner=current_user)
        db.session.add(qs)
        db.session.commit()
        flash('Question set created. Add some questions.', 'success')
        return redirect(url_for('quiz.detail', set_id=qs.id))
    return render_template('quiz/new_set.html', form=form)


@quiz_bp.route('/<int:set_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_set(set_id):
    qs = _get_owned_set(set_id)
    form = QuestionSetForm(obj=qs)
    if form.validate_on_submit():
        qs.name = form.name.data
        qs.description = form.description.data
        db.session.commit()
        flash('Question set updated.', 'success')
        return redirect(url_for('quiz.detail', set_id=qs.id))
    return render_template('quiz/edit_set.html', form=form, question_set=qs)


@quiz_bp.route('/<int:set_id>')
@login_required
def detail(set_id):
    qs = _get_owned_set(set_id)
    return render_template('quiz/detail.html', question_set=qs)


@quiz_bp.route('/<int:set_id>/questions/new', methods=['GET', 'POST'])
@login_required
def new_question(set_id):
    qs = _get_owned_set(set_id)
    form = QuestionForm()
    if form.validate_on_submit():
        max_position = qs.questions.order_by(None).with_entities(db.func.max(Question.position)).scalar()
        q = Question(
            question_set=qs,
            position=0 if max_position is None else max_position + 1,
            text=form.text.data,
            option_a=form.option_a.data,
            option_b=form.option_b.data,
            option_c=form.option_c.data or None,
            option_d=form.option_d.data or None,
            correct_option=form.correct_option.data,
            time_limit=form.time_limit.data,
        )
        db.session.add(q)
        db.session.commit()
        flash('Question added.', 'success')
        return redirect(url_for('quiz.detail', set_id=qs.id))
    return render_template('quiz/new_question.html', form=form, question_set=qs)


@quiz_bp.route('/<int:set_id>/questions/<int:question_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_question(set_id, question_id):
    qs = _get_owned_set(set_id)
    q = qs.questions.filter_by(id=question_id).first_or_404()
    form = QuestionForm(obj=q)
    if form.validate_on_submit():
        q.text = form.text.data
        q.option_a = form.option_a.data
        q.option_b = form.option_b.data
        q.option_c = form.option_c.data or None
        q.option_d = form.option_d.data or None
        q.correct_option = form.correct_option.data
        q.time_limit = form.time_limit.data
        db.session.commit()
        flash('Question updated.', 'success')
        return redirect(url_for('quiz.detail', set_id=qs.id))
    return render_template('quiz/edit_question.html', form=form, question_set=qs, question=q)


@quiz_bp.route('/<int:set_id>/questions/<int:question_id>/delete', methods=['POST'])
@login_required
def delete_question(set_id, question_id):
    qs = _get_owned_set(set_id)
    q = qs.questions.filter_by(id=question_id).first_or_404()
    db.session.delete(q)
    db.session.commit()
    flash('Question deleted.', 'info')
    return redirect(url_for('quiz.detail', set_id=qs.id))


@quiz_bp.route('/<int:set_id>/delete', methods=['POST'])
@login_required
def delete_set(set_id):
    qs = _get_owned_set(set_id)
    db.session.delete(qs)
    db.session.commit()
    flash('Question set deleted.', 'info')
    return redirect(url_for('quiz.index'))


def _get_owned_set(set_id):
    qs = QuestionSet.query.get_or_404(set_id)
    if qs.owner_id != current_user.id:
        abort(403)
    return qs
