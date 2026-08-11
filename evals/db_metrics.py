"""Cheap, non-LLM stats computed straight from Question rows. No LLM
calls — safe to run on every real quiz generation (see the routes.py
hook), unlike the fact-check metric in evaluate_question_set.py.
"""
from __future__ import annotations

from app.models import QuestionSet


def compute_question_set_metrics(question_set_id: int) -> dict:
    qs = QuestionSet.query.get(question_set_id)
    if qs is None:
        raise ValueError(f'No QuestionSet with id={question_set_id}')

    questions = qs.questions.all()
    n_questions = len(questions)
    closed_book_leak_count = sum(1 for q in questions if q.source == 'closed_book')
    difficulty_counts = {'easy': 0, 'medium': 0, 'hard': 0, 'none': 0}
    for q in questions:
        difficulty_counts[q.difficulty or 'none'] += 1

    return {
        'question_set_id': question_set_id,
        'n_questions': n_questions,
        'closed_book_leak_count': closed_book_leak_count,
        'closed_book_leak_rate': (closed_book_leak_count / n_questions) if n_questions else 0.0,
        'difficulty_counts': difficulty_counts,
    }
