"""Ongoing LLM-only fact-check runner — runs the calibrated judge
(FactualCorrectnessMetric) over a real QuestionSet's questions, prints a
per-question verdict + reason, reports an aggregate pass rate, and logs to
MLflow. Usable against production too (point DATABASE_URL at Render's DB).

    python -m evals.evaluate_question_set --question-set-id 42
    python -m evals.evaluate_question_set --latest 5
"""
from __future__ import annotations

import argparse

from dotenv import load_dotenv

load_dotenv()

import logging  # noqa: E402

from app import create_app  # noqa: E402
from app.models import QuestionSet  # noqa: E402
from evals.judge import NvidiaJudgeLLM  # noqa: E402
from evals.metrics import FactualCorrectnessMetric, question_to_test_case  # noqa: E402
from evals.mlflow_logging import log_fact_check  # noqa: E402

log = logging.getLogger('evals')


def fact_check_question_set(question_set_id: int, model=None) -> dict:
    qs = QuestionSet.query.get(question_set_id)
    if qs is None:
        raise ValueError(f'No QuestionSet with id={question_set_id}')

    judge_model = model or NvidiaJudgeLLM()
    metric = FactualCorrectnessMetric(model=judge_model)

    questions = qs.questions.all()
    results = []
    n_pass = 0
    total_score = 0.0

    n_errors = 0
    for question in questions:
        test_case = question_to_test_case(question)
        try:
            metric.measure(test_case)
        except Exception as exc:
            # A single malformed/truncated judge response must not kill an
            # entire multi-set batch run (evaluate_question_set.py --latest
            # N) — log distinctly, record as an error (not a silent pass or
            # fail), and keep going.
            n_errors += 1
            log.warning(f'evaluate_question_set: judge call failed for question '
                        f'#{question.id}: {exc!r}')
            results.append({
                'question_id': question.id,
                'question_text': question.text,
                'score': None,
                'passed': False,
                'reason': f'JUDGE ERROR: {exc!r}',
            })
            print(f'[ERROR] #{question.id} judge call failed: {exc!r}')
            continue
        passed = metric.score >= metric.threshold
        if passed:
            n_pass += 1
        total_score += metric.score
        results.append({
            'question_id': question.id,
            'question_text': question.text,
            'score': metric.score,
            'passed': passed,
            'reason': metric.reason,
        })
        status = 'PASS' if passed else 'FAIL'
        print(f'[{status}] #{question.id} (score={metric.score:.2f}) {question.text}')
        if not passed:
            print(f'        reason: {metric.reason}')

    n_questions = len(questions)
    n_scored = n_questions - n_errors
    pass_rate = (n_pass / n_scored) if n_scored else 0.0
    avg_score = (total_score / n_scored) if n_scored else 0.0
    if n_errors:
        print(f'({n_errors}/{n_questions} question(s) could not be judged — see [ERROR] lines above)')

    summary = {
        'question_set_id': question_set_id,
        'n_questions': n_questions,
        'n_errors': n_errors,
        'pass_rate': pass_rate,
        'avg_score': avg_score,
        'results': results,
    }

    print(f'\n=== QuestionSet {question_set_id}: {n_pass}/{n_scored} scored passed '
          f'({pass_rate:.1%}), avg score {avg_score:.2f}'
          + (f', {n_errors} judge error(s)' if n_errors else '') + ' ===')

    log_fact_check(question_set_id, pass_rate=pass_rate, avg_score=avg_score, n_questions=n_questions)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--question-set-id', type=int, default=None)
    parser.add_argument('--latest', type=int, default=None,
                         help='Fact-check the N most recently created QuestionSets instead of one id.')
    args = parser.parse_args()

    if not args.question_set_id and not args.latest:
        parser.error('Pass --question-set-id or --latest')

    app = create_app()
    with app.app_context():
        if args.question_set_id:
            fact_check_question_set(args.question_set_id)
            return

        sets = (
            QuestionSet.query.order_by(QuestionSet.created_at.desc())
            .limit(args.latest)
            .all()
        )
        if not sets:
            print('No QuestionSets found.')
            return
        judge_model = NvidiaJudgeLLM()
        for qs in sets:
            fact_check_question_set(qs.id, model=judge_model)
            print()


if __name__ == '__main__':
    main()
