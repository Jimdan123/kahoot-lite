import re

from flask_wtf import FlaskForm
from wtforms import SelectField, PasswordField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, URL, ValidationError

from app.ssrf_protection import SSRFError, validate_public_https_url

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


# A custom provider's label is stored in the same `provider` column as the
# 4 known codes above (UserApiKey.provider) and reused as a literal URL
# path segment in the existing /settings/keys/<provider>/delete route — the
# charset restriction below keeps that route working unmodified (Werkzeug's
# default `string` converter rejects '/' in a path segment) and keeps
# process-wide log/cache keys elsewhere readable.
RESERVED_PROVIDER_LABELS = {code for code, _ in PROVIDER_CHOICES}
_LABEL_RE = re.compile(r'^[A-Za-z0-9 ._-]{1,50}$')


class CustomProviderForm(FlaskForm):
    """Add a fully custom, arbitrary OpenAI-compatible provider — not
    limited to the 4 known ones above. See
    app/ai/langgraph_flow/llm_utils.py's make_llm(custom_providers=...)."""
    label = StringField('Label', validators=[DataRequired(), Length(min=1, max=50)])
    base_url = StringField('Base URL', validators=[DataRequired(), URL(require_tld=True), Length(max=500)])
    model_name = StringField(
        'Model name(s) — comma-separated for a fallback chain',
        validators=[DataRequired(), Length(max=1000)],
    )
    api_key = PasswordField('API Key', validators=[DataRequired(), Length(min=1, max=500)])
    submit = SubmitField('Add Custom Provider')

    def validate_label(self, field):
        value = field.data.strip()
        if not _LABEL_RE.match(value):
            raise ValidationError('Label may only contain letters, numbers, spaces, ".", "_", "-".')
        if value.lower() in RESERVED_PROVIDER_LABELS:
            raise ValidationError(f'"{value}" is a reserved provider name — choose a different label.')
        field.data = value  # normalize (trimmed) before it reaches the route/DB

    def validate_base_url(self, field):
        # validate_public_https_url covers https-only (superseding the
        # simple prefix check this used to be) plus the SSRF checks: no
        # embedded credentials, standard port only, and the hostname must
        # not resolve to a private/loopback/link-local/reserved/multicast
        # address — see app/ssrf_protection.py's module docstring for why
        # this is checked again at pipeline-run time too, not just here.
        try:
            validate_public_https_url(field.data)
        except SSRFError as exc:
            raise ValidationError(str(exc))
