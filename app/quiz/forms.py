from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SubmitField, RadioField, IntegerField
from wtforms.validators import DataRequired, Length, NumberRange, Optional


class QuestionSetForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired(), Length(max=200)])
    description = TextAreaField('Description', validators=[Optional(), Length(max=500)])
    submit = SubmitField('Save')


class QuestionForm(FlaskForm):
    text = TextAreaField('Question', validators=[DataRequired()])
    option_a = StringField('Option A', validators=[DataRequired()])
    option_b = StringField('Option B', validators=[DataRequired()])
    option_c = StringField('Option C (optional)', validators=[Optional()])
    option_d = StringField('Option D (optional)', validators=[Optional()])
    correct_option = RadioField(
        'Correct answer',
        choices=[('A', 'A'), ('B', 'B'), ('C', 'C'), ('D', 'D')],
        validators=[DataRequired()],
    )
    time_limit = IntegerField('Time limit (seconds)', default=20, validators=[NumberRange(min=5, max=120)])
    submit = SubmitField('Save Question')
