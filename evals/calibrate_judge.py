"""Runs the LLM judge over every human-labeled question (evals/human_labels.json,
produced by evals/human_review.py) and reports its agreement with that
human baseline. Run this — and read the disagreements — before trusting
the judge for unsupervised ongoing checks in evaluate_question_set.py.

    python -m evals.calibrate_judge
    python -m evals.calibrate_judge --limit 6   # score only the first 6 labels
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402
from app.models import Question  # noqa: E402
from evals.human_labels import load_labels  # noqa: E402
from evals.judge import NvidiaJudgeLLM  # noqa: E402
from evals.metrics import FactualCorrectnessMetric, question_to_test_case  # noqa: E402
from evals.mlflow_logging import log_calibration  # noqa: E402

REPORT_PATH = Path(__file__).parent / 'calibration_report.json'


def _judge_verdict(score: float, threshold: float) -> str:
    return 'correct' if score >= threshold else 'incorrect'


def run_calibration(limit: int | None = None) -> dict:
    labels = load_labels()
    if not labels:
        print('No human labels found — run `python -m evals.human_review` first.')
        return {}
    if limit is not None:
        labels = labels[:limit]
        print(f'--limit {limit}: scoring the first {len(labels)} of the accumulated labels.')

    judge_model = NvidiaJudgeLLM()
    metric = FactualCorrectnessMetric(model=judge_model)

    results = []
    n_agree = 0
    n_false_positive = 0   # judge says correct, human says incorrect — the dangerous case
    n_false_negative = 0   # judge says incorrect, human says correct — judge too harsh
    n_scored = 0

    app = create_app()
    with app.app_context():
        for label in labels:
            if label['human_verdict'] not in ('correct', 'incorrect'):
                continue  # 'skip' labels never get written, but be defensive

            question = Question.query.get(label['question_id'])
            if question is None:
                print(f"Skipping question_id={label['question_id']} — no longer in DB.")
                continue

            test_case = question_to_test_case(question)
            metric.measure(test_case)
            judge_verdict = _judge_verdict(metric.score, metric.threshold)
            human_verdict = label['human_verdict']

            agree = judge_verdict == human_verdict
            n_scored += 1
            if agree:
                n_agree += 1
            elif judge_verdict == 'correct' and human_verdict == 'incorrect':
                n_false_positive += 1
            elif judge_verdict == 'incorrect' and human_verdict == 'correct':
                n_false_negative += 1

            results.append({
                'question_id': question.id,
                'question_text': question.text,
                'human_verdict': human_verdict,
                'human_comment': label.get('comment', ''),
                'human_info_gap': label.get('info_gap', ''),
                'judge_verdict': judge_verdict,
                'judge_score': metric.score,
                'judge_reason': metric.reason,
                'agree': agree,
            })

            if not agree:
                print(f'\nDISAGREEMENT on question #{question.id}: {question.text}')
                print(f"  Human: {human_verdict}  |  comment: {label.get('comment', '')}"
                      f"  |  info_gap: {label.get('info_gap', '')}")
                print(f'  Judge: {judge_verdict} (score={metric.score:.2f})  |  reason: {metric.reason}')

    if n_scored == 0:
        print('No labeled questions could be matched to DB rows.')
        return {}

    summary = {
        'n_labeled': n_scored,
        'agreement_rate': n_agree / n_scored,
        'false_positive_rate': n_false_positive / n_scored,
        'false_negative_rate': n_false_negative / n_scored,
    }

    REPORT_PATH.write_text(json.dumps({'summary': summary, 'results': results}, indent=2))

    print('\n=== Calibration summary ===')
    print(f"n_labeled:            {summary['n_labeled']}")
    print(f"agreement_rate:       {summary['agreement_rate']:.1%}")
    print(f"false_positive_rate:  {summary['false_positive_rate']:.1%}  "
          f"(judge said correct, human said incorrect — the dangerous case)")
    print(f"false_negative_rate:  {summary['false_negative_rate']:.1%}  (judge too harsh)")
    print(f'Full audit trail written to {REPORT_PATH}')

    log_calibration(summary)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--limit', type=int, default=None,
                         help='Score only the first N accumulated labels (e.g. for a '
                              'controlled/cheap demo run) instead of all of them.')
    args = parser.parse_args()
    run_calibration(limit=args.limit)
