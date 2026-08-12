"""
LangGraph pipeline for Part 1.2 — PDF study material → high-quality quiz set.

Graph shape:

     extract_text
          |
     chunk_by_topic
          |
     comprehend_chunks      -- per-chunk claims/definitions/mechanisms/spans
          |
     merge_comprehension    -- dedupe + find cross-chunk links (two-hop fuel)
          |
     enrich_context         -- OPTIONAL: only if the doc looks
          |                    thin/definitional up front, needs
          |                    TAVILY_API_KEY — adds external "why"
          |                    facts (nodes/enrich.py)
          |
     practice               -- SEPARATE track: easy/med/hard practice Qs on
          |                    the topic, count chosen by the host
          |                    (nodes/practice.py)
     generate_questions <───────────────────────────┐
          |                                         │
     closed_book_check                              │  retry if too few
          |                                         │  validated Qs and
     quality_check                                  │  retry budget left
          │                                         │
          ├─ enough pass ─► save                    │
          └─ too few ─┬─ not leaking heavily ──► bump_retry ──┘
                       │
                       └─ leaking heavily AND ──► enrich_context_retry ──┘
                          not yet enriched            (same fn, retry-path
                                                        trigger — see
                                                        nodes/enrich.py)

A retry only re-enters at generate_questions — comprehension is deterministic
per document and expensive to redo, so a bad question-writing pass gets
another shot at the (unchanged) comprehension record rather than re-reading
the PDF. Retries are capped by `retry_count` in state (MAX_RETRIES).

The retry decision has a third branch beyond plain retry/save: if a failed
attempt's closed-book leak rate was high (most drafts were answerable
without the document — see nodes/enrich.py's HIGH CLOSED-BOOK LEAK RATE
trigger) and search_context hasn't been fetched yet, the retry goes through
enrich_context_retry first. Confirmed necessary against a real ADT/stack/
queue lecture where the thin-document check alone missed the failure: a
non-thin, claims-rich comprehension record still leaked 9/9 on every
attempt, because the topic itself is canonical enough that any two-hop
combination drawn from it is still guessable by a closed-book model.

`practice` runs once, is not retried, and never sets state['error'] on
failure (see nodes/practice.py) — it's additive on top of the two-hop
questions `generate`/`closed_book`/`quality` produce, not a replacement.

Requires GROQ_API_KEY in the environment. See .env.example.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from langgraph.graph import END, StateGraph

from app.ai.langgraph_flow.config import (
    MAX_RETRIES,
    MIN_ACCEPTED_QUESTIONS,
    PRACTICE_QUESTIONS_PER_DIFFICULTY,
    which_provider,
)
from app.ai.langgraph_flow.nodes.chunk import chunk_by_topic
from app.ai.langgraph_flow.nodes.comprehend import comprehend_chunks, merge_comprehension
from app.ai.langgraph_flow.nodes.critic import closed_book_check, quality_check
from app.ai.langgraph_flow.nodes.enrich import enrich_context, needs_enrichment
from app.ai.langgraph_flow.nodes.extract import extract_text
from app.ai.langgraph_flow.nodes.generate import generate_questions
from app.ai.langgraph_flow.nodes.practice import generate_practice_questions
from app.ai.langgraph_flow.nodes.save import save
from app.ai.langgraph_flow.progress import emit
from app.ai.langgraph_flow.state import PipelineState

log = logging.getLogger('kahoot.ai')


def _should_retry(state: PipelineState) -> str:
    validated = state.get('validated_questions') or []
    retries = state.get('retry_count', 0)
    if len(validated) >= MIN_ACCEPTED_QUESTIONS:
        return 'save'
    if retries >= MAX_RETRIES:
        log.warning(f'run_pipeline: saving with only {len(validated)} accepted question(s), '
                    f'below MIN_ACCEPTED_QUESTIONS={MIN_ACCEPTED_QUESTIONS}, after exhausting '
                    f'{retries} retries — quota scaling in generate.py could not make up the '
                    f'shortfall for this document')
        return 'save'
    # A failed attempt with a high closed-book leak rate gets one shot at
    # external WHY context before the plain retry — see nodes/enrich.py's
    # HIGH CLOSED-BOOK LEAK RATE trigger. Skipped once search_context is
    # already populated (either this fired earlier, or the proactive
    # thin-doc check already did) so a run never searches twice.
    if not state.get('search_context') and needs_enrichment(state):
        return 'enrich_then_retry'
    return 'retry'


def _resolve_user_llm_keys(owner_id: int) -> dict:
    """Query + decrypt this user's saved BYOK keys for the 4 known
    providers once per run (not once per node — see
    PipelineState['user_llm_keys']). Custom-provider rows are handled
    separately by _resolve_custom_providers below — the is_custom=False
    filter keeps this resolver from wastefully decrypting (and building a
    dead dict entry for) a row it would never use anyway. A row that fails
    to decrypt (e.g. API_KEY_ENCRYPTION_KEY was rotated/lost since it was
    saved) is logged and dropped rather than failing the whole run —
    consistent with this pipeline's "one bad input doesn't kill the run"
    philosophy elsewhere (generate.py/critic.py's per-item try/except)."""
    from app.crypto_utils import DecryptionError
    from app.models import UserApiKey
    keys = {}
    for row in UserApiKey.query.filter_by(user_id=owner_id, is_custom=False).all():
        try:
            keys[row.provider] = row.get_key()
        except DecryptionError as exc:
            log.warning(f'run_pipeline: could not decrypt saved {row.provider} key for '
                        f'user {owner_id} ({exc}); skipping it for this run')
    return keys


def _resolve_custom_providers(owner_id: int) -> list:
    """Query + decrypt this user's saved custom-provider rows once per run
    (mirrors _resolve_user_llm_keys above). A row missing base_url or a
    usable model_name, one that fails to decrypt, or one whose base_url
    now fails the SSRF check, is logged and dropped rather than failing
    the whole run — same "one bad input doesn't kill the run" philosophy.

    The SSRF re-check here (not just at save time in CustomProviderForm)
    matters against DNS rebinding: a hostname could resolve to a public IP
    when the row was saved, then be repointed at an internal address
    before a pipeline run actually uses it — see
    app/ssrf_protection.py's module docstring for the full threat model
    and the residual TOCTOU gap this narrows but doesn't fully close."""
    from app.crypto_utils import DecryptionError
    from app.models import UserApiKey
    from app.ssrf_protection import SSRFError, validate_public_https_url
    providers = []
    rows = (UserApiKey.query
            .filter_by(user_id=owner_id, is_custom=True)
            .order_by(UserApiKey.id)
            .all())
    for row in rows:
        models = [m.strip() for m in (row.model_name or '').split(',') if m.strip()]
        if not row.base_url or not models:
            log.warning(f'run_pipeline: skipping malformed custom provider {row.provider!r} '
                        f'for user {owner_id} (missing base_url or model_name)')
            continue
        try:
            validate_public_https_url(row.base_url)
        except SSRFError as exc:
            log.warning(f'run_pipeline: custom provider {row.provider!r} for user {owner_id} '
                        f'failed the SSRF check at run time ({exc}); skipping it for this run')
            continue
        try:
            api_key = row.get_key()
        except DecryptionError as exc:
            log.warning(f'run_pipeline: could not decrypt saved custom provider {row.provider!r} '
                        f'key for user {owner_id} ({exc}); skipping it for this run')
            continue
        providers.append({'label': row.provider, 'base_url': row.base_url,
                           'models': models, 'api_key': api_key})
    return providers


def _bump_retry(state: PipelineState) -> dict:
    emit(state, 'Not enough good questions — retrying…', 0.8)
    return {'retry_count': state.get('retry_count', 0) + 1}


def _build_graph():
    graph = StateGraph(PipelineState)
    graph.add_node('extract', extract_text)
    graph.add_node('chunk', chunk_by_topic)
    graph.add_node('comprehend', comprehend_chunks)
    graph.add_node('merge_comprehension', merge_comprehension)
    graph.add_node('enrich_context', enrich_context)
    # Same function, registered under a second name for the retry-path
    # trigger — see nodes/enrich.py's module docstring for why one node
    # can't serve both call sites (different outgoing edges).
    graph.add_node('enrich_context_retry', enrich_context)
    graph.add_node('practice', generate_practice_questions)
    graph.add_node('generate', generate_questions)
    graph.add_node('closed_book', closed_book_check)
    graph.add_node('quality', quality_check)
    graph.add_node('bump_retry', _bump_retry)
    graph.add_node('save', save)

    graph.set_entry_point('extract')
    graph.add_edge('extract', 'chunk')
    graph.add_edge('chunk', 'comprehend')
    graph.add_edge('comprehend', 'merge_comprehension')
    graph.add_edge('merge_comprehension', 'enrich_context')
    graph.add_edge('enrich_context', 'practice')
    graph.add_edge('practice', 'generate')
    graph.add_edge('generate', 'closed_book')
    graph.add_edge('closed_book', 'quality')
    graph.add_conditional_edges('quality', _should_retry, {
        'enrich_then_retry': 'enrich_context_retry',
        'retry': 'bump_retry',
        'save': 'save',
    })
    graph.add_edge('enrich_context_retry', 'bump_retry')
    graph.add_edge('bump_retry', 'generate')
    graph.add_edge('save', END)
    return graph.compile()


_COMPILED_GRAPH = None


def _graph():
    global _COMPILED_GRAPH
    if _COMPILED_GRAPH is None:
        _COMPILED_GRAPH = _build_graph()
    return _COMPILED_GRAPH


def run_pipeline(
    pdf_path: str,
    owner_id: int,
    quiz_name: str = '',
    quiz_description: str = '',
    practice_questions_per_difficulty: int = PRACTICE_QUESTIONS_PER_DIFFICULTY,
    progress_cb: Optional[Callable[[str, float], None]] = None,
) -> int:
    """Run the pipeline synchronously and return the created QuestionSet id.

    Meant to be called from a background task — total runtime is on the
    order of tens of seconds for a small PDF, minutes for a large one.
    Raises RuntimeError on any fatal state['error'].
    """
    user_llm_keys = _resolve_user_llm_keys(owner_id)
    custom_llm_providers = _resolve_custom_providers(owner_id)
    if not user_llm_keys and not custom_llm_providers and which_provider() is None:
        # Message deliberately doesn't name the server's specific provider —
        # this reaches the end user via the failed-job UI (app/ai/routes.py's
        # _worker() catches this and stores str(exc) as job.error). An
        # operator setting up their own deployment gets the actual env var
        # name from .env.example/README instead.
        raise RuntimeError(
            'No AI provider is configured. Add your own key on the API Keys '
            'page, or ask the site operator to configure one.'
        )
    initial: PipelineState = {
        'pdf_path': pdf_path,
        'owner_id': owner_id,
        'quiz_name': quiz_name,
        'quiz_description': quiz_description,
        'practice_questions_per_difficulty': practice_questions_per_difficulty,
        'retry_count': 0,
        'progress_cb': progress_cb,
        'user_llm_keys': user_llm_keys,
        'custom_llm_providers': custom_llm_providers,
        'token_usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
    }
    log.info(f'run_pipeline starting: pdf={pdf_path} owner={owner_id}')
    final = _graph().invoke(initial)
    if final.get('error'):
        log.error(f'run_pipeline error: {final["error"]}')
        raise RuntimeError(final['error'])
    log.info(f'run_pipeline done: question_set_id={final.get("question_set_id")}')
    return final['question_set_id']
