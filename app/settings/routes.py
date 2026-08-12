from flask import render_template, redirect, url_for, flash
from flask_login import login_required, current_user
from app.settings import settings_bp
from app.settings.forms import ApiKeyForm, PROVIDER_CHOICES
from app.extensions import db
from app.models import UserApiKey

_LABELS = dict(PROVIDER_CHOICES)


@settings_bp.route('/', methods=['GET', 'POST'])
@login_required
def index():
    form = ApiKeyForm()
    if form.validate_on_submit():
        provider = form.provider.data
        row = UserApiKey.query.filter_by(user_id=current_user.id, provider=provider).first()
        is_new = row is None
        row = row or UserApiKey(user_id=current_user.id, provider=provider)
        row.set_key(form.api_key.data)
        if is_new:
            db.session.add(row)
        db.session.commit()
        flash(f'{"Saved" if is_new else "Updated"} your {_LABELS[provider]} key.', 'success')
        return redirect(url_for('settings.index'))

    saved_keys = (
        UserApiKey.query.filter_by(user_id=current_user.id)
        .order_by(UserApiKey.provider)
        .all()
    )
    return render_template(
        'settings/index.html', form=form, saved_keys=saved_keys, provider_labels=_LABELS,
    )


@settings_bp.route('/keys/<provider>/delete', methods=['POST'])
@login_required
def delete_key(provider):
    row = UserApiKey.query.filter_by(user_id=current_user.id, provider=provider).first()
    if row:
        db.session.delete(row)
        db.session.commit()
        flash(f'Removed your {_LABELS.get(provider, provider)} key.', 'info')
    return redirect(url_for('settings.index'))
