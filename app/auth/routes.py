from flask import render_template, redirect, url_for, flash, request
from flask_login import login_user, logout_user, login_required, current_user
from app.auth import auth_bp
from app.auth.forms import LoginForm, SignupForm
from app.extensions import db
from app.models import User


def _is_safe_next(target):
    """
    Reject anything but a strict host-relative path.

    A single leading '/' is required; '//foo' and '/\\foo' are rejected because
    browsers can resolve them to a different origin. This is stricter than a
    netloc check because urlsplit reports an empty netloc for edge cases like
    '////evil.com' and 'https:/evil.com' that browsers still resolve off-origin.
    """
    if not target or not target.startswith('/'):
        return False
    if target.startswith('//') or target.startswith('/\\'):
        return False
    return True


@auth_bp.route('/signup', methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    form = SignupForm()
    if form.validate_on_submit():
        user = User(email=form.email.data.lower(), display_name=form.display_name.data)
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        login_user(user)
        flash('Welcome! Your account is ready.', 'success')
        return redirect(url_for('main.index'))
    return render_template('auth/signup.html', form=form)


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.lower()).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            next_page = request.args.get('next')
            if not _is_safe_next(next_page):
                next_page = url_for('main.index')
            return redirect(next_page)
        flash('Invalid email or password', 'danger')
    return render_template('auth/login.html', form=form)


@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    flash('Logged out.', 'info')
    return redirect(url_for('main.index'))
