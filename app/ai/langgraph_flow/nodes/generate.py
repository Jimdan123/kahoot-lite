"""Stage 5 — write MCQs from the comprehension record, not from raw text.

The old version asked the LLM to read a chunk and "test factual
understanding" of it directly — that's an open invitation to write recall
questions ("What is X called?"). This version instead hands the model the
STRUCTURED record built by comprehend_chunks/merge_comprehension, points it
at a cross-chunk link when one touches this chunk, and forces it to commit to
which two facts (`hop_a`/`hop_b`) a question combines — and why that second
fact is actually necessary (`tests`) — BEFORE it writes the question text.
Field order matters here: an LLM filling JSON top-to-bottom that writes
`question` first will happily backfill a justification after the fact.
"""
from __future__ import annotations

import logging
import math
from typing import List

from app.ai.langgraph_flow.config import (
    FALLBACK_TIME_LIMIT,
    MAX_LINKS_PER_CHUNK,
    MAX_QUESTIONS_PER_CHUNK,
    MAX_TIME_LIMIT,
    MIN_ACCEPTED_QUESTIONS,
    MIN_TIME_LIMIT,
    QUESTIONS_PER_CHUNK,
    SHORT_DOC_QUOTA_SAFETY_FACTOR,
)
from app.ai.langgraph_flow.json_utils import content_to_text
from app.ai.langgraph_flow.llm_utils import invoke_json, make_llm
from app.ai.langgraph_flow.progress import emit
from app.ai.langgraph_flow.state import PipelineState
from langchain_core.messages import HumanMessage, SystemMessage

log = logging.getLogger('kahoot.ai')


_GENERATE_SYSTEM = """You write multiple-choice quiz questions for classroom
use. You are given a passage plus a structured comprehension record of it
(claims, definitions, mechanisms, quantities), possibly one or two LINKS to
facts that live in a different part of the same document, and — only for
documents that are thin on their own internal connections — an EXTERNAL
CONTEXT block of background why/reason facts from a web search on the
document's topic.

Write {n} MCQs.

THE TWO-HOP RULE — every question must require combining two distinct items
from the record (or one item from the record plus a LINK), not just one.
Valid pairings, in order of preference:
  1. A LINK, if one is given below — that's a fact in this passage plus a
     fact from elsewhere in the document; use both.
  2. APPLICATION — a MECHANISM that is a procedure, formula, or method (not
     just a narrative cause-and-effect chain), applied by YOU to a new,
     concrete scenario with numbers or specifics you invent — never stated
     in the passage. Prefer this pairing whenever the record contains a
     procedure/formula/method that could actually be executed on new
     inputs (a calculation, a decision rule, a classification test): it
     tests whether the student can USE what they read, not just recognize
     it restated. Work the invented scenario through to the answer
     yourself, step by step, before writing `correct` — a wrong worked
     answer is worse than skipping this pairing.
  3. A mechanism and the quantity/condition that constrains it.
  4. A claim and the assumption or definition it depends on.
  5. Two claims that reinforce or sit in tension with each other.
  6. A document DEFINITION plus a WHY/REASON fact from EXTERNAL CONTEXT, if
     given below — the definition supplies WHAT the term means; the
     EXTERNAL CONTEXT fact supplies WHY it matters or how it's actually
     used. Use this pairing only when EXTERNAL CONTEXT is present below AND
     none of pairings 1-5 apply to this passage.

MIX YOUR REFERENCE TYPES — do not pair two DEFINITIONS together, and write
at most one question per batch whose two hops are both DEFINITIONS. A
definition should combine with a CLAIM, MECHANISM, or QUANTITY that puts it
to use in context — e.g. "term X means Y" plus "the passage describes a
situation where Y applies" — not two glossary entries stitched together. If
the record is genuinely all definitions with nothing else to pair them with,
write fewer questions rather than padding with definition-only pairs (unless
pairing 6 above applies).

HARD RULE — EXTERNAL CONTEXT IS NEVER BOTH HOPS: EXTERNAL CONTEXT exists
ONLY to supply a WHY/REASON for a term the document itself defines. At most
ONE of hop_a/hop_b may come from EXTERNAL CONTEXT. The OTHER hop MUST
always be a DEFINITION, CLAIM, MECHANISM, QUANTITY, or LINK that is
actually present in THIS passage's comprehension record — never invent a
document-side hop, and never use two EXTERNAL CONTEXT facts together. A
question that could be fully answered using EXTERNAL CONTEXT alone, with
no real dependence on the document-side hop, tests general knowledge, not
whether the student read this document, and defeats the entire point of a
quiz generated from their upload. If you cannot find one genuine
document-grounded hop to pair a WHY fact with, do not use EXTERNAL CONTEXT
for that question at all — fall back to pairings 1-5, or write fewer
questions.

FORBIDDEN — do not write:
  - pure recall ("What is X called?", "What did the passage say about Y?")
  - a question answerable from a single item in the record with the other
    item as decoration
  - a question whose stem quotes the passage's exact phrasing (that makes it
    keyword-matchable instead of requiring understanding)
  - "According to the passage/text..." style phrasing — the student never
    sees the source
  - a question built entirely from two DEFINITION items (see MIX YOUR
    REFERENCE TYPES above)
  - a question where hop_a and hop_b are both EXTERNAL CONTEXT facts, or
    where the document-side hop is decorative and the question is really
    answerable from EXTERNAL CONTEXT alone (see HARD RULE above)

Rules for the options:
  - Exactly 4 options, labelled A, B, C, D. Exactly one is correct.
  - Wrong options must be plausible — something a student who understood only
    ONE of the two required hops would pick, not a random fact.
  - Question wording is standalone.
  - Language of the questions matches the language of the passage.

DIFFICULTY & TIME — rate each question "easy", "medium", or "hard" based on
how many real reasoning steps combining hop_a and hop_b actually takes (not
on vocabulary difficulty). Then set `time_limit`: YOUR honest estimate in
seconds of how long a student who has the two hops in mind still needs to
work through the combination to the answer. Think it through yourself
first — a question with real arithmetic or a multi-step chain needs far
more than a recall check, even if you rated it "medium". Do not default to
a round number out of habit.

PROCEDURE — for each question, fill fields IN ORDER. Identify hop_a and hop_b
FIRST. Then write `tests`: the one specific misunderstanding a student falls
into if they only have one of the two hops. Only THEN write the question,
then rate difficulty and time_limit last (you can't judge either until the
question exists). For an APPLICATION pairing specifically: work the invented
scenario through to a numeric/concrete answer yourself BEFORE writing the
question or options — the `correct` option must match your own worked
answer exactly, and the wrong options should be answers a student would get
from one specific, identifiable slip (wrong formula variable, off-by-one
step, wrong unit), not arbitrary numbers.

JSON SAFETY: your entire response must be valid JSON. If a question, option,
or any field would involve mathematical notation (LaTeX like \\ldots, \\theta,
matrix/vector syntax, or symbols like Θ, σ, √), do NOT write the raw
notation — rewrite it in plain words/ASCII instead (e.g. "the square root
of A transpose A" not "\\sqrt{{A^TA}}", "lambda" not "\\lambda"). A literal
backslash almost always breaks JSON string escaping — never include one.

Return a single JSON array. No prose, no code fences. Each element:
{{
  "hop_a": "<the first fact used, and where it's from — a DEFINITION/CLAIM/
            MECHANISM/QUANTITY/LINK in the document, an APPLICATION (a
            document procedure/formula applied to numbers you invented and
            solved yourself), or EXTERNAL CONTEXT>",
  "hop_b": "<the second fact used, and where it's from — same options as
            hop_a; at least one of hop_a/hop_b must be document-side>",
  "tests": "<what a student who only knew hop_a would get wrong>",
  "question": "...",
  "A": "...", "B": "...", "C": "...", "D": "...",
  "correct": "A",
  "difficulty": "easy | medium | hard",
  "time_limit": 20
}}
"""


def _chunk_importance(n_chunks: int, comprehension: dict) -> List[float]:
    """Score each chunk 0..n_chunks-1 by how central its content is to the
    document, so its question quota can scale with that instead of being
    flat. Cross-chunk LINKS are merge_comprehension's own signal for "this
    connects to the rest of the document" (its docstring calls links "the
    most valuable output of this step"); MECHANISMS are comprehend_chunks'
    own signal for "this makes the best exam questions" (its docstring:
    "look for them even if it takes rereading the passage"). A chunk with
    neither still gets the baseline weight of 1.0, never zero — thin filler
    chunks get fewer questions, not none."""
    weights = [1.0] * n_chunks
    for link in comprehension.get('links', []) or []:
        for idx in (link.get('chunk_a'), link.get('chunk_b')):
            if isinstance(idx, int) and 0 <= idx < n_chunks:
                weights[idx] += 1.0
    for m in comprehension.get('mechanisms', []) or []:
        idx = m.get('chunk_index')
        if isinstance(idx, int) and 0 <= idx < n_chunks:
            weights[idx] += 0.5
    return weights


def _allocate_quotas(n_chunks: int, comprehension: dict) -> List[int]:
    """Turn chunk importance weights into an integer per-chunk question
    quota. The total budget scales with document length — short documents
    (few chunks) target roughly MIN_ACCEPTED_QUESTIONS * SHORT_DOC_QUOTA_
    SAFETY_FACTOR drafts total, a safety margin against quality_check's own
    rejection rate, instead of the flat QUESTIONS_PER_CHUNK baseline
    undershooting MIN_ACCEPTED_QUESTIONS after filtering (confirmed live: a
    3-chunk document settled at 3 accepted questions, under its own
    5-question floor, with no scaling). That total is then distributed
    across chunks proportionally to importance rather than evenly, so a
    chunk with more cross-chunk links or a real procedural mechanism gets a
    bigger share than a chunk with neither. Every chunk still gets at least
    1, capped at MAX_QUESTIONS_PER_CHUNK, so nothing is starved to zero or
    asked for an unreasonable number of questions from one short passage."""
    if n_chunks == 0:
        return []
    base = max(QUESTIONS_PER_CHUNK,
               math.ceil(MIN_ACCEPTED_QUESTIONS * SHORT_DOC_QUOTA_SAFETY_FACTOR / n_chunks))
    base = min(base, MAX_QUESTIONS_PER_CHUNK)
    total_budget = base * n_chunks
    weights = _chunk_importance(n_chunks, comprehension)
    weight_sum = sum(weights) or float(n_chunks)
    return [max(1, min(MAX_QUESTIONS_PER_CHUNK, round(total_budget * w / weight_sum)))
            for w in weights]


def generate_questions(state: PipelineState) -> dict:
    chunks = state['chunks']
    comprehension = state.get('comprehension') or {}
    chunk_records = state.get('chunk_records') or []
    content_type_by_index = {r.get('chunk_index'): r.get('content_type', 'narrative')
                              for r in chunk_records}
    n_chunks = len(chunks)
    quotas = _allocate_quotas(n_chunks, comprehension)
    emit(state, f'Connecting to the AI service (0/{n_chunks})…', 0.45)
    try:
        llm = make_llm(user_keys=state.get('user_llm_keys'), custom_providers=state.get('custom_llm_providers'))
    except Exception as exc:
        # Fatal: can't make the client at all (bad model name, missing/invalid key).
        # Message deliberately generic — this reaches the end user via the
        # failed-job UI, and which underlying provider/model powers the app
        # isn't something to expose there. Full detail (including exc,
        # which may mention the provider) still goes to the server log.
        log.error(f'LLM client init failed: {exc!r}')
        return {'error': f'Could not initialize the AI client: {exc}'}

    drafts: List[dict] = []
    first_error = None
    fail_count = 0
    skipped_unanswerable = 0

    for i, chunk in enumerate(chunks):
        emit(state, f'Writing questions ({i}/{n_chunks})…',
             0.45 + 0.25 * i / max(n_chunks, 1))
        if content_type_by_index.get(i) == 'unanswerable_exercise':
            # Fill-in-blank/listening exercise whose answers depend on audio,
            # an image, or a table the student fills in themselves — nothing
            # in the text to ground a question in, so don't ask the LLM to
            # invent one. Cheaper and safer than relying on the prompt alone.
            skipped_unanswerable += 1
            continue
        context = _build_context(i, comprehension, state.get('search_context') or [])
        try:
            resp, parsed = invoke_json(llm, [
                SystemMessage(content=_GENERATE_SYSTEM.format(n=quotas[i])),
                HumanMessage(content=f'Passage:\n\n{chunk}\n\n{context}'),
            ], token_usage=state.get('token_usage'))
            log.info(f'chunk {i + 1}/{n_chunks}: parsed {len(parsed)} draft questions')
            if not parsed:
                raise ValueError(
                    f'AI response was not parseable as JSON '
                    f'({len(content_to_text(resp.content))} chars received, '
                    f'see the parse_llm_json warning above for exactly where it broke)'
                )
            for q in parsed:
                q['_chunk_index'] = i
                _normalize_difficulty_and_time(q)
            drafts.extend(parsed)
        except Exception as exc:
            # One bad chunk doesn't kill the run, but keep the FIRST error
            # so we can surface something useful if EVERY chunk fails.
            fail_count += 1
            if first_error is None:
                first_error = f'{type(exc).__name__}: {exc}'
            log.warning(f'chunk {i + 1}/{n_chunks} failed: {first_error}')
            continue

    if skipped_unanswerable:
        log.info(f'generate_questions: skipped {skipped_unanswerable}/{n_chunks} '
                  f'unanswerable_exercise chunks')
    emit(state, f'Drafted {len(drafts)} candidate questions.', 0.7)

    attempted = n_chunks - skipped_unanswerable
    if not drafts and first_error and attempted and fail_count == attempted:
        return {'draft_questions': [], 'error': f'All LLM calls failed. First error: {first_error}'}

    # Explicitly clear a stale error from an earlier failed attempt — LangGraph
    # merges returned dicts into state and does not drop keys we don't return,
    # so without this a retry that succeeds after a fully-failed first pass
    # would still carry the old error through to run_pipeline's final check.
    return {'draft_questions': drafts, 'error': None}


def _normalize_difficulty_and_time(q: dict) -> None:
    """Clamp the LLM's own difficulty/time_limit rating to sane bounds
    in-place. This is a safety net, not the source of truth — the model
    picks the actual value per question; we just guard against it omitting
    the field or returning nonsense."""
    if q.get('difficulty') not in ('easy', 'medium', 'hard'):
        q['difficulty'] = 'medium'
    try:
        time_limit = int(q.get('time_limit'))
    except (TypeError, ValueError):
        time_limit = FALLBACK_TIME_LIMIT
    q['time_limit'] = max(MIN_TIME_LIMIT, min(MAX_TIME_LIMIT, time_limit))


def _build_context(chunk_index: int, comprehension: dict, search_context: list) -> str:
    """Render the local comprehension items for this chunk, plus any
    cross-chunk links touching it and any external why/reason facts, as
    plain text to append to the prompt."""
    lines = ['Comprehension record for this passage:']

    def _touches(indices_key: str, single_key: str, item: dict) -> bool:
        if indices_key in item:
            return chunk_index in (item.get(indices_key) or [])
        return item.get(single_key) == chunk_index

    for claim in comprehension.get('claims', []):
        if _touches('chunk_indices', 'chunk_index', claim):
            lines.append(f"- CLAIM: {claim.get('text', '')}")
    for d in comprehension.get('definitions', []):
        if _touches('chunk_indices', 'chunk_index', d):
            lines.append(f"- DEFINITION: {d.get('term', '')} = {d.get('definition', '')}")
    for m in comprehension.get('mechanisms', []):
        if m.get('chunk_index') == chunk_index:
            steps = ' -> '.join(m.get('steps', []))
            lines.append(f"- MECHANISM: {m.get('name', '')}: {steps}")
    for q in comprehension.get('quantities', []):
        if q.get('chunk_index') == chunk_index:
            lines.append(f"- QUANTITY: {q.get('value', '')} measures {q.get('measures', '')} "
                          f"({q.get('conditions', '')})")

    links = [l for l in comprehension.get('links', [])
             if l.get('chunk_a') == chunk_index or l.get('chunk_b') == chunk_index]
    for link in links[:MAX_LINKS_PER_CHUNK]:
        other_side = link.get('item_b') if link.get('chunk_a') == chunk_index else link.get('item_a')
        lines.append(f"- LINK to another part of the document ({link.get('relation', '')}): {other_side}")

    if len(lines) == 1:
        lines.append('(no extracted items for this passage — read it directly)')

    if search_context:
        lines.append('')
        lines.append('EXTERNAL CONTEXT (background only — from a web search on the '
                      'document topic, NOT part of the document itself):')
        for fact in search_context:
            lines.append(f"- WHY: {fact.get('topic_term', '')} — {fact.get('fact', '')}")

    return '\n'.join(lines)
