from flask import render_template
from flask_login import login_required
from app.ai import ai_bp


@ai_bp.route('/upload')
@login_required
def upload():
    """Stub for Part 1.2 — LangGraph-driven PDF → question-set generation."""
    return render_template('ai/upload.html')
