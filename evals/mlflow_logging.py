"""MLflow logging — no-ops entirely if evals.config.has_mlflow() is
false, mirroring app/ai/langgraph_flow/config/enrichment.py's
has_search_enrichment() "no key = fully skipped" convention (same idea,
independent implementation). Every entry point is wrapped in try/except
that logs a warning and never raises, since this must never break the
caller — the routes.py hook runs in the production request path.
"""
from __future__ import annotations

import logging

from evals.config import has_mlflow, mlflow_tracking_uri
from evals.db_metrics import compute_question_set_metrics

log = logging.getLogger('evals')


def _mlflow():
    import mlflow
    mlflow.set_tracking_uri(mlflow_tracking_uri())
    return mlflow


def log_question_set(question_set_id: int, extra_metrics: dict | None = None) -> None:
    """Cheap DB-derived metrics only — never runs the LLM judge. Called from
    app/ai/routes.py for every real quiz generation."""
    if not has_mlflow():
        return
    try:
        mlflow = _mlflow()
        metrics = compute_question_set_metrics(question_set_id)
        with mlflow.start_run(run_name=f'question_set_{question_set_id}'):
            mlflow.log_param('question_set_id', question_set_id)
            mlflow.log_metric('n_questions', metrics['n_questions'])
            mlflow.log_metric('closed_book_leak_count', metrics['closed_book_leak_count'])
            mlflow.log_metric('closed_book_leak_rate', metrics['closed_book_leak_rate'])
            for difficulty, count in metrics['difficulty_counts'].items():
                mlflow.log_metric(f'difficulty_{difficulty}_count', count)
            if extra_metrics:
                mlflow.log_metrics(extra_metrics)
    except Exception as exc:
        log.warning(f'log_question_set({question_set_id}): MLflow logging failed: {exc!r}')


def log_calibration(metrics: dict) -> None:
    """One judge_calibration run per calibrate_judge.py run — a permanent,
    dated record of the judge's agreement with the human baseline."""
    if not has_mlflow():
        return
    try:
        mlflow = _mlflow()
        with mlflow.start_run(run_name='judge_calibration'):
            for key in ('agreement_rate', 'false_positive_rate', 'false_negative_rate', 'n_labeled'):
                if key in metrics:
                    mlflow.log_metric(key, metrics[key])
    except Exception as exc:
        log.warning(f'log_calibration: MLflow logging failed: {exc!r}')


def log_fact_check(question_set_id: int, *, pass_rate: float, avg_score: float, n_questions: int) -> None:
    """One run per evaluate_question_set.py invocation — the LLM-judge fact
    check itself, distinct from the cheap per-generation run above."""
    if not has_mlflow():
        return
    try:
        mlflow = _mlflow()
        with mlflow.start_run(run_name=f'fact_check_question_set_{question_set_id}'):
            mlflow.log_param('question_set_id', question_set_id)
            mlflow.log_metric('pass_rate', pass_rate)
            mlflow.log_metric('avg_score', avg_score)
            mlflow.log_metric('n_questions', n_questions)
    except Exception as exc:
        log.warning(f'log_fact_check({question_set_id}): MLflow logging failed: {exc!r}')
