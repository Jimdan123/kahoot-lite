"""FactualCorrectnessMetric — an independent LLM-judge fact-check of a
generated question's correctness. The judge sees only the question, its
four options, and which one is labeled correct — never the source
document — and decides using its own general knowledge whether the label
is actually right and the distractors are actually wrong.

Calibrated against a human baseline in calibrate_judge.py before being
trusted for ongoing checks in evaluate_question_set.py.
"""
from __future__ import annotations

from deepeval.metrics import GEval
from deepeval.test_case import LLMTestCase, SingleTurnParams

from evals.config import JUDGE_PASS_THRESHOLD
from evals.judge import NvidiaJudgeLLM

# Every verdict must come with the judge's stated basis, not just a bare
# accept/reject — GEval's own `reason` output is required to name the
# specific fact(s) relied on, so a human reviewer can check the judge's
# work instead of trusting a bare score. See calibrate_judge.py, which
# pairs this reason against the human's info_gap note for every
# disagreement.
#
# Deliberately uses ONLY SingleTurnParams.INPUT — no actual_output. GEval's
# normal use case is grading a *given* candidate output against a rubric;
# an earlier version of this metric put a one-line stub
# ("Option B is claimed correct") in actual_output and asked the judge to
# fact-check it, but that framing made the judge critique the stub itself
# for not containing verification (verified live 2026-08-03: every score
# landed near 0.0 with reasons like "the response does not verify the
# correctness..." — it was grading the stub's presentation, not doing
# independent fact-checking). Putting the full claim inside 'input' and
# instructing the judge to supply its own verification, with no
# actual_output to critique instead, avoids that failure mode.
#
# Deliberately does NOT penalize ambiguity, leaky/overlapping distractors,
# or shallow discrimination — an earlier version did, but human labeling
# of set 41 (2026-08-03) showed the human baseline marks a question
# "correct" whenever the keyed option is factually the best answer, even
# while noting in the comment that the question is ambiguous or has weak
# distractors (e.g. #140, #141, #150, #151 — all marked correct despite
# comments describing multiple defensible options). Penalizing ambiguity
# here would make the judge systematically disagree with that baseline
# for reasons that have nothing to do with factual correctness. Question
# quality/design concerns belong in set_quality_reviews.json instead —
# keeping this metric to strictly "is the keyed answer factually right"
# keeps the two tracks from overlapping.
FACTUAL_CORRECTNESS_CRITERIA = (
    "The 'input' describes a multiple-choice question, its four labeled "
    "options (A-D), and states which option is claimed to be the correct "
    "answer. There is no other context to grade — YOU are the "
    "fact-checker. Using ONLY your own general knowledge of the subject "
    "(NOT any external document), determine whether the claimed answer is "
    "factually correct and whether the other three options are factually "
    "wrong. Do not comment on whether 'input' itself contains "
    "justification — it never will, by design; you must supply the "
    "verification yourself. Your reasoning MUST cite the specific fact(s) "
    "you are relying on (e.g. 'the Krebs cycle occurs in the "
    "mitochondrial matrix, not the cytoplasm — standard cell biology'), "
    "not just assert a confident-sounding conclusion. Grade ONLY factual "
    "correctness of the claimed answer — do NOT penalize the question for "
    "being ambiguous, having distractors that overlap or are arguably "
    "also defensible, testing shallow recall, or any other question-"
    "design quality issue; those are a separate concern from whether the "
    "claimed answer is the factually correct one. Score close to 1.0 if "
    "the claimed answer is factually correct (even if the question itself "
    "is poorly discriminating or has weak distractors). Score close to "
    "0.0 only if the claimed answer is factually wrong."
)


def question_to_test_case(question) -> LLMTestCase:
    """Build an LLMTestCase from a Question row (or an equivalent dict with
    the same keys — used by human_review.py, which works off raw DB rows,
    and calibrate_judge.py, which replays human_labels.json entries)."""
    if hasattr(question, 'options'):
        options = question.options()
        text = question.text
        correct = question.correct_option
    else:
        options = question['options']
        text = question['text']
        correct = question['correct_option']

    input_text = (
        f"Question: {text}\n"
        f"A. {options['A']}\n"
        f"B. {options['B']}\n"
        f"C. {options['C']}\n"
        f"D. {options['D']}\n"
        f"Claimed correct option: {correct}"
    )
    return LLMTestCase(input=input_text)


def FactualCorrectnessMetric(model=None, threshold: float = JUDGE_PASS_THRESHOLD) -> GEval:
    """model defaults to a fresh NvidiaJudgeLLM() — pass an explicit
    instance to reuse one client/token across many measure() calls in a
    single run."""
    return GEval(
        name='FactualCorrectness',
        criteria=FACTUAL_CORRECTNESS_CRITERIA,
        evaluation_params=[SingleTurnParams.INPUT],
        model=model or NvidiaJudgeLLM(),
        threshold=threshold,
    )
