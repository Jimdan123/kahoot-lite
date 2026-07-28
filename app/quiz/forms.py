from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SubmitField, RadioField, IntegerField
from wtforms.validators import DataRequired, Length, NumberRange, Optional, ValidationError


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

    def validate_correct_option(self, field):
        # correct_option's choices are always A-D regardless of whether C/D
        # were filled in — without this, a question can be saved pointing at
        # an option nobody will ever see a button for, making it unwinnable.
        option_field = {
            'A': self.option_a, 'B': self.option_b,
            'C': self.option_c, 'D': self.option_d,
        }[field.data]
        if not (option_field.data or '').strip():
            raise ValidationError(f"Correct answer is set to {field.data}, but Option {field.data} is empty.")
