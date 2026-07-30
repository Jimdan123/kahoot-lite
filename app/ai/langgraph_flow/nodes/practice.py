"""Stage 4.5 — topic-practice track: easy/medium/hard MCQs on the document's
subject, separate from the two-hop comprehension questions generate.py
writes.

Those two-hop questions must combine two facts from THIS document and get
rejected later if a closed-book model could answer them without it (see
critic.py) — the whole point is testing whether the student read it. This
track is the opposite: general PRACTICE problems on the same topic/skill,
meant to be answerable from the model's own knowledge. (Originally this used
Groq's compound-mini for real web search, but this org's Groq console only
has llama-3.3-70b-versatile and qwen/qwen3.6-27b enabled — see
GROQ_MODEL_CHAIN in config.py — so it runs on the same rotating text model
as everything else and is told to reason/verify carefully instead.) If the
document already has its own exercises, this explicitly writes NEW problems
of the same kind rather than rewording them.

Additive and best-effort: any failure here just yields an empty list rather
than touching state['error'], since the main pipeline already produces a
usable question set without it.
"""
from __future__ import annotations

import logging

from app.ai.langgraph_flow.config import (
    FALLBACK_TIME_LIMIT,
    MAX_TIME_LIMIT,
    MIN_TIME_LIMIT,
    PRACTICE_QUESTIONS_PER_DIFFICULTY,
)
from app.ai.langgraph_flow.llm_utils import invoke_json, make_llm
from app.ai.langgraph_flow.progress import emit
from app.ai.langgraph_flow.state import PipelineState
from langchain_core.messages import HumanMessage, SystemMessage

log = logging.getLogger('kahoot.ai')


_PRACTICE_SYSTEM = """You write extra multiple-choice PRACTICE questions on a
given topic, for a student who just studied it. These are separate from any
questions already in the source document — your job is to give the student
MORE problems of the same kind to practice on, not to quiz them on the
document itself.

Topic: {topic}

Before writing anything with a specific numeric or factual answer, work
through the method properly step by step and verify your chosen answer is
actually correct — do not guess at a calculation or state a fact you are not
confident in.

{existing_block}

Write exactly {n} EASY, {n} MEDIUM, and {n} HARD questions ({total} total).
  - EASY: tests the basic definition/rule directly, one step.
  - MEDIUM: requires applying the rule/method once, in a slightly novel
    setting.
  - HARD: requires multiple steps, combining two things about the topic, or
    a less common edge case. Not just "bigger numbers" — genuinely more
    reasoning.

Rules:
  - If the document already has its own exercises (given above), your
    questions must be NEW problems on the same underlying concept/skill —
    different numbers, scenario, or wording — never a reworded copy of one
    of them.
  - Exactly 4 options, labelled A, B, C, D. Exactly one is correct.
  - Wrong options must be plausible mistakes (a sign error, an off-by-one, a
    common misconception) — not random distractors.
  - Question wording is standalone (no "according to the passage/text...").
  - `time_limit` is YOUR honest estimate, in seconds, of how long a student
    actually needs to solve THIS specific question — think it through
    yourself first. A one-step recall question might need 12-20s. A
    multi-step statistics or math calculation can legitimately need
    60-90s+. Do not default to a round number out of habit — base it on the
    real number of steps/arithmetic involved.

Return a single JSON array. No prose, no code fences. Each element:
{{
  "difficulty": "easy | medium | hard",
  "time_limit": 20,
  "question": "...",
  "A": "...", "B": "...", "C": "...", "D": "...",
  "correct": "A"
}}
"""


def generate_practice_questions(state: PipelineState) -> dict:
    comprehension = state.get('comprehension') or {}
    topic = (comprehension.get('topic') or '').strip()
    if not topic:
        # merge_comprehension fell back to naive concat (no LLM-derived
        # topic) — use the first chunk as a stand-in for "what is this
        # about" rather than skipping the whole track.
        chunks = state.get('chunks') or []
        topic = (chunks[0][:300] if chunks else '').strip()
    if not topic:
        log.info('generate_practice_questions: no topic available, skipping practice track')
        return {'practice_questions': []}

    n = state.get('practice_questions_per_difficulty') or PRACTICE_QUESTIONS_PER_DIFFICULTY
    total = n * 3
    emit(state, f'Writing {total} extra practice problems on the topic…', 0.42)

    existing = comprehension.get('existing_exercises') or []
    existing_block = (
        'The document already contains these exercises — do NOT rewrite or '
        'lightly reword any of them:\n' + '\n'.join(f'- {e}' for e in existing[:8])
    ) if existing else ''

    try:
        llm = make_llm(temperature=0.4)
        resp, parsed = invoke_json(llm, [
            SystemMessage(content=_PRACTICE_SYSTEM.format(
                topic=topic, existing_block=existing_block, n=n, total=total)),
            HumanMessage(content='Write the practice questions now.'),
        ])
        if not isinstance(parsed, list):
            raise ValueError('practice response was not a JSON array')
    except Exception as exc:
        log.warning(f'generate_practice_questions: LLM call failed ({exc!r}), skipping practice track')
        return {'practice_questions': []}

    questions = [norm for q in parsed if (norm := _normalize(q)) is not None]
    for q in questions:
        q['_source'] = 'practice'
    log.info(f'generate_practice_questions: {len(questions)}/{len(parsed)} practice questions kept')
    return {'practice_questions': questions}


def _normalize(q):
    if not isinstance(q, dict):
        return None
    if q.get('correct') not in ('A', 'B', 'C', 'D'):
        return None
    if not all((q.get(k) or '').strip() for k in ('question', 'A', 'B', 'C', 'D')):
        return None
    difficulty = q.get('difficulty') if q.get('difficulty') in ('easy', 'medium', 'hard') else 'medium'
    try:
        time_limit = int(q.get('time_limit'))
    except (TypeError, ValueError):
        time_limit = FALLBACK_TIME_LIMIT
    time_limit = max(MIN_TIME_LIMIT, min(MAX_TIME_LIMIT, time_limit))
    return {**q, 'difficulty': difficulty, 'time_limit': time_limit}
