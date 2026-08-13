# Edit/delete for question sets and questions

## Context

`app/quiz/routes.py` currently only supports create-and-leave-it: `new_set` (create a `QuestionSet`), `new_question` (add a `Question` to it), `detail` (view), and `delete_set` (delete the whole set, cascading its questions). There is no way to fix a typo in a set's name, correct a wrong answer on a question, or remove a single bad question without deleting and rebuilding the entire set — true whether the set was built by hand or by the AI pipeline (Part 1.2).

`QuestionSetForm` and `QuestionForm` (`app/quiz/forms.py`) are already generic enough to reuse for editing — their field names match the model columns exactly (`name`/`description` on `QuestionSet`; `text`/`option_a..d`/`correct_option`/`time_limit` on `Question`), so WTForms' `Form(obj=instance)` constructor pre-fills them with no extra code.

**Safety check performed during design:** deleting a single question leaves a gap in `Question.position` (e.g. 0,1,3 after deleting position 2). Verified this is harmless: the live game loop (`app/game/game_service.py:101-102`) indexes into a plain ordered Python list (`self.questions[self.current_index]`), not by the `position` value, and the "Question N of Total" display (`host_view.html`/`player_view.html`) is computed from that list index, not `position`. `position` is otherwise only used as a SQLAlchemy `order_by` key (`app/models.py:98`), where gaps are irrelevant. So no reindexing logic is needed anywhere in this feature.

## Decisions

- Full scope: edit a set's name/description, edit any field of an existing question, and delete a single question independently of the whole set.
- Reuse the existing forms as-is — no new form classes.
- Follow this codebase's existing convention of one small template per route (matches `new_set.html`/`new_question.html`) rather than introducing a shared macro/partial.
- No "can't edit while a game using this set is live" guard — out of scope, and consistent with `delete_set` today having no such guard either.
- No question-reordering UI — out of scope.

## 1. Routes (`app/quiz/routes.py`)

Three new view functions, following the existing `_get_owned_set` ownership-check pattern:

```python
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
```

`qs.questions.filter_by(id=question_id)` is scoped to a set already ownership-checked by `_get_owned_set`, so a question belonging to another user's set 404s rather than needing a separate ownership check — same pattern the rest of this file already uses.

## 2. Templates

**`app/templates/quiz/edit_set.html`** (new, mirrors `new_set.html`):
```html
{% extends 'base.html' %}
{% block content %}
<h2>Edit Question Set</h2>
<form method="POST" class="col-md-6">
    {{ form.hidden_tag() }}
    <div class="mb-3">
        {{ form.name.label(class='form-label') }}
        {{ form.name(class='form-control') }}
    </div>
    <div class="mb-3">
        {{ form.description.label(class='form-label') }}
        {{ form.description(class='form-control', rows=3) }}
    </div>
    {{ form.submit(class='btn btn-primary') }}
    <a href="{{ url_for('quiz.detail', set_id=question_set.id) }}" class="btn btn-outline-secondary">Cancel</a>
</form>
{% endblock %}
```

**`app/templates/quiz/edit_question.html`** (new, mirrors `new_question.html`, same field layout, heading and Back-link changed):
```html
{% extends 'base.html' %}
{% block content %}
<h2>Edit Question</h2>
<form method="POST" class="col-md-8">
    {{ form.hidden_tag() }}
    {% for field in form if field.errors %}
        {% for error in field.errors %}
            <div class="alert alert-danger py-2">{{ field.label.text }}: {{ error }}</div>
        {% endfor %}
    {% endfor %}
    <div class="mb-3">
        {{ form.text.label(class='form-label') }}
        {{ form.text(class='form-control', rows=2) }}
    </div>
    <div class="row">
        <div class="col-md-6 mb-3">
            {{ form.option_a.label(class='form-label') }}
            {{ form.option_a(class='form-control') }}
        </div>
        <div class="col-md-6 mb-3">
            {{ form.option_b.label(class='form-label') }}
            {{ form.option_b(class='form-control') }}
        </div>
        <div class="col-md-6 mb-3">
            {{ form.option_c.label(class='form-label') }}
            {{ form.option_c(class='form-control') }}
        </div>
        <div class="col-md-6 mb-3">
            {{ form.option_d.label(class='form-label') }}
            {{ form.option_d(class='form-control') }}
        </div>
    </div>
    <div class="mb-3">
        <label class="form-label">Correct answer</label>
        <div>
            {% for sub in form.correct_option %}
                <span class="me-3">{{ sub }} {{ sub.label.text }}</span>
            {% endfor %}
        </div>
    </div>
    <div class="mb-3">
        {{ form.time_limit.label(class='form-label') }}
        {{ form.time_limit(class='form-control', style='max-width: 200px') }}
    </div>
    {{ form.submit(class='btn btn-primary') }}
    <a href="{{ url_for('quiz.detail', set_id=question_set.id) }}" class="btn btn-outline-secondary">Cancel</a>
</form>
{% endblock %}
```

**`app/templates/quiz/detail.html`** — add an "Edit Set" link next to "+ Add Question", and per-question Edit/Delete controls inside the existing `{% for q in question_set.questions %}` loop:

```html
<a href="{{ url_for('quiz.edit_set', set_id=question_set.id) }}" class="btn btn-outline-secondary">Edit Set</a>
```
(placed alongside the existing "+ Add Question" button)

Inside the `<li>` for each question, after the existing time-limit/badges `<small>` block:
```html
<div class="mt-2">
    <a href="{{ url_for('quiz.edit_question', set_id=question_set.id, question_id=q.id) }}" class="btn btn-sm btn-outline-primary">Edit</a>
    <form method="POST" action="{{ url_for('quiz.delete_question', set_id=question_set.id, question_id=q.id) }}" class="d-inline" onsubmit="return confirm('Delete this question?');">
        <input type="hidden" name="csrf_token" value="{{ csrf_token() }}">
        <button type="submit" class="btn btn-sm btn-outline-danger">Delete</button>
    </form>
</div>
```

## Verification

- Edit a set's name/description, confirm it persists and the detail page reflects the change.
- Edit a question's text, an option, the correct answer, and the time limit; confirm each persists and re-renders correctly (including that `correct_option`'s radio button shows the right one pre-selected on the edit form's GET).
- Delete one question from a set with 3+ questions; confirm only that question is gone, the others are unaffected and still orderable, and hosting a game from the set still works end-to-end (exercises the position-gap safety check).
- Attempt to edit/delete a set or question you don't own (different `owner_id`) via direct URL and confirm a 404, not a 403 leak of existence — matching `_get_owned_set`'s existing behavior.
- Confirm `QuestionForm`'s existing `validate_correct_option` cross-field check still fires correctly on edit (e.g. editing `correct_option` to `C` while `option_c` is blank should still be rejected).
