"""
Part 1.2 — HTTP glue around the LangGraph pipeline.

Flow:
  GET  /ai/upload                → form
  POST /ai/upload  {file, name}  → validate, save file, kick off background
                                    job, redirect to /ai/processing/<id>
  GET  /ai/processing/<job_id>   → status page that polls /ai/status/<id>
  GET  /ai/status/<job_id>       → JSON progress payload
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from flask import (
    abort, current_app, flash, jsonify, redirect, render_template,
    request, url_for,
)
from flask_login import current_user, login_required

from app.ai import ai_bp
from app.ai import jobs
from app.ai.langgraph_flow import run_pipeline
from app.extensions import limiter, socketio


MAX_PDF_BYTES = 10 * 1024 * 1024        # 10 MB
PDF_MAGIC = b'%PDF-'


@ai_bp.route('/upload', methods=['GET', 'POST'])
@login_required
@limiter.limit('5 per hour; 2 per minute', methods=['POST'])
def upload():
    if request.method == 'GET':
        from app.ai.langgraph_flow import _which_provider
        return render_template(
            'ai/upload.html',
            enabled=_which_provider() is not None,
            provider=_which_provider(),
        )

    # --- POST: validate and start ---
    file = request.files.get('pdf')
    if not file or not file.filename:
        flash('Please choose a PDF file to upload.', 'warning')
        return redirect(url_for('ai.upload'))

    # Peek at the first few bytes to reject files that aren't actually PDFs
    head = file.stream.read(len(PDF_MAGIC))
    file.stream.seek(0)
    if head != PDF_MAGIC:
        flash('That file does not look like a PDF.', 'danger')
        return redirect(url_for('ai.upload'))

    # Size check (Flask's MAX_CONTENT_LENGTH is set in config, but re-verify
    # here so we can give a friendly message instead of a 413).
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    if size == 0:
        flash('That file is empty.', 'danger')
        return redirect(url_for('ai.upload'))
    if size > MAX_PDF_BYTES:
        flash('PDFs must be 10 MB or smaller.', 'danger')
        return redirect(url_for('ai.upload'))

    # Save with a server-generated filename to defeat directory traversal
    upload_dir: Path = current_app.config['UPLOAD_FOLDER']
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f'{secrets.token_urlsafe(12)}.pdf'
    saved_path = upload_dir / safe_name
    file.save(saved_path)

    quiz_name = (request.form.get('name') or '').strip()[:200]
    quiz_description = (request.form.get('description') or '').strip()[:500]

    job = jobs.create(owner_id=current_user.id)
    app = current_app._get_current_object()

    def _worker():
        # Push a fresh app context because we're outside the request that
        # started this task; the SQLAlchemy session in _save() needs it.
        with app.app_context():
            try:
                def progress(message, fraction):
                    jobs.update(job.job_id, status='running',
                                message=message, progress=fraction)
                qs_id = run_pipeline(
                    pdf_path=str(saved_path),
                    owner_id=job.owner_id,
                    quiz_name=quiz_name,
                    quiz_description=quiz_description,
                    progress_cb=progress,
                )
                jobs.mark_done(job.job_id, qs_id)
            except Exception as exc:
                jobs.mark_failed(job.job_id, str(exc))
            finally:
                try:
                    saved_path.unlink(missing_ok=True)
                except OSError:
                    pass

    socketio.start_background_task(_worker)
    return redirect(url_for('ai.processing', job_id=job.job_id))


@ai_bp.route('/processing/<job_id>')
@login_required
def processing(job_id):
    job = jobs.get(job_id)
    if not job or job.owner_id != current_user.id:
        abort(404)
    return render_template('ai/processing.html', job=job)


@ai_bp.route('/status/<job_id>')
@login_required
def status(job_id):
    job = jobs.get(job_id)
    if not job or job.owner_id != current_user.id:
        abort(404)
    return jsonify({
        'status': job.status,
        'message': job.message,
        'progress': job.progress,
        'question_set_id': job.question_set_id,
        'error': job.error,
        'redirect': (url_for('quiz.detail', set_id=job.question_set_id)
                     if job.question_set_id else None),
    })
