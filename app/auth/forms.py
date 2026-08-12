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
            Regexp(r'^[A-Za-z0-9_]+\Z', message='Letters, numbers, and underscores only'),
        ],
    )
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6)])
    submit = SubmitField('Sign Up')

    def validate_username(self, field):
        if User.query.filter_by(username=field.data.lower()).first():
            raise ValidationError('Username already taken')
