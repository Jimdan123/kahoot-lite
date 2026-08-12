# Username-only authentication

## Context

Host signup/login currently requires an email address (validated with WTForms' `Email()` validator), a `display_name` field, a password, and a confirm-password field. The user wants authentication simplified to just username + password for both signup and login — no email, no display name, no confirm-password field anywhere in the auth flow.

Investigation confirmed:
- `email` is used only for login/signup/`User.__repr__` — no password-reset flow, no email-sending feature, nothing else in the codebase depends on it.
- `display_name` is used only for the navbar greeting (`app/templates/base.html:19`) — nowhere else (e.g. `QuestionSet.owner` is never rendered).
- The app is deployed on Render with a live Postgres database (`render.yaml`, README's deploy section) and already has an established idempotent-migration convention in `app/__init__.py` for exactly this situation — a comment there explains it exists because "on Render, which has real data" `db.create_all()` alone won't alter an existing table.

This supersedes an earlier version of this same spec (committed as `c5e8cdb`, never implemented) that used a 30-char username limit and kept a confirm-password field. Both were revisited and changed in this session — see decisions below.

## Decisions

- Replace `email` entirely with `username` as the sole identity/login field (not added alongside email).
- Signup becomes exactly username + password — `display_name` and `password_confirm` are both dropped, matching login field-for-field.
- Username format: 3–20 characters, letters/numbers/underscore only (`^[A-Za-z0-9_]+$`).
- Case-insensitive uniqueness: normalized to lowercase on both signup and login, same pattern the current code already applies to email.
- The navbar greeting shows `current_user.username` instead of `current_user.display_name`.
- Old accounts are not preserved — a clean break. Confirmed acceptable by the user; unavoidable in any case since a username can't be safely auto-derived from an email at the DB level.

## 1. Data model (`app/models.py`)

Replace `email`/`display_name` on `User` with a single column:
```python
username = db.Column(db.String(20), unique=True, nullable=False, index=True)
```
`User.__repr__` switches from `self.email` to `self.username`.

## 2. Forms (`app/auth/forms.py`)

- `LoginForm`: `username` (`DataRequired`) + `password` (`DataRequired`). Drop the `Email` validator/import.
- `SignupForm`: `username` (`DataRequired`, `Length(min=3, max=20)`, `Regexp(r'^[A-Za-z0-9_]+$')`) + `password` (`DataRequired`, `Length(min=6)`). Drop `display_name` and `password_confirm` (and the now-unused `EqualTo` import).
- `validate_username` replaces `validate_email`: rejects if a `User` with that (lowercased) username already exists.

## 3. Routes (`app/auth/routes.py`)

`signup()`/`login()` swap every `email` reference for `username` (lowercased before storage/lookup), including the `IntegrityError` race-condition fallback message, which becomes "Username already taken", and the login failure flash, which becomes "Invalid username or password".

## 4. Templates

- `app/templates/auth/login.html`: the `form.email` field block becomes `form.username`.
- `app/templates/auth/signup.html`: the `form.email` field block becomes `form.username`; the `form.display_name` and `form.password_confirm` field blocks are removed entirely — just username + password.
- `app/templates/base.html:19`: navbar greeting switches from `current_user.display_name` to `current_user.username`.

## 5. Migration (`app/__init__.py`)

Extend the existing idempotent post-`create_all()` block (same pattern already used there for `source`, `total_tokens`, and the `user_api_keys` columns) with:
```sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(20);
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username);
ALTER TABLE users DROP COLUMN IF EXISTS email;
ALTER TABLE users DROP COLUMN IF EXISTS display_name;
```
- `CREATE UNIQUE INDEX IF NOT EXISTS` is the idempotent equivalent of `ADD CONSTRAINT IF NOT EXISTS`, which Postgres doesn't support directly.
- Dropping `email`/`display_name` is necessary, not just tidiness: `email` is `NOT NULL` at the DB level, so leaving it in place would make every new signup fail (the new code never sets it) even though the model no longer references it.
- Net effect on Render: any old rows survive as harmless orphaned `(id, password_hash, created_at)` records with `username IS NULL` — unable to log in, referenced by nothing. Their `question_sets`/`questions` (FK'd via `owner_id`, untouched by this migration) are unaffected. On a fresh local DB, `db.create_all()` builds the table correctly from the model directly, so this block is a no-op there beyond the (harmless, idempotent) `ADD COLUMN`.
- This runs automatically on next boot in both environments — no manual DB reset step needed, unlike a plain local drop/recreate.

## 6. Test scripts

`tests/smoke_test.py`, `tests/test_security.py`, `tests/test_impersonation.py`, `tests/test_reconnect.py` are standalone HTTP scripts that drive a real running server and currently sign up via `email`/`display_name`/`password_confirm` form fields. Update their `signup()`/`login()` helpers and seed values to use `username`/`password` only. Direct, necessary consequence of the field rename, not unrelated scope.

## Verification

- Fresh local DB: sign up with a new username + password, confirm login works, confirm the navbar shows the username, confirm a duplicate-username signup is rejected with a friendly message, confirm a too-short/invalid-character username is rejected client-and-server-side.
- Run the four updated standalone test scripts against a locally running server (`tests/README.md`'s existing invocation pattern) and confirm they still pass.
- After deploying, confirm the migration block runs cleanly against the live Render Postgres table (check boot logs) and that a fresh signup on the deployed app works end-to-end.
