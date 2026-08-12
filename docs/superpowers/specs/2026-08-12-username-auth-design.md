# Username-only authentication

## Context

Host signup/login currently requires an email address (validated with WTForms' `Email()` validator) plus a separate `display_name` field, plus password. The user wants authentication simplified to just username + password — no email anywhere in the auth flow. Investigation confirmed `email` is used *only* for login/signup/`User.__repr__` — no password-reset flow, no email-sending feature, nothing else in the codebase depends on it. `display_name` is used only for the navbar greeting.

Two scope decisions were confirmed with the user before this design:
- Signup becomes username + password only — no separate display-name field; the username itself is shown in the navbar.
- The app's already-deployed Render Postgres database does not need existing accounts preserved — a clean break (old accounts stop working, no backfill) is acceptable.

## 1. Data model (`app/models.py`)

Replace `email`/`display_name` on `User` with a single column:
```python
username = db.Column(db.String(30), unique=True, nullable=False, index=True)
```
- Normalized to lowercase on both signup and login (same pattern the current code already applies to email), so lookups/uniqueness are case-insensitive without needing a case-insensitive DB index.
- `User.__repr__` switches from `self.email` to `self.username`.
- The navbar greeting (`app/templates/base.html`) switches from `current_user.display_name` to `current_user.username`.

## 2. Forms (`app/auth/forms.py`)

- `LoginForm`: `username` (`DataRequired`) + `password` (`DataRequired`). Drop the `Email` validator import.
- `SignupForm`: `username` (`DataRequired`, `Length(min=3, max=30)`, regex-validated to letters/numbers/underscore only) + `password` (`DataRequired`, `Length(min=6)`) + `password_confirm` (`EqualTo('password')`). Drop `display_name` entirely.
- `validate_username` replaces `validate_email`: rejects if a `User` with that (lowercased) username already exists.

## 3. Routes (`app/auth/routes.py`)

`signup()`/`login()` swap every `email` reference for `username` (lowercased before storage/lookup), including the `IntegrityError` race-condition fallback message, which becomes "Username already taken".

## 4. Templates

- `app/templates/auth/login.html`: the `form.email` field block becomes `form.username`.
- `app/templates/auth/signup.html`: the `form.email` field block becomes `form.username`; the `form.display_name` field block is removed entirely (username + password + confirm only).

## 5. Database migration for the live Render table

`db.create_all()` (already running at boot in `app/__init__.py`) only creates tables that don't exist yet — it never alters an existing one, confirmed by the comment already in that file explaining the existing `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` patches for `source`/`total_tokens`. The live `users` table on Render already exists with the old columns, so the model change alone does nothing there without an explicit migration.

Since a clean break is acceptable, add to the same idempotent-migration block in `app/__init__.py`:
```sql
ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(30);
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username);
ALTER TABLE users DROP COLUMN IF EXISTS email;
ALTER TABLE users DROP COLUMN IF EXISTS display_name;
```
- `CREATE UNIQUE INDEX IF NOT EXISTS` is the idempotent equivalent of `ADD CONSTRAINT IF NOT EXISTS`, which Postgres doesn't support directly.
- Dropping `email`/`display_name` (rather than leaving them vestigial) is necessary, not just tidiness: the existing `email` column is `NOT NULL` at the DB level, so leaving it in place would make every new signup fail (the new code no longer sets it) even though the model doesn't reference it.
- Net effect: any old rows survive as harmless orphaned `(id, password_hash, created_at)` records with `username IS NULL` — unable to log in, referenced by nothing. Their `question_sets`/`questions` (FK'd via `owner_id`, not touched by this migration) are untouched. On a fresh/local DB, `db.create_all()` builds the table correctly from the model directly, so this block is a no-op there beyond the (harmless, already-idempotent) `ADD COLUMN IF NOT EXISTS`.

## 6. Test scripts

`tests/smoke_test.py`, `tests/test_security.py`, `tests/test_impersonation.py`, `tests/test_reconnect.py` currently sign up via `email`/`display_name` form fields (standalone HTTP scripts driving a real running server — see `tests/README.md`). Update their signup/login payloads to use `username` so they keep passing. This is a direct, necessary consequence of the field rename, not unrelated scope.

## Verification

- Fresh local DB: sign up with a new username + password, confirm login works, confirm the navbar shows the username, confirm duplicate-username signup is rejected with a friendly message.
- Run the four updated standalone test scripts against a locally running server (`tests/README.md`'s existing invocation pattern) and confirm they still pass.
- After deploying, confirm the migration block runs cleanly against the live Render Postgres table (check boot logs) and that a fresh signup on the deployed app works end-to-end.
