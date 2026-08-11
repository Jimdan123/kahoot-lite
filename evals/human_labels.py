"""Tiny read/write helper for evals/human_labels.json — the human-labeled
calibration set (Phase 1). One JSON object per line, append-only. A plain
file, not a DB table/migration: this is a small one-time calibration
dataset, not app data, so it doesn't belong in app/models.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

LABELS_PATH = Path(__file__).parent / 'human_labels.json'


def load_labels() -> list[dict]:
    if not LABELS_PATH.exists():
        return []
    labels = []
    with LABELS_PATH.open() as f:
        for line in f:
            line = line.strip()
            if line:
                labels.append(json.loads(line))
    return labels


def labeled_question_ids() -> set[int]:
    return {label['question_id'] for label in load_labels()}


def append_label(*, question_id: int, question_set_id: int, human_verdict: str,
                  comment: str = '', info_gap: str = '') -> dict:
    """human_verdict: 'correct' | 'incorrect'. Callers should not persist a
    'skip' verdict — skipped questions simply stay out of the file so a
    later review pass can revisit them."""
    record = {
        'question_id': question_id,
        'question_set_id': question_set_id,
        'human_verdict': human_verdict,
        'comment': comment,
        'info_gap': info_gap,
        'reviewed_at': datetime.now(timezone.utc).isoformat(),
    }
    with LABELS_PATH.open('a') as f:
        f.write(json.dumps(record) + '\n')
    return record
