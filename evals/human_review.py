"""Interactive CLI — a human reviews real generated questions and labels
them correct/incorrect. This is the ground truth calibrate_judge.py
measures the LLM judge against, before the judge is trusted for
unsupervised ongoing checks (evaluate_question_set.py).

Usage:
    python -m evals.human_review --latest 20
    python -m evals.human_review --question-set-id 42
    python -m evals.human_review --question-set-ids 41,42,43,44,45,46,47
"""
from __future__ import annotations

import argparse

from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402
from app.models import QuestionSet  # noqa: E402
from evals.human_labels import append_label, labeled_question_ids  # noqa: E402


def _question_sets(args):
    if args.question_set_ids is not None:
        ids = [int(x) for x in args.question_set_ids.split(',') if x.strip()]
        sets = [QuestionSet.query.get(i) for i in ids]
        return [qs for qs in sets if qs is not None]
    if args.question_set_id is not None:
        qs = QuestionSet.query.get(args.question_set_id)
        return [qs] if qs else []
    return (
        QuestionSet.query.order_by(QuestionSet.created_at.asc())
        .limit(args.latest)
        .all()
    )


def _prompt_verdict() -> str:
    while True:
        raw = input('Verdict [c]orrect / [i]ncorrect / [s]kip: ').strip().lower()
        if raw in ('c', 'correct'):
            return 'correct'
        if raw in ('i', 'incorrect'):
            return 'incorrect'
        if raw in ('s', 'skip', ''):
            return 'skip'
        print("Please enter 'c', 'i', or 's'.")


def review_question(question) -> bool:
    """Returns True if a label was recorded, False if skipped."""
    opts = question.options()
    print()
    print(f'--- Question #{question.id} (set {question.question_set_id}) ---')
    print(question.text)
    for letter in ('A', 'B', 'C', 'D'):
        if not opts[letter]:
            continue
        marker = '*' if letter == question.correct_option else ' '
        print(f'  {marker} {letter}. {opts[letter]}')
    print(f'Labeled correct answer: {question.correct_option}')

    verdict = _prompt_verdict()
    if verdict == 'skip':
        return False

    comment = input('Comment (optional, Enter to skip): ').strip()
    info_gap = input(
        'Info gap (optional) — specifically what information was missing '
        'or ambiguous that made this wrong (Enter to skip): '
    ).strip()
    append_label(
        question_id=question.id,
        question_set_id=question.question_set_id,
        human_verdict=verdict,
        comment=comment,
        info_gap=info_gap,
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--latest', type=int, default=20,
                         help='Review questions from the N earliest QuestionSets (default: 20).')
    parser.add_argument('--question-set-id', type=int, default=None,
                         help='Review only this QuestionSet instead of --latest.')
    parser.add_argument('--question-set-ids', type=str, default=None,
                         help='Comma-separated QuestionSet ids to review, in order '
                              '(e.g. a specific calibration batch that is not the '
                              'N earliest sets by created_at). Overrides --latest.')
    args = parser.parse_args()

    app = create_app()
    with app.app_context():
        already_labeled = labeled_question_ids()
        sets = _question_sets(args)
        if not sets:
            print('No matching QuestionSets found.')
            return

        reviewed = 0
        for qs in sets:
            for question in qs.questions:
                if question.id in already_labeled:
                    continue
                if review_question(question):
                    reviewed += 1

        print(f'\nDone. Recorded {reviewed} new label(s) in evals/human_labels.json.')


if __name__ == '__main__':
    main()
