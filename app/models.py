from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import db, login_manager


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(20), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    question_sets = db.relationship(
        'QuestionSet',
        backref='owner',
        lazy='dynamic',
        cascade='all, delete-orphan',
    )

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.username}>'


class UserApiKey(db.Model):
    """A user's own BYOK provider API key — lets their quiz-generation runs
    draw from their own quota instead of the server's shared one (tried
    first, server's own key(s) stay a fallback — see
    app/ai/langgraph_flow/llm_utils.py's make_llm()). The unique constraint
    gives upsert semantics: re-adding a key for a provider you've already
    saved updates it in place rather than creating a duplicate row."""
    __tablename__ = 'user_api_keys'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'provider', name='uq_user_api_keys_user_provider'),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    # One of 'groq'/'nvidia'/'openrouter'/'deepseek' for a known provider, or
    # the user's own trimmed label for a custom one (is_custom=True) — never
    # ambiguous, since CustomProviderForm.validate_label rejects the 4
    # reserved names. String(50), not 20: a real custom label needs room
    # ("Together AI Free Tier" alone is 22 chars).
    provider = db.Column(db.String(50), nullable=False)
    # True for a user-defined arbitrary OpenAI-compatible endpoint (base_url/
    # model_name set) rather than one of the 4 hardcoded providers (whose
    # base_url/model chain live in app/ai/langgraph_flow/config/providers.py
    # instead). Explicit column, never inferred from base_url being set, so
    # every reader has one trustworthy discriminator.
    is_custom = db.Column(db.Boolean, nullable=False, default=False, server_default='false')
    base_url = db.Column(db.String(500))    # set only when is_custom
    model_name = db.Column(db.Text)         # comma-separated model list, set only when is_custom
    encrypted_key = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = db.relationship(
        'User', backref=db.backref('api_keys', cascade='all, delete-orphan', lazy='dynamic'))

    def set_key(self, plaintext: str) -> None:
        from app.crypto_utils import encrypt
        self.encrypted_key = encrypt(plaintext)

    def get_key(self) -> str:
        from app.crypto_utils import decrypt
        return decrypt(self.encrypted_key)

    def __repr__(self):
        return f'<UserApiKey {self.provider} for user {self.user_id}>'


class QuestionSet(db.Model):
    __tablename__ = 'question_sets'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    owner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Total tokens (prompt + completion) the LangGraph pipeline used to
    # generate this set — see PipelineState['token_usage'] and
    # nodes/save.py. Null for manually-created sets and any set generated
    # before this field existed.
    total_tokens = db.Column(db.Integer)

    questions = db.relationship(
        'Question',
        backref='question_set',
        lazy='dynamic',
        cascade='all, delete-orphan',
        order_by='Question.position',
    )

    def __repr__(self):
        return f'<QuestionSet {self.name}>'


class Question(db.Model):
    __tablename__ = 'questions'

    id = db.Column(db.Integer, primary_key=True)
    question_set_id = db.Column(db.Integer, db.ForeignKey('question_sets.id'), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    text = db.Column(db.Text, nullable=False)
    option_a = db.Column(db.String(500), nullable=False)
    option_b = db.Column(db.String(500), nullable=False)
    option_c = db.Column(db.String(500))
    option_d = db.Column(db.String(500))
    correct_option = db.Column(db.String(1), nullable=False)
    time_limit = db.Column(db.Integer, default=20)
    difficulty = db.Column(db.String(10))  # 'easy' | 'medium' | 'hard'; null for manually-added questions
    # 'closed_book' for a document-grounded question the pipeline kept despite
    # it also being answerable without the PDF (see critic.py's quality_check —
    # a closed-book leak used to be an automatic reject; now it's a label
    # instead of a discard). Null for everything else, including practice
    # questions, which are closed-book by design and don't need the tag.
    source = db.Column(db.String(20))

    def options(self):
        return {
            'A': self.option_a,
            'B': self.option_b,
            'C': self.option_c,
            'D': self.option_d,
        }

    def __repr__(self):
        return f'<Question {self.text[:30]}>'


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))
