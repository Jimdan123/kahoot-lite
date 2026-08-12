from flask import render_template, redirect, url_for, flash
from flask_login import login_required, current_user
from app.settings import settings_bp
from app.settings.forms import ApiKeyForm, CustomProviderForm, PROVIDER_CHOICES
from app.extensions import db
from app.models import UserApiKey

_LABELS = dict(PROVIDER_CHOICES)


def _settings_context(form=None, custom_form=None):
    """Builds the saved_keys/custom_keys query pair shared by every render
    path on this page (index() GET, and the invalid-form fallthrough in
    both index() and add_custom_provider()) — factored out so that split
    query doesn't get triplicated across those three spots."""
    saved_keys = (
        UserApiKey.query.filter_by(user_id=current_user.id, is_custom=False)
        .order_by(UserApiKey.provider)
        .all()
    )
    custom_keys = (
        UserApiKey.query.filter_by(user_id=current_user.id, is_custom=True)
        .order_by(UserApiKey.provider)
        .all()
    )
    return dict(
        form=form or ApiKeyForm(),
        custom_form=custom_form or CustomProviderForm(),
        saved_keys=saved_keys,
        custom_keys=custom_keys,
        provider_labels=_LABELS,
    )


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

    return render_template('settings/index.html', **_settings_context(form=form))


@settings_bp.route('/custom-provider/add', methods=['POST'])
@login_required
def add_custom_provider():
    custom_form = CustomProviderForm()
    if custom_form.validate_on_submit():
        label = custom_form.label.data
        row = (
            UserApiKey.query
            .filter_by(user_id=current_user.id, provider=label, is_custom=True)
            .first()
        )
        is_new = row is None
        row = row or UserApiKey(user_id=current_user.id, provider=label, is_custom=True)
        row.base_url = custom_form.base_url.data
        row.model_name = custom_form.model_name.data
        row.set_key(custom_form.api_key.data)
        if is_new:
            db.session.add(row)
        db.session.commit()
        flash(f'{"Saved" if is_new else "Updated"} your "{label}" custom provider.', 'success')
        return redirect(url_for('settings.index'))

    # Validation failed: re-render (not redirect) so field errors survive —
    # same fall-through-on-invalid shape as index() above.
    return render_template('settings/index.html', **_settings_context(custom_form=custom_form))


@settings_bp.route('/keys/<provider>/delete', methods=['POST'])
@login_required
def delete_key(provider):
    # Matches purely on (user_id, provider) — unambiguous for both known
    # and custom rows, since CustomProviderForm.validate_label guarantees
    # no custom label can ever collide with one of the 4 reserved codes.
    # Works unchanged for a custom entry's label.
    row = UserApiKey.query.filter_by(user_id=current_user.id, provider=provider).first()
    if row:
        db.session.delete(row)
        db.session.commit()
        flash(f'Removed your {_LABELS.get(provider, provider)} key.', 'info')
    return redirect(url_for('settings.index'))
