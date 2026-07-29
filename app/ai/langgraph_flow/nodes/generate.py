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
from typing import List

from app.ai.langgraph_flow.config import MAX_LINKS_PER_CHUNK, QUESTIONS_PER_CHUNK
from app.ai.langgraph_flow.json_utils import parse_llm_json, content_to_text
from app.ai.langgraph_flow.llm_utils import make_llm
from app.ai.langgraph_flow.progress import emit
from app.ai.langgraph_flow.state import PipelineState
from langchain_core.messages import HumanMessage, SystemMessage

log = logging.getLogger('kahoot.ai')


_GENERATE_SYSTEM = """You write multiple-choice quiz questions for classroom
use. You are given a passage plus a structured comprehension record of it
(claims, definitions, mechanisms, quantities), and possibly one or two LINKS
to facts that live in a different part of the same document.

Write {n} MCQs.

THE TWO-HOP RULE — every question must require combining two distinct items
from the record (or one item from the record plus a LINK), not just one.
Valid pairings, in order of preference:
  1. A LINK, if one is given below — that's a fact in this passage plus a
     fact from elsewhere in the document; use both.
  2. A mechanism and the quantity/condition that constrains it.
  3. A claim and the assumption or definition it depends on.
  4. Two claims that reinforce or sit in tension with each other.

FORBIDDEN — do not write:
  - pure recall ("What is X called?", "What did the passage say about Y?")
  - a question answerable from a single item in the record with the other
    item as decoration
  - a question whose stem quotes the passage's exact phrasing (that makes it
    keyword-matchable instead of requiring understanding)
  - "According to the passage/text..." style phrasing — the student never
    sees the source

Rules for the options:
  - Exactly 4 options, labelled A, B, C, D. Exactly one is correct.
  - Wrong options must be plausible — something a student who understood only
    ONE of the two required hops would pick, not a random fact.
  - Question wording is standalone.
  - Language of the questions matches the language of the passage.

PROCEDURE — for each question, fill fields IN ORDER. Identify hop_a and hop_b
FIRST. Then write `tests`: the one specific misunderstanding a student falls
into if they only have one of the two hops. Only THEN write the question.

Return a single JSON array. No prose, no code fences. Each element:
{{
  "hop_a": "<the first fact used, and where it's from>",
  "hop_b": "<the second fact used, and where it's from>",
  "tests": "<what a student who only knew hop_a would get wrong>",
  "question": "...",
  "A": "...", "B": "...", "C": "...", "D": "...",
  "correct": "A"
}}
"""


def generate_questions(state: PipelineState) -> dict:
    chunks = state['chunks']
    comprehension = state.get('comprehension') or {}
    n_chunks = len(chunks)
    emit(state, f'Connecting to Groq (0/{n_chunks})…', 0.45)
    try:
        llm = make_llm()
    except Exception as exc:
        # Fatal: can't make the client at all (bad model name, missing/invalid key).
        log.error(f'ChatGroq init failed: {exc!r}')
        return {'error': f'Could not initialize Groq client: {exc}'}

    drafts: List[dict] = []
    first_error = None
    fail_count = 0

    for i, chunk in enumerate(chunks):
        emit(state, f'Writing questions ({i}/{n_chunks})…',
             0.45 + 0.25 * i / max(n_chunks, 1))
        context = _build_context(i, comprehension)
        try:
            resp = llm.invoke([
                SystemMessage(content=_GENERATE_SYSTEM.format(n=QUESTIONS_PER_CHUNK)),
                HumanMessage(content=f'Passage:\n\n{chunk}\n\n{context}'),
            ])
            parsed = parse_llm_json(resp.content)
            log.info(f'chunk {i + 1}/{n_chunks}: parsed {len(parsed)} draft questions')
            if not parsed:
                raise ValueError(
                    f'Groq response was not parseable as JSON '
                    f'({len(content_to_text(resp.content))} chars received, '
                    f'see the parse_llm_json warning above for exactly where it broke)'
                )
            for q in parsed:
                q['_chunk_index'] = i
            drafts.extend(parsed)
        except Exception as exc:
            # One bad chunk doesn't kill the run, but keep the FIRST error
            # so we can surface something useful if EVERY chunk fails.
            fail_count += 1
            if first_error is None:
                first_error = f'{type(exc).__name__}: {exc}'
            log.warning(f'chunk {i + 1}/{n_chunks} failed: {first_error}')
            continue

    emit(state, f'Drafted {len(drafts)} candidate questions.', 0.7)

    if not drafts and first_error and fail_count == n_chunks:
        return {'draft_questions': [], 'error': f'All LLM calls failed. First error: {first_error}'}

    # Explicitly clear a stale error from an earlier failed attempt — LangGraph
    # merges returned dicts into state and does not drop keys we don't return,
    # so without this a retry that succeeds after a fully-failed first pass
    # would still carry the old error through to run_pipeline's final check.
    return {'draft_questions': drafts, 'error': None}


def _build_context(chunk_index: int, comprehension: dict) -> str:
    """Render the local comprehension items for this chunk, plus any
    cross-chunk links touching it, as plain text to append to the prompt."""
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
    return '\n'.join(lines)
