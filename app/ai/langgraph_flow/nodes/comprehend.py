"""Stage 3 & 4 — build a structured understanding of the material BEFORE any
question gets written.

comprehend_chunks extracts claims/definitions/mechanisms/quantities (each
grounded in a verbatim span) from every chunk independently.

merge_comprehension then dedupes those per-chunk records and — the whole
point of splitting this into two stages — looks for CROSS-CHUNK LINKS: pairs
of facts that live in different chunks but connect (a mechanism's condition
quantified elsewhere, a term defined in one place and used in another). Those
links are what let generate_questions ask something that requires combining
two separate parts of the document, instead of one question per isolated
paragraph.
"""
from __future__ import annotations

import json
import logging
from typing import List

from app.ai.langgraph_flow.json_utils import parse_llm_json
from app.ai.langgraph_flow.llm_utils import make_llm
from app.ai.langgraph_flow.progress import emit
from app.ai.langgraph_flow.state import PipelineState
from langchain_core.messages import HumanMessage, SystemMessage

log = logging.getLogger('kahoot.ai')


# ---- Stage 3: per-chunk extraction ------------------------------------------

_COMPREHEND_SYSTEM = """You are building a structured comprehension record of
one passage of study material. This record will be used later to write exam
questions, so it must be precise — a vague record produces vague questions.

Extract:

CLAIMS — factual assertions the passage makes that carry explanatory weight.
Skip claims too trivial to be worth testing.

DEFINITIONS — terms the passage defines or uses in a specialized sense.

MECHANISMS — causal or procedural chains: "A causes B, which leads to C
under condition D." Write as ordered steps. These make the best exam
questions, so look for them even if it takes rereading the passage.

QUANTITIES — numbers, thresholds, or ranges, with what they measure and
under what condition they were obtained.

Every item must include `span`: the shortest verbatim quote (max 20 words)
from the passage that supports it. If you cannot produce a span, drop the
item — do not include unsupported items.

Return JSON:
{
  "claims": [{"text": "", "span": ""}],
  "definitions": [{"term": "", "definition": "", "span": ""}],
  "mechanisms": [{"name": "", "steps": [""], "span": ""}],
  "quantities": [{"value": "", "measures": "", "conditions": "", "span": ""}]
}

Empty arrays are correct when a passage genuinely has nothing of that type.
Do not pad, and do not invent anything not present in the passage.
"""


def comprehend_chunks(state: PipelineState) -> dict:
    chunks = state['chunks']
    n_chunks = len(chunks)
    emit(state, f'Reading for comprehension (0/{n_chunks})…', 0.25)
    llm = make_llm(temperature=0.0)  # extraction should be deterministic, not creative

    records: List[dict] = []
    for i, chunk in enumerate(chunks):
        emit(state, f'Reading for comprehension ({i}/{n_chunks})…',
             0.25 + 0.15 * i / max(n_chunks, 1))
        record = {'chunk_index': i, 'claims': [], 'definitions': [],
                   'mechanisms': [], 'quantities': []}
        try:
            resp = llm.invoke([
                SystemMessage(content=_COMPREHEND_SYSTEM),
                HumanMessage(content=f'Passage:\n\n{chunk}'),
            ])
            parsed = parse_llm_json(resp.content)
            if isinstance(parsed, dict):
                record.update({k: parsed.get(k, []) for k in
                               ('claims', 'definitions', 'mechanisms', 'quantities')})
        except Exception as exc:
            log.warning(f'comprehend_chunks: chunk {i + 1}/{n_chunks} failed: {exc!r}')
        records.append(record)

    total_items = sum(len(r['claims']) + len(r['definitions']) + len(r['mechanisms']) + len(r['quantities'])
                       for r in records)
    log.info(f'comprehend_chunks: {total_items} items extracted across {n_chunks} chunks')
    return {'chunk_records': records}


# ---- Stage 4: cross-chunk merge ---------------------------------------------

_MERGE_SYSTEM = """You are merging structured comprehension records that were
extracted separately from consecutive chunks of one document, to find
connections a per-chunk read would miss.

Input: a JSON array of per-chunk records, each already tagged with its
`chunk_index`.

Perform:

1. DEDUPLICATE — merge claims/definitions that are essentially the same
   statement appearing in multiple chunks; keep the clearest text, but keep
   every source span in `spans` and every source chunk in `chunk_indices`.

2. UNDEFINED TERMS — collect terms used in `claims` or `mechanisms` text that
   never appear as a `term` in any chunk's `definitions`. These are risky to
   quiz on directly since the student was never told what they mean.

3. CROSS-CHUNK LINKS — the most valuable output of this step. Find pairs of
   items that live in DIFFERENT chunks and meaningfully connect: a mechanism
   in one chunk whose triggering condition is quantified in another, a term
   defined in one chunk and relied on by a claim in another, two claims that
   reinforce or conflict with each other. A good exam question needs two
   pieces of information from two different places in the document — this is
   where those pairs come from. Only include links that are ACTUALLY
   connected, never ones that merely share a topic. It is fine to return few
   links, or none, if the document doesn't have that many real connections.

Return JSON:
{
  "claims": [{"text": "", "spans": [""], "chunk_indices": [0]}],
  "definitions": [{"term": "", "definition": "", "spans": [""], "chunk_indices": [0]}],
  "mechanisms": [{"name": "", "steps": [""], "span": "", "chunk_index": 0}],
  "quantities": [{"value": "", "measures": "", "conditions": "", "span": "", "chunk_index": 0}],
  "undefined_terms": [""],
  "links": [
    {
      "chunk_a": 0, "item_a": "<short description of the fact in chunk_a>",
      "chunk_b": 1, "item_b": "<short description of the fact in chunk_b>",
      "relation": "<one phrase: how they connect, e.g. 'condition for the mechanism'>"
    }
  ]
}

If everything genuinely lives in one chunk (a short document), `links` will
legitimately be empty — do not force connections that aren't there.
"""


def merge_comprehension(state: PipelineState) -> dict:
    emit(state, 'Cross-referencing sections…', 0.4)
    records = state.get('chunk_records') or []
    if not records:
        return {'comprehension': _empty_comprehension()}

    llm = make_llm(temperature=0.0)
    try:
        resp = llm.invoke([
            SystemMessage(content=_MERGE_SYSTEM),
            HumanMessage(content=json.dumps(records, ensure_ascii=False)),
        ])
        merged = parse_llm_json(resp.content)
        if not isinstance(merged, dict) or not merged:
            raise ValueError('merge response was not a usable JSON object')
    except Exception as exc:
        log.warning(f'merge_comprehension: LLM merge failed ({exc!r}), falling back to naive concat')
        merged = _naive_merge(records)

    n_links = len(merged.get('links') or [])
    log.info(f'merge_comprehension: {n_links} cross-chunk links found')
    return {'comprehension': merged}


def _empty_comprehension() -> dict:
    return {'claims': [], 'definitions': [], 'mechanisms': [], 'quantities': [],
            'undefined_terms': [], 'links': []}


def _naive_merge(records: List[dict]) -> dict:
    """Fallback used if the merge LLM call fails: just concatenate everything
    with no dedup and no cross-chunk links, rather than losing the whole
    comprehension pass."""
    merged = _empty_comprehension()
    for r in records:
        idx = r.get('chunk_index', 0)
        for c in r.get('claims', []):
            merged['claims'].append({'text': c.get('text', ''), 'spans': [c.get('span', '')],
                                      'chunk_indices': [idx]})
        for d in r.get('definitions', []):
            merged['definitions'].append({'term': d.get('term', ''), 'definition': d.get('definition', ''),
                                           'spans': [d.get('span', '')], 'chunk_indices': [idx]})
        for m in r.get('mechanisms', []):
            merged['mechanisms'].append({**m, 'chunk_index': idx})
        for q in r.get('quantities', []):
            merged['quantities'].append({**q, 'chunk_index': idx})
    return merged
