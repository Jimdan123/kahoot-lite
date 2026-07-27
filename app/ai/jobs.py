"""
Very small in-process job registry for the PDF → LangGraph pipeline.

Free-tier PaaS can't hold an HTTP request open for the ~30–60s the pipeline
takes, so upload starts a background task and the browser polls this
registry for status. Kept intentionally simple — one dict, no persistence.
A restart loses in-flight jobs; that's acceptable for a class demo.

For a real deployment you'd move this to Redis / Celery / RQ.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from threading import RLock
from typing import Dict, Optional


JOB_TTL_SECONDS = 60 * 30      # 30 min after finish before the record is pruned


@dataclass
class JobState:
    job_id: str
    owner_id: int
    status: str = 'queued'                # queued | running | done | failed
    message: str = ''                     # human-readable progress line
    progress: float = 0.0                 # 0.0 .. 1.0
    question_set_id: Optional[int] = None
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None


_jobs: Dict[str, JobState] = {}
_lock = RLock()


def create(owner_id: int) -> JobState:
    with _lock:
        job = JobState(job_id=secrets.token_urlsafe(12), owner_id=owner_id)
        _jobs[job.job_id] = job
        _sweep_expired()
        return job


def get(job_id: str) -> Optional[JobState]:
    with _lock:
        return _jobs.get(job_id)


def update(job_id: str, **fields) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        for k, v in fields.items():
            setattr(job, k, v)


def mark_done(job_id: str, question_set_id: int) -> None:
    update(job_id, status='done', question_set_id=question_set_id,
           progress=1.0, message='Done.', finished_at=time.time())


def mark_failed(job_id: str, error: str) -> None:
    update(job_id, status='failed', error=error, finished_at=time.time())


def _sweep_expired() -> None:
    now = time.time()
    stale = [
        jid for jid, j in _jobs.items()
        if j.finished_at is not None and now - j.finished_at > JOB_TTL_SECONDS
    ]
    for jid in stale:
        _jobs.pop(jid, None)
