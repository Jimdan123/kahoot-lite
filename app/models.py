from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import db, login_manager


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    display_name = db.Column(db.String(80))
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
        return f'<User {self.email}>'


class QuestionSet(db.Model):
    __tablename__ = 'question_sets'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    owner_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
