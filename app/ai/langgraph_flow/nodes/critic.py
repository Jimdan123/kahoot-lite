"""Stage 6 & 7 — verify a question actually needs the source material.

closed_book_check asks a SEPARATE, context-free LLM call to answer each
draft using only general knowledge. quality_check used to hard-reject any
question that call answered correctly with real confidence (a "closed-book
leak"); it now KEEPS that question instead — the two-hop reasoning is often
still genuine even when the specific answer happens to also be common
knowledge — and tags it source='closed_book' so it's honestly labeled
rather than silently discarded. The other rejection tests (structural
issues, decorative second hop, ambiguity, etc.) are unchanged.
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
    llm = make_llm(temperature=0.0, user_keys=state.get('user_llm_keys'))
    try:
        resp, results = invoke_json(llm, [
            SystemMessage(content=_CLOSED_BOOK_SYSTEM),
            HumanMessage(content=json.dumps(items, ensure_ascii=False)),
        ], token_usage=state.get('token_usage'))
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

Each item includes the question, its options and correct answer, and the two
hops it claims to combine. Whether the question is ALSO answerable from
general knowledge is judged separately, outside this pass, and is NOT one of
your rejection tests — do not reject a question just because it seems like
common knowledge; judge it purely on whether it genuinely requires combining
hop_a and hop_b.

Apply these rejection tests, in order. Reject on the first failure.

  STRUCTURAL
    More than one option looks correct, the correct option isn't among
    A-D, or an option repeats the answer verbatim from the question stem.
    REJECT.

  PURE DEFINITION PAIRING
    Both hop_a and hop_b are dictionary-style definitions or glossary
    entries (e.g. two vocabulary-matching pairs), with no narrative fact,
    mechanism, or quantity connecting them to an actual situation in the
    passage. That's vocabulary recall dressed up as two hops. REJECT.

  EXTERNAL-ONLY PAIRING
    Both hop_a and hop_b are attributed to EXTERNAL CONTEXT (a web-search
    background fact), with no hop actually grounded in the document's own
    comprehension record. That means the question doesn't test the
    document at all — REJECT.

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
"""


def quality_check(state: PipelineState) -> dict:
    emit(state, 'Checking quality…', 0.85)
    # Questions already accepted on an earlier retry attempt (see graph.py's
    # retry loop) — generate_questions writes fresh drafts each retry from
    # the same unchanged comprehension record, so without carrying these
    # forward a retry would silently throw away good questions attempt 1
    # already found, instead of building on them toward MIN_ACCEPTED_QUESTIONS.
    accumulated = list(state.get('validated_questions') or [])
    drafts = state.get('draft_questions') or []
    if not drafts:
        return {'validated_questions': accumulated}

    closed_book = state.get('closed_book_results') or [{} for _ in drafts]
    # Computed here, in Python, from closed_book_check's own results rather
    # than asked of the critic LLM — deterministic, and keeps "does this
    # leak" (a fact) separate from "is this well-formed" (the critic's
    # actual job, see _CRITIC_SYSTEM).
    leaked = [_is_closed_book_leak(closed_book[i] if i < len(closed_book) else {}, d)
              for i, d in enumerate(drafts)]

    items = [
        {'question': q.get('question', ''), 'A': q.get('A', ''), 'B': q.get('B', ''),
         'C': q.get('C', ''), 'D': q.get('D', ''), 'correct': q.get('correct', ''),
         'hop_a': q.get('hop_a', ''), 'hop_b': q.get('hop_b', ''), 'tests': q.get('tests', '')}
        for q in drafts
    ]

    llm = make_llm(temperature=0.0, user_keys=state.get('user_llm_keys'))  # deterministic grading
    try:
        resp, verdicts = invoke_json(llm, [
            SystemMessage(content=_CRITIC_SYSTEM),
            HumanMessage(content=json.dumps(items, ensure_ascii=False)),
        ], token_usage=state.get('token_usage'))
        if not isinstance(verdicts, list) or len(verdicts) != len(drafts):
            raise ValueError('critic response malformed')
        keep = [(q, leaked[i]) for i, (q, v) in enumerate(zip(drafts, verdicts))
                if isinstance(v, dict) and v.get('verdict') == 'accept']
    except Exception as exc:
        # If the grader fails, fall back to structural validity only rather
        # than dropping everything (or keeping obviously-broken drafts).
        log.warning(f'quality_check: critic call failed ({exc!r}), falling back to structural checks')
        keep = [(q, leaked[i]) for i, q in enumerate(drafts) if _structurally_valid(q)]

    # Dedupe by exact question text (case-insensitive) — seeded with
    # already-accumulated questions from earlier retries so this attempt's
    # drafts can't re-add a near-identical question a prior attempt kept.
    unique: List[dict] = list(accumulated)
    seen: set = {(q.get('question') or '').strip().lower() for q in accumulated}
    n_tagged = 0
    for q, is_leak in keep:
        key = (q.get('question') or '').strip().lower()
        if key and key not in seen:
            seen.add(key)
            if is_leak:
                q['source'] = 'closed_book'
                n_tagged += 1
            unique.append(q)
    n_new = len(unique) - len(accumulated)
    log.info(f'quality_check: {n_new}/{len(drafts)} new questions passed '
             f'({n_tagged} tagged closed_book), {len(unique)} accepted total so far')
    return {'validated_questions': unique}


def _is_closed_book_leak(cb: dict, draft: dict) -> bool:
    """True if the context-free attempt (see closed_book_check) matched this
    draft's correct answer at medium/high confidence — i.e. answerable
    without the document. No longer a rejection reason; kept questions get
    tagged source='closed_book' instead of discarded (see quality_check)."""
    return cb.get('confidence') in ('high', 'medium') and cb.get('answer') == draft.get('correct')


def _structurally_valid(q: dict) -> bool:
    if q.get('correct') not in ('A', 'B', 'C', 'D'):
        return False
    return all((q.get(k) or '').strip() for k in ('question', 'A', 'B', 'C', 'D'))
