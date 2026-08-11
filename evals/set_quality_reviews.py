"""Tiny read/write helper for evals/set_quality_reviews.json — human
set-level quality reviews (uniqueness/duplication, question-type variety,
cross-question internal consistency). One JSON object per line,
append-only, same convention as human_labels.py.

This is a different axis than human_labels.json: that file holds
per-question factual-correctness verdicts (what calibrate_judge.py
calibrates FactualCorrectnessMetric against). This file holds set-level
structural/quality observations — duplicate or near-duplicate questions,
contradictory keys across questions, question-type monotony — none of
which the pipeline's own critic.py (structural well-formedness,
closed-book leakage) or the DeepEval judge (per-question correctness)
currently check. Intended to inform future pipeline changes (e.g. a
deduplication or cross-question-consistency check in critic.py), not to
feed the judge-calibration loop directly.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

REVIEWS_PATH = Path(__file__).parent / 'set_quality_reviews.json'


def load_set_reviews() -> list[dict]:
    if not REVIEWS_PATH.exists():
        return []
    reviews = []
    with REVIEWS_PATH.open() as f:
        for line in f:
            line = line.strip()
            if line:
                reviews.append(json.loads(line))
    return reviews


def append_set_review(*, question_set_id: int, overall_grade: str,
                       uniqueness: dict, dynamics: dict, internal_consistency: dict,
                       raw_review_text: str = '', recommended_fixes: list[str] | None = None,
                       revises: str | None = None) -> dict:
    """revises: reviewed_at timestamp of the review this one supersedes, if
    any — reviews are append-only (never mutated in place), so a follow-up
    correction is a new record that points back at what it revises rather
    than rewriting history."""
    record = {
        'question_set_id': question_set_id,
        'overall_grade': overall_grade,
        'uniqueness': uniqueness,
        'dynamics': dynamics,
        'internal_consistency': internal_consistency,
        'recommended_fixes': recommended_fixes or [],
        'revises': revises,
        'raw_review_text': raw_review_text,
        'reviewed_at': datetime.now(timezone.utc).isoformat(),
    }
    with REVIEWS_PATH.open('a') as f:
        f.write(json.dumps(record) + '\n')
    return record
