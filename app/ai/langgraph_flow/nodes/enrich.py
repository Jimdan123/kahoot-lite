"""Stage 4.4 (optional) — enrich documents with external "why does this
matter" context before question generation, or before a retry.

Two INDEPENDENT triggers decide whether this node actually does anything:

1. THIN DOCUMENT (checked before the first generate attempt) — a source
   document (e.g. an ADT/data-structures lecture that's mostly "here's the
   formal definition, here's the informal definition") is almost entirely
   DEFINITIONS with few claims/mechanisms/quantities to pair them against.
   generate.py's two-hop rule then has nothing to work with except
   definition-definition pairs, which it correctly refuses to write (see
   MIX YOUR REFERENCE TYPES in generate.py).

2. HIGH CLOSED-BOOK LEAK RATE (checked before a retry, once a real attempt
   exists) — confirmed against a real UBC CPSC221 ADT/stack/queue slide
   deck: even a NON-thin comprehension record (24 non-definition items,
   only 20% definitions) still produced a 9/9 closed-book leak rate on
   every attempt, because the topic is canonical enough that any two-hop
   combination drawn from it is still guessable by a general-purpose LLM.
   The thin-document check alone misses this case entirely — it only looks
   at what KIND of facts the record has, not whether combining them
   actually produces something a closed-book model can't already answer.

Either signal makes generate.py's two-hop rule and critic.py's
closed-book-leak check reject nearly everything, producing zero validated
questions. This node gives those documents a THIRD kind of hop material:
short external "why/reason" facts on the document's topic (why the concept
matters, how it's used in practice), fetched via Tavily web search and
distilled into a small structured list by one LLM call. generate.py then
gets a new, fifth pairing option — a document DEFINITION plus one of these
WHY facts — without ever letting BOTH hops come from outside the document
(see the HARD RULE in generate.py's system prompt: at least one hop must
always come from the document itself).

Registered as TWO graph nodes pointing at the same `enrich_context`
function (see graph.py): one before the first `generate` attempt (checks
trigger 1; trigger 2 has no data yet and is always False there), one in the
retry path after a failed `quality_check` (checks both; trigger 2 now has
real `draft_questions`/`closed_book_results` to look at). Whichever fires
first sets `search_context` for the rest of the run — a second visit is a
no-op (see the `state.get('search_context')` guard below) so a run never
pays for more than one search.

Fully optional, two independent ways to short-circuit to a free no-op:
  - Only runs at all if TAVILY_API_KEY is set (config.has_search_enrichment())
    — the exact same optional-tier pattern as NVIDIA_API_KEY/
    OPENROUTER_API_KEY in llm_utils.py. Unset means this node is a
    zero-cost pass-through returning {'search_context': []}.
  - Only does search+distill work if `needs_enrichment()` says yes — a
    document that's neither thin nor leaking heavily gains nothing from
    this and shouldn't pay the extra cost.

Best-effort like merge_comprehension's naive-merge fallback and
practice.py's track: any failure here — a Tavily error, a malformed
distillation response — degrades to an empty search_context rather than
touching state['error']. generate.py and critic.py behave exactly as they
did before this node existed whenever search_context is empty, so a
broken/missing Tavily key can never break the pipeline, only leave it
exactly as capable as it was before this feature.
"""
from __future__ import annotations

import logging
import os
from typing import List

from app.ai.langgraph_flow.config import (
    HIGH_LEAK_RATIO,
    MAX_SEARCH_RESULTS,
    MIN_DEFINITIONS_FOR_ENRICHMENT,
    MIN_DRAFTS_FOR_LEAK_CHECK,
    THIN_DOCUMENT_DEFINITION_RATIO,
    has_search_enrichment,
)
from app.ai.langgraph_flow.llm_utils import invoke_json, make_llm
from app.ai.langgraph_flow.progress import emit
from app.ai.langgraph_flow.state import PipelineState
from langchain_core.messages import HumanMessage, SystemMessage

log = logging.getLogger('kahoot.ai')


_DISTILL_SYSTEM = """You are given raw web search snippets about a topic a
student is studying, plus the topic itself. Distill them into a short list
of "why/reason" facts — the kind of context a good teacher adds on top of a
bare definition to explain why a concept matters or how it's actually used
in practice. The student's source document already defines these terms; do
NOT restate a definition, only add what the document does not already say.

Each fact must be:
  - genuinely explanatory — why/how/when something is used, a real
    consequence or trade-off, a concrete real-world application — NOT a
    rephrased dictionary definition,
  - traceable to the snippets you were given; do not invent anything the
    snippets don't support,
  - short: one or two sentences.

Return JSON:
{
  "facts": [
    {"topic_term": "<the specific term/concept this fact is about>",
     "fact": "<the why/reason fact itself>"}
  ]
}

Return at most 6 facts. Fewer, high-quality facts beat padding. If the
snippets don't actually support any genuine why/reason fact, return an
empty list rather than inventing one.
"""


def enrich_context(state: PipelineState) -> dict:
    if not has_search_enrichment():
        log.info('enrich_context: TAVILY_API_KEY not set, skipping')
        return {'search_context': []}

    if state.get('search_context'):
        # Already enriched earlier this run (the proactive thin-doc check
        # fired, or an earlier retry already triggered this) — never spend
        # a second search on the same document.
        return {}

    comprehension = state.get('comprehension') or {}
    thin = _is_thin(comprehension)
    high_leak = _high_leak_rate(state)
    if not (thin or high_leak):
        return {'search_context': []}

    topic = (comprehension.get('topic') or '').strip()
    if not topic:
        log.info('enrich_context: document needs enrichment but no topic available, skipping search')
        return {'search_context': []}

    reason = 'mostly definitions' if thin else 'still answerable without the document'
    progress = 0.41 if thin else 0.8
    emit(state, f'Document is {reason} — searching for context…', progress)

    query = f'why does {topic} matter, real-world use and importance'
    try:
        snippets = _tavily_search(query)
    except Exception as exc:
        log.warning(f'enrich_context: Tavily search failed ({exc!r}), skipping enrichment')
        return {'search_context': []}
    if not snippets:
        log.info('enrich_context: Tavily returned no results, skipping enrichment')
        return {'search_context': []}

    try:
        llm = make_llm(temperature=0.0, user_keys=state.get('user_llm_keys'))  # distillation should be deterministic, not creative
        resp, parsed = invoke_json(llm, [
            SystemMessage(content=_DISTILL_SYSTEM),
            HumanMessage(content=f'Topic: {topic}\n\nSnippets:\n' +
                                  '\n'.join(f'- {s}' for s in snippets)),
        ], token_usage=state.get('token_usage'))
        facts = parsed.get('facts', []) if isinstance(parsed, dict) else []
        facts = [f for f in facts
                 if isinstance(f, dict) and (f.get('fact') or '').strip()]
    except Exception as exc:
        log.warning(f'enrich_context: distillation LLM call failed ({exc!r}), skipping enrichment')
        return {'search_context': []}

    log.info(f'enrich_context: {len(facts)} why/reason facts distilled from '
              f'{len(snippets)} search snippets for topic {topic!r}')
    return {'search_context': facts}


def _is_thin(comprehension: dict) -> bool:
    """A document is 'thin/definitional' if definitions dominate the
    record with little else to pair them against — see
    THIN_DOCUMENT_DEFINITION_RATIO/MIN_DEFINITIONS_FOR_ENRICHMENT in
    config.py. Computed here rather than in merge_comprehension because
    it's a pure derived signal used by exactly one consumer."""
    n_defs = len(comprehension.get('definitions') or [])
    n_substance = (len(comprehension.get('claims') or [])
                   + len(comprehension.get('mechanisms') or [])
                   + len(comprehension.get('quantities') or []))
    total = n_defs + n_substance
    if total == 0 or n_defs < MIN_DEFINITIONS_FOR_ENRICHMENT:
        return False
    ratio = n_defs / total
    is_thin = ratio >= THIN_DOCUMENT_DEFINITION_RATIO
    log.info(f'enrich_context: {n_defs} definitions / {total} total items '
              f'(ratio={ratio:.2f}) — thin={is_thin}')
    return is_thin


def _high_leak_rate(state: PipelineState) -> bool:
    """True if the most recent generate attempt's drafts were mostly
    answerable without the document — see HIGH_LEAK_RATIO/
    MIN_DRAFTS_FOR_LEAK_CHECK in config.py. Empty/mismatched state (no
    attempt has happened yet) safely returns False, so this only ever
    fires on a retry, never on the very first pass."""
    drafts = state.get('draft_questions') or []
    results = state.get('closed_book_results') or []
    if len(drafts) < MIN_DRAFTS_FOR_LEAK_CHECK or len(results) != len(drafts):
        return False
    leaked = sum(1 for r, d in zip(results, drafts)
                 if isinstance(r, dict) and r.get('confidence') in ('high', 'medium')
                 and r.get('answer') == d.get('correct'))
    ratio = leaked / len(drafts)
    is_high = ratio >= HIGH_LEAK_RATIO
    log.info(f'enrich_context: closed-book leak rate {leaked}/{len(drafts)} '
              f'(ratio={ratio:.2f}) — high_leak={is_high}')
    return is_high


def needs_enrichment(state: PipelineState) -> bool:
    """True if either trigger says this document could use external WHY
    context — thin/definitional up front, or a high closed-book leak rate
    on the most recent attempt. Shared by enrich_context itself (decides
    whether to spend a search) and graph.py's retry routing (decides
    whether to visit the retry-path enrich_context node at all)."""
    comprehension = state.get('comprehension') or {}
    return _is_thin(comprehension) or _high_leak_rate(state)


def _tavily_search(query: str) -> List[str]:
    """Best-effort outbound Tavily call, isolated so enrich_context's own
    try/except only has to handle "something about search failed" rather
    than every tavily-python exception type individually."""
    from tavily import TavilyClient
    client = TavilyClient(api_key=os.environ['TAVILY_API_KEY'])
    response = client.search(query=query, max_results=MAX_SEARCH_RESULTS, search_depth='basic')
    return [r.get('content', '') for r in (response.get('results') or []) if r.get('content')]
