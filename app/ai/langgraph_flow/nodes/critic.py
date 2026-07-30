"""Stage 6 & 7 — verify a question actually needs the source material.

closed_book_check asks a SEPARATE, context-free LLM call to answer each
draft using only general knowledge. quality_check then rejects any question
that call answered correctly with real confidence (a "closed-book leak") —
that's the single strongest signal a question tests trivia recall rather
than comprehension of this specific document — plus the usual structural
checks (multiple correct options, decorative second hop, ambiguity).
"""
from __future__ import annotations

import json
import logging
from typing import List

from app.ai.langgraph_flow.llm_utils import invoke_json, make_llm
from app.ai.langgraph_flow.progress import emit
from app.ai.langgraph_flow.state import PipelineState
from langchain_core.messages import HumanMessage, SystemMessage

log = logging.getLogger('kahoot.ai')


# ---- Stage 6: closed-book attempt --------------------------------------

_CLOSED_BOOK_SYSTEM = """For each question below, answer using ONLY your
general knowledge — you have not read whatever source document these came
from, and won't see it.

Input: a JSON array of {"index": 0, "question": "", "A": "", "B": "", "C": "", "D": ""}

Return a JSON array, same length and order as the input. Each element:
{"answer": "A | B | C | D | unknown", "confidence": "high | medium | low"}
No prose, no code fences, no explanation after the array — the array is the
entire response.

Confidence reflects how sure you are of your chosen answer. If the options
all seem equally plausible without having read the source, answer "unknown"
with "low" confidence rather than guessing randomly among them.
"""


def closed_book_check(state: PipelineState) -> dict:
    drafts = state.get('draft_questions') or []
    if not drafts:
        return {'closed_book_results': []}
    emit(state, 'Checking questions aren\'t answerable from general knowledge…', 0.75)

    items = [
        {'index': i, 'question': q.get('question', ''),
         'A': q.get('A', ''), 'B': q.get('B', ''), 'C': q.get('C', ''), 'D': q.get('D', '')}
        for i, q in enumerate(drafts)
    ]
    llm = make_llm(temperature=0.0)
    try:
        resp, results = invoke_json(llm, [
            SystemMessage(content=_CLOSED_BOOK_SYSTEM),
            HumanMessage(content=json.dumps(items, ensure_ascii=False)),
        ])
        if not isinstance(results, list) or len(results) != len(drafts):
            raise ValueError(f'expected {len(drafts)} results, got '
                              f'{len(results) if isinstance(results, list) else type(results)}')
    except Exception as exc:
        # If this call fails we should NOT silently treat every question as
        # "leaked" (that would reject everything) nor as "safe" (that would
        # skip the check entirely) — mark all unknown so quality_check falls
        # through to its structural checks only.
        log.warning(f'closed_book_check: LLM call failed ({exc!r}), skipping leak detection this round')
        results = [{'answer': 'unknown', 'confidence': 'low'} for _ in drafts]

    n_leaked = sum(1 for r, d in zip(results, drafts)
                   if isinstance(r, dict) and r.get('confidence') in ('high', 'medium')
                   and r.get('answer') == d.get('correct'))
    log.info(f'closed_book_check: {n_leaked}/{len(drafts)} questions answerable without the document')
    return {'closed_book_results': results}


# ---- Stage 7: final quality gate --------------------------------------

_CRITIC_SYSTEM = """You are quality-filtering generated exam questions before
they go live in a classroom quiz.

Each item includes the question, its options and correct answer, the two
hops it claims to combine, and the result of a SEPARATE attempt to answer
the same question using only general knowledge (no access to the source
document).

Apply these rejection tests, in order. Reject on the first failure.

  CLOSED-BOOK LEAK
    The closed-book attempt chose the SAME letter as `correct`, at medium or
    high confidence, with no access to the document. That means the
    question is answerable from general knowledge alone — REJECT it no
    matter how good hop_a/hop_b look on paper.

  STRUCTURAL
    More than one option looks correct, the correct option isn't among
    A-D, or an option repeats the answer verbatim from the question stem.
    REJECT.

  PURE DEFINITION PAIRING
    Both hop_a and hop_b are dictionary-style definitions or glossary
    entries (e.g. two vocabulary-matching pairs), with no narrative fact,
    mechanism, or quantity connecting them to an actual situation in the
    passage. That's vocabulary recall dressed up as two hops. REJECT.

  DECORATIVE HOP
    Read hop_b. If the question is fully answerable while ignoring it, it's
    decoration, not a real second hop. REJECT.

  AMBIGUOUS OR TRIVIAL
    The question is unclear, admits more than one defensible correct
    answer, or turns out to be trivial despite claiming two hops. REJECT.

Otherwise ACCEPT.

Input: a JSON array of question records (see fields above).

Return a JSON array, same length and order as the input. Each element:
{"verdict": "accept | reject", "reason": "<one short phrase>"}
No prose, no code fences, no explanation after the array — the array is the
entire response.

Be strict about the closed-book leak test especially — it's the single best
signal a question doesn't actually require this document. Expect to reject
a meaningful fraction of what you're given.
"""


def quality_check(state: PipelineState) -> dict:
    emit(state, 'Checking quality…', 0.85)
    drafts = state.get('draft_questions') or []
    if not drafts:
        return {'validated_questions': []}

    closed_book = state.get('closed_book_results') or [{} for _ in drafts]
    items = []
    for i, q in enumerate(drafts):
        cb = closed_book[i] if i < len(closed_book) else {}
        items.append({
            'question': q.get('question', ''), 'A': q.get('A', ''), 'B': q.get('B', ''),
            'C': q.get('C', ''), 'D': q.get('D', ''), 'correct': q.get('correct', ''),
            'hop_a': q.get('hop_a', ''), 'hop_b': q.get('hop_b', ''), 'tests': q.get('tests', ''),
            'closed_book_answer': cb.get('answer', 'unknown'),
            'closed_book_confidence': cb.get('confidence', 'low'),
        })

    llm = make_llm(temperature=0.0)  # deterministic grading
    try:
        resp, verdicts = invoke_json(llm, [
            SystemMessage(content=_CRITIC_SYSTEM),
            HumanMessage(content=json.dumps(items, ensure_ascii=False)),
        ])
        if not isinstance(verdicts, list) or len(verdicts) != len(drafts):
            raise ValueError('critic response malformed')
        keep = [q for q, v in zip(drafts, verdicts)
                if isinstance(v, dict) and v.get('verdict') == 'accept']
    except Exception as exc:
        # If the grader fails, fall back to structural validity only rather
        # than dropping everything (or keeping obviously-broken drafts).
        log.warning(f'quality_check: critic call failed ({exc!r}), falling back to structural checks')
        keep = [q for q in drafts if _structurally_valid(q)]

    # Dedupe by exact question text (case-insensitive)
    seen: set = set()
    unique: List[dict] = []
    for q in keep:
        key = (q.get('question') or '').strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(q)
    log.info(f'quality_check: {len(unique)}/{len(drafts)} questions passed')
    return {'validated_questions': unique}


def _structurally_valid(q: dict) -> bool:
    if q.get('correct') not in ('A', 'B', 'C', 'D'):
        return False
    return all((q.get(k) or '').strip() for k in ('question', 'A', 'B', 'C', 'D'))
