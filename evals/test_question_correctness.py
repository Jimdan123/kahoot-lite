"""Regression suite: asserts the LLM judge's fact-check pass rate for a
real QuestionSet stays at/above evals.config.MIN_PASS_RATE. Rerun after
pipeline/prompt changes to catch regressions — compare the result against
the calibration baseline (evals/calibration_report.json) and prior MLflow
runs rather than trusting a single pass/fail in isolation.

By default tests the most recently created QuestionSet; override with
EVALS_TEST_QUESTION_SET_ID to pin a specific one (e.g. a known-good
QuestionSet kept around specifically for this suite). Needs
OPENROUTER_JUDGE_API_KEY (or OPENROUTER_API_KEY) — this suite calls the
real judge, it's not free/instant.

    pytest evals/test_question_correctness.py
"""
from __future__ import annotations

import os

import pytest

from app.models import QuestionSet
from evals.config import MIN_PASS_RATE
from evals.evaluate_question_set import fact_check_question_set


@pytest.fixture
def question_set_id(app):
    override = os.environ.get('EVALS_TEST_QUESTION_SET_ID')
    if override:
        return int(override)
    qs = QuestionSet.query.order_by(QuestionSet.created_at.desc()).first()
    if qs is None:
        pytest.skip('No QuestionSet in the database to test against.')
    return qs.id


def test_fact_check_pass_rate(app, question_set_id):
    summary = fact_check_question_set(question_set_id)
    assert summary['n_questions'] > 0, 'QuestionSet has no questions to check.'
    assert summary['pass_rate'] >= MIN_PASS_RATE, (
        f"Fact-check pass rate {summary['pass_rate']:.1%} for QuestionSet "
        f"{question_set_id} fell below the {MIN_PASS_RATE:.0%} threshold. "
        f"See the printed per-question reasons above for what failed."
    )
