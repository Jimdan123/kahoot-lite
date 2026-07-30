"""Topic-practice track tunables — separate from the document's own two-hop
comprehension questions (see nodes/practice.py). Default used when the
upload form doesn't send a count (or sends one outside the allowed range) —
the user can otherwise pick their own value per upload, clamped to
[MIN, MAX] in routes.py.
"""
from __future__ import annotations

PRACTICE_QUESTIONS_PER_DIFFICULTY = 2     # easy, medium, hard each get this many
MIN_PRACTICE_QUESTIONS_PER_DIFFICULTY = 1
MAX_PRACTICE_QUESTIONS_PER_DIFFICULTY = 5
