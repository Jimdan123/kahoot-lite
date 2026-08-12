# Username-Only Authentication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace email+display_name+confirm-password auth with plain username+password for both signup and login.

**Architecture:** Single `User.username` column replaces `email`/`display_name`. Forms, routes, and templates are edited to match. Because the live Render deployment has an existing `users` table, the schema change is applied through the app's existing idempotent post-`create_all()` migration block in `app/__init__.py` (raw `ALTER TABLE ... IF NOT EXISTS` statements run on every boot), not a manual DB reset — this is the codebase's established pattern for schema changes that must land on both a fresh local DB and Render's live one.

**Tech Stack:** Flask, Flask-SQLAlchemy, Flask-WTF (WTForms), Flask-Login, Postgres. No new dependencies.

## Global Constraints

- Username: 3–20 characters, `^[A-Za-z0-9_]+$` only, case-insensitive (stored/looked-up lowercased) — from `docs/superpowers/specs/2026-08-12-username-auth-design.md` Decisions section.
- Signup form fields: exactly `username` + `password` — no `display_name`, no `password_confirm`.
- Login form fields: exactly `username` + `password`.
- Old accounts (rows with no `username`) are intentionally orphaned, not migrated — confirmed acceptable.
- No SQLite fallback exists in this app; all local verification runs against local Postgres per `README.md`'s Setup section (`DATABASE_URL` in `.env` already points at it).

---

### Task 1: Data model + live-schema migration

**Files:**
- Modify: `app/models.py:7-30` (the `User` class)
- Modify: `app/__init__.py:92-104` (idempotent migration block, insert before the final `db.session.commit()` on line 104)

**Interfaces:**
- Produces: `User.username` (str, unique, lowercased), replacing `User.email` and `User.display_name` everywhere downstream (Tasks 2–4 consume this).

- [ ] **Step 1: Edit the `User` model**

In `app/models.py`, replace:
```python
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    display_name = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
```
with:
```python
    username = db.Column(db.String(20), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
```

And replace the `__repr__`:
```python
    def __repr__(self):
        return f'<User {self.email}>'
```
with:
```python
    def __repr__(self):
        return f'<User {self.username}>'
```

- [ ] **Step 2: Verify the file still parses**

Run: `python -m py_compile app/models.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Extend the idempotent migration block**

In `app/__init__.py`, immediately after the existing:
```python
        db.session.execute(text(
            'ALTER TABLE user_api_keys ADD COLUMN IF NOT EXISTS model_name TEXT'
        ))
```
and before:
```python
        db.session.commit()
```
add:
```python
        # Username-only auth: `email`/`display_name` are gone from the model.
        # `username` is added nullable at the DB level (unlike the model's
        # nullable=False) because ALTER TABLE ADD COLUMN can't add a NOT NULL
        # column with no default to a table that already has rows on Render —
        # existing rows end up as harmless orphans with username IS NULL,
        # unable to log in. New signups always set it, so app-level code
        # never sees a null username.
        db.session.execute(text(
            'ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(20)'
        ))
        db.session.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)'
        ))
        db.session.execute(text(
            'ALTER TABLE users DROP COLUMN IF EXISTS email'
        ))
        db.session.execute(text(
            'ALTER TABLE users DROP COLUMN IF EXISTS display_name'
        ))
```

- [ ] **Step 4: Verify the file still parses**

Run: `python -m py_compile app/__init__.py`
Expected: no output, exit code 0.

- [ ] **Step 5: Boot the app against local Postgres and confirm the migration runs cleanly**

Run (needs local Postgres up and `.env` configured per `README.md`'s Setup section):
```bash
SECRET_KEY=dev-secret PORT=5001 timeout 5 python run.py
```
Expected: `[boot] DATABASE=postgresql` printed to stderr, no traceback. (The `timeout 5` just kills the dev server after boot succeeds — this step only checks startup, not runtime behavior.)

Then confirm the schema directly:
```bash
psql "$DATABASE_URL" -c '\d users'
```
Expected: `username` column present (`character varying(20)`), `email` and `display_name` columns absent.

- [ ] **Step 6: Commit**

```bash
git add app/models.py app/__init__.py
git commit -m "feat(auth): replace email/display_name with username on User model"
```

---

### Task 2: Auth forms

**Files:**
- Modify: `app/auth/forms.py` (entire file)

**Interfaces:**
- Consumes: `User` from `app.models` (Task 1's `User.username`).
- Produces: `LoginForm.username`, `LoginForm.password`; `SignupForm.username`, `SignupForm.password` — Task 3's routes read these field names.

- [ ] **Step 1: Rewrite `app/auth/forms.py`**

Replace the entire file with:
```python
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Length, Regexp, ValidationError
from app.models import User


class LoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])
    submit = SubmitField('Log In')


class SignupForm(FlaskForm):
    username = StringField(
        'Username',
        validators=[
            DataRequired(),
            Length(min=3, max=20),
            Regexp(r'^[A-Za-z0-9_]+$', message='Letters, numbers, and underscores only'),
        ],
    )
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6)])
    submit = SubmitField('Sign Up')

    def validate_username(self, field):
        if User.query.filter_by(username=field.data.lower()).first():
            raise ValidationError('Username already taken')
```

- [ ] **Step 2: Verify the file still parses and imports cleanly**

Run: `python -m py_compile app/auth/forms.py`
Expected: no output, exit code 0.

- [ ] **Step 3: Commit**

```bash
git add app/auth/forms.py
git commit -m "feat(auth): drop email/display_name/confirm-password from auth forms"
```

---

### Task 3: Auth routes

**Files:**
- Modify: `app/auth/routes.py:26-67` (the `signup` and `login` view functions)

**Interfaces:**
- Consumes: `LoginForm.username`/`.password`, `SignupForm.username`/`.password` (Task 2); `User.username` (Task 1).

- [ ] **Step 1: Edit `signup()`**

Replace:
```python
    form = SignupForm()
    if form.validate_on_submit():
        user = User(email=form.email.data.lower(), display_name=form.display_name.data)
        user.set_password(form.password.data)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            # Narrow race: two signups for the same email passed the
            # pre-commit uniqueness check before either committed. The DB's
            # unique constraint is the real guard; turn its failure into the
            # same friendly message instead of a 500.
            db.session.rollback()
            flash('Email already registered', 'danger')
            return render_template('auth/signup.html', form=form)
```
with:
```python
    form = SignupForm()
    if form.validate_on_submit():
        user = User(username=form.username.data.lower())
        user.set_password(form.password.data)
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            # Narrow race: two signups for the same username passed the
            # pre-commit uniqueness check before either committed. The DB's
            # unique constraint is the real guard; turn its failure into the
            # same friendly message instead of a 500.
            db.session.rollback()
            flash('Username already taken', 'danger')
            return render_template('auth/signup.html', form=form)
```

- [ ] **Step 2: Edit `login()`**

Replace:
```python
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
```
with:
```python
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data.lower()).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            next_page = request.args.get('next')
            if not _is_safe_next(next_page):
                next_page = url_for('main.index')
            return redirect(next_page)
        flash('Invalid username or password', 'danger')
```

- [ ] **Step 3: Verify the file still parses**

Run: `python -m py_compile app/auth/routes.py`
Expected: no output, exit code 0.

- [ ] **Step 4: Commit**

```bash
git add app/auth/routes.py
git commit -m "feat(auth): swap email for username in signup/login routes"
```

---

### Task 4: Templates

**Files:**
- Modify: `app/templates/auth/login.html:11-14`
- Modify: `app/templates/auth/signup.html:11-28`
- Modify: `app/templates/base.html:19`

**Interfaces:**
- Consumes: `form.username` (Task 2), `current_user.username` (Task 1).

- [ ] **Step 1: Edit `app/templates/auth/login.html`**

Replace:
```html
                    <div class="mb-3">
                        {{ form.email.label(class='form-label') }}
                        {{ form.email(class='form-control') }}
                    </div>
```
with:
```html
                    <div class="mb-3">
                        {{ form.username.label(class='form-label') }}
                        {{ form.username(class='form-control') }}
                    </div>
```

- [ ] **Step 2: Edit `app/templates/auth/signup.html`**

Replace:
```html
                    <div class="mb-3">
                        {{ form.email.label(class='form-label') }}
                        {{ form.email(class='form-control') }}
                        {% for e in form.email.errors %}<small class="text-danger">{{ e }}</small>{% endfor %}
                    </div>
                    <div class="mb-3">
                        {{ form.display_name.label(class='form-label') }}
                        {{ form.display_name(class='form-control') }}
                    </div>
                    <div class="mb-3">
                        {{ form.password.label(class='form-label') }}
                        {{ form.password(class='form-control') }}
                    </div>
                    <div class="mb-3">
                        {{ form.password_confirm.label(class='form-label') }}
                        {{ form.password_confirm(class='form-control') }}
                        {% for e in form.password_confirm.errors %}<small class="text-danger">{{ e }}</small>{% endfor %}
                    </div>
```
with:
```html
                    <div class="mb-3">
                        {{ form.username.label(class='form-label') }}
                        {{ form.username(class='form-control') }}
                        {% for e in form.username.errors %}<small class="text-danger">{{ e }}</small>{% endfor %}
                    </div>
                    <div class="mb-3">
                        {{ form.password.label(class='form-label') }}
                        {{ form.password(class='form-control') }}
                    </div>
```

- [ ] **Step 3: Edit `app/templates/base.html`**

Replace:
```html
                    <span class="navbar-text text-white me-2">{{ current_user.display_name }}</span>
```
with:
```html
                    <span class="navbar-text text-white me-2">{{ current_user.username }}</span>
```

- [ ] **Step 4: Manually verify the rendered pages**

With the server running (`SECRET_KEY=dev-secret PORT=5001 python run.py`), in another shell:
```bash
curl -s http://localhost:5001/auth/signup | grep -Eo '(name="[a-z_]+")' 
```
Expected output contains `name="username"` and `name="password"` and `name="csrf_token"` and `name="submit"` — and does **not** contain `name="email"`, `name="display_name"`, or `name="password_confirm"`.

```bash
curl -s http://localhost:5001/auth/login | grep -Eo '(name="[a-z_]+")'
```
Expected: `name="username"`, `name="password"`, `name="csrf_token"`, `name="submit"` — no `name="email"`.

- [ ] **Step 5: Commit**

```bash
git add app/templates/auth/login.html app/templates/auth/signup.html app/templates/base.html
git commit -m "feat(auth): update login/signup/navbar templates for username-only auth"
```

---

### Task 5: Update the standalone integration scripts

**Files:**
- Modify: `tests/smoke_test.py:26-43,180`
- Modify: `tests/test_security.py:31-32,46-57,60-69,114,125-141,191,205-208`
- Modify: `tests/test_impersonation.py:24-28`
- Modify: `tests/test_reconnect.py:27-31`

**Interfaces:**
- Consumes: the live `/auth/signup` and `/auth/login` HTTP endpoints from Tasks 1–4 (`username`/`password` form fields only).

- [ ] **Step 1: Edit `tests/smoke_test.py`**

Replace the `signup()` helper:
```python
def signup(session, email, password, display_name):
    token = get_csrf(session, f"{BASE}/auth/signup")
    r = session.post(
        f"{BASE}/auth/signup",
        data={
            "csrf_token": token,
            "email": email,
            "display_name": display_name,
            "password": password,
            "password_confirm": password,
            "submit": "Sign Up",
        },
        allow_redirects=False,
    )
    print(f"  signup -> HTTP {r.status_code}")
    if r.status_code not in (200, 302):
        raise RuntimeError(f"signup failed: {r.text[:400]}")
```
with:
```python
def signup(session, username, password):
    token = get_csrf(session, f"{BASE}/auth/signup")
    r = session.post(
        f"{BASE}/auth/signup",
        data={
            "csrf_token": token,
            "username": username,
            "password": password,
            "submit": "Sign Up",
        },
        allow_redirects=False,
    )
    print(f"  signup -> HTTP {r.status_code}")
    if r.status_code not in (200, 302):
        raise RuntimeError(f"signup failed: {r.text[:400]}")
```

And update the call site in `main()`:
```python
    print("\n1) Signup + login as host")
    signup(s, "alice@example.com", "password123", "Alice")
```
becomes:
```python
    print("\n1) Signup + login as host")
    signup(s, "alicehost", "password123")
```

- [ ] **Step 2: Edit `tests/test_security.py`**

Replace:
```python
HOST_EMAIL = "sec@example.com"
HOST_PW = "password123"
```
with:
```python
HOST_USERNAME = "sechost"
HOST_PW = "password123"
```

Replace the `signup()` helper:
```python
def signup(session, email, password, display_name):
    r = session.post(
        f"{BASE}/auth/signup",
        data={
            "csrf_token": get_csrf(session, f"{BASE}/auth/signup"),
            "email": email, "display_name": display_name,
            "password": password, "password_confirm": password, "submit": "Sign Up",
        },
        allow_redirects=False,
    )
    # 302 = created + auto-logged-in; 200 = validation error (e.g. already exists)
    return r.status_code
```
with:
```python
def signup(session, username, password):
    r = session.post(
        f"{BASE}/auth/signup",
        data={
            "csrf_token": get_csrf(session, f"{BASE}/auth/signup"),
            "username": username,
            "password": password, "submit": "Sign Up",
        },
        allow_redirects=False,
    )
    # 302 = created + auto-logged-in; 200 = validation error (e.g. already exists)
    return r.status_code
```

Replace the `login()` helper:
```python
def login(session, email, password, next_param=None):
    url = f"{BASE}/auth/login"
    if next_param is not None:
        url += "?next=" + next_param
    return session.post(
        url,
        data={"csrf_token": get_csrf(session, f"{BASE}/auth/login"),
              "email": email, "password": password, "submit": "Log In"},
        allow_redirects=False,
    )
```
with:
```python
def login(session, username, password, next_param=None):
    url = f"{BASE}/auth/login"
    if next_param is not None:
        url += "?next=" + next_param
    return session.post(
        url,
        data={"csrf_token": get_csrf(session, f"{BASE}/auth/login"),
              "username": username, "password": password, "submit": "Log In"},
        allow_redirects=False,
    )
```

In `check_open_redirect()`, replace:
```python
        r = login(s, HOST_EMAIL, HOST_PW, next_param=payload)
```
with:
```python
        r = login(s, HOST_USERNAME, HOST_PW, next_param=payload)
```

In `check_sql_injection()`, replace:
```python
    payloads = [
        ("' OR '1'='1", "' OR '1'='1"),
        ("admin'--", "anything"),
        ("sec@example.com' --", "wrong"),
    ]
    for email, pw in payloads:
        s = requests.Session()
        r = login(s, email, pw)
        # A successful login 302-redirects; a failure re-renders the form (200).
        print(f"  email={email!r:22} -> HTTP {r.status_code}")
        assert r.status_code == 200, f"injection may have authenticated: {email!r}"
```
with:
```python
    payloads = [
        ("' OR '1'='1", "' OR '1'='1"),
        ("admin'--", "anything"),
        ("sechost' --", "wrong"),
    ]
    for username, pw in payloads:
        s = requests.Session()
        r = login(s, username, pw)
        # A successful login 302-redirects; a failure re-renders the form (200).
        print(f"  username={username!r:22} -> HTTP {r.status_code}")
        assert r.status_code == 200, f"injection may have authenticated: {username!r}"
```

In `check_rate_limit()`, replace:
```python
        codes.append(login(s, "nobody@example.com", "wrong").status_code)
```
with:
```python
        codes.append(login(s, "nobody", "wrong").status_code)
```

In `main()`, replace:
```python
    status = signup(host_session, HOST_EMAIL, HOST_PW, "SecHost")
    if status == 200:
        # Already exists from a prior run — log in instead to get an authed session.
        login(host_session, HOST_EMAIL, HOST_PW)
```
with:
```python
    status = signup(host_session, HOST_USERNAME, HOST_PW)
    if status == 200:
        # Already exists from a prior run — log in instead to get an authed session.
        login(host_session, HOST_USERNAME, HOST_PW)
```

- [ ] **Step 3: Edit `tests/test_impersonation.py`**

Replace:
```python
    s.post(f"{BASE}/auth/signup", data={
        "csrf_token": csrf(s, f"{BASE}/auth/signup"),
        "email": "carol@example.com", "display_name": "Carol",
        "password": "password123", "password_confirm": "password123", "submit": "Sign Up",
    }, allow_redirects=False)
```
with:
```python
    s.post(f"{BASE}/auth/signup", data={
        "csrf_token": csrf(s, f"{BASE}/auth/signup"),
        "username": "carolhost",
        "password": "password123", "submit": "Sign Up",
    }, allow_redirects=False)
```

- [ ] **Step 4: Edit `tests/test_reconnect.py`**

Replace:
```python
    s.post(f"{BASE}/auth/signup", data={
        "csrf_token": csrf(s, f"{BASE}/auth/signup"),
        "email": "dave@example.com", "display_name": "Dave",
        "password": "password123", "password_confirm": "password123", "submit": "Sign Up",
    }, allow_redirects=False)
```
with:
```python
    s.post(f"{BASE}/auth/signup", data={
        "csrf_token": csrf(s, f"{BASE}/auth/signup"),
        "username": "davehost",
        "password": "password123", "submit": "Sign Up",
    }, allow_redirects=False)
```

- [ ] **Step 5: Verify all four files still parse**

Run: `python -m py_compile tests/smoke_test.py tests/test_security.py tests/test_impersonation.py tests/test_reconnect.py`
Expected: no output, exit code 0.

- [ ] **Step 6: Commit**

```bash
git add tests/smoke_test.py tests/test_security.py tests/test_impersonation.py tests/test_reconnect.py
git commit -m "test: update standalone auth scripts for username-only signup/login"
```

---

### Task 6: End-to-end verification

**Files:** none (verification only, no code changes).

**Interfaces:** none — this exercises the full stack from Tasks 1–5 together.

- [ ] **Step 1: Reset the local `users` table to a clean state**

The four scripts below create fixed usernames (`alicehost`, `sechost`, `carolhost`, `davehost`) and expect them not to already exist from a prior manual test run:
```bash
psql "$DATABASE_URL" -c "DELETE FROM users WHERE username IN ('alicehost','sechost','carolhost','davehost');"
```

- [ ] **Step 2: Run the three functional scripts against a throttle-off server**

In one shell:
```bash
RATELIMIT_ENABLED=0 SECRET_KEY=dev-secret PORT=5001 python run.py
```
In another shell:
```bash
BASE=http://localhost:5001 python tests/smoke_test.py
BASE=http://localhost:5001 python tests/test_impersonation.py
BASE=http://localhost:5001 python tests/test_reconnect.py
```
Expected: each prints `=== ALL CHECKS PASSED ===` and exits 0.

- [ ] **Step 3: Stop that server, then run the security suite against a throttle-on server**

Stop the Step 2 server (Ctrl-C). Reset the fixture row again (the security suite signs up `sechost` fresh):
```bash
psql "$DATABASE_URL" -c "DELETE FROM users WHERE username = 'sechost';"
```
In one shell:
```bash
SECRET_KEY=dev-secret PORT=5001 python run.py
```
In another shell:
```bash
BASE=http://localhost:5001 python tests/test_security.py
```
Expected: `=== ALL SECURITY CHECKS PASSED ===` and exit 0. (Per `tests/README.md`, this suite deliberately exhausts the login rate limiter as its last check — that's expected, not a failure.)

- [ ] **Step 4: Manual UI sanity check**

With any of the above servers running, open `http://localhost:5001/auth/signup` in a browser:
- Confirm the form shows only Username and Password fields.
- Sign up with a new username (e.g. `manualcheck`) and password `password123`.
- Confirm you land on the app home page and the navbar top-right shows `manualcheck`.
- Log out, then log back in at `/auth/login` with the same username/password and confirm it succeeds.
- Try signing up again with the same username `manualcheck` and confirm you see "Username already taken".
