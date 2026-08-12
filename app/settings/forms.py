from flask_wtf import FlaskForm
from wtforms import SelectField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Length

# WTForms' SelectField rejects any value outside `choices` server-side by
# default (validate_choice) — no custom validator needed to keep a bogus
# provider out of the DB even if the client <select> is tampered with.
PROVIDER_CHOICES = [
    ('groq', 'Groq'),
    ('nvidia', 'NVIDIA NIM'),
    ('openrouter', 'OpenRouter'),
    ('deepseek', 'DeepSeek'),
]


class ApiKeyForm(FlaskForm):
    provider = SelectField('Provider', choices=PROVIDER_CHOICES, validators=[DataRequired()])
    api_key = PasswordField('API Key', validators=[DataRequired(), Length(min=1, max=500)])
    submit = SubmitField('Save Key')
