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
     generate_questions <───────────┐
          |                         │
     closed_book_check              │  retry if too few
          |                         │  validated Qs and
     quality_check                  │  retry budget left
          │                         │
          ├─ enough pass ─► save    │
          └─ too few ─────────────► bump_retry ──┘

A retry only re-enters at generate_questions — comprehension is deterministic
per document and expensive to redo, so a bad question-writing pass gets
another shot at the (unchanged) comprehension record rather than re-reading
the PDF. Retries are capped by `retry_count` in state (MAX_RETRIES).

Requires GROQ_API_KEY in the environment. See .env.example.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from langgraph.graph import END, StateGraph

from app.ai.langgraph_flow.config import MAX_RETRIES, MIN_ACCEPTED_QUESTIONS, which_provider
from app.ai.langgraph_flow.nodes.chunk import chunk_by_topic
from app.ai.langgraph_flow.nodes.comprehend import comprehend_chunks, merge_comprehension
from app.ai.langgraph_flow.nodes.critic import closed_book_check, quality_check
from app.ai.langgraph_flow.nodes.extract import extract_text
from app.ai.langgraph_flow.nodes.generate import generate_questions
from app.ai.langgraph_flow.nodes.save import save
from app.ai.langgraph_flow.progress import emit
from app.ai.langgraph_flow.state import PipelineState

log = logging.getLogger('kahoot.ai')


def _should_retry(state: PipelineState) -> str:
    validated = state.get('validated_questions') or []
    retries = state.get('retry_count', 0)
    if len(validated) < MIN_ACCEPTED_QUESTIONS and retries < MAX_RETRIES:
        return 'retry'
    return 'save'


def _bump_retry(state: PipelineState) -> dict:
    emit(state, 'Not enough good questions — retrying…', 0.8)
    return {'retry_count': state.get('retry_count', 0) + 1}


def _build_graph():
    graph = StateGraph(PipelineState)
    graph.add_node('extract', extract_text)
    graph.add_node('chunk', chunk_by_topic)
    graph.add_node('comprehend', comprehend_chunks)
    graph.add_node('merge_comprehension', merge_comprehension)
    graph.add_node('generate', generate_questions)
    graph.add_node('closed_book', closed_book_check)
    graph.add_node('quality', quality_check)
    graph.add_node('bump_retry', _bump_retry)
    graph.add_node('save', save)

    graph.set_entry_point('extract')
    graph.add_edge('extract', 'chunk')
    graph.add_edge('chunk', 'comprehend')
    graph.add_edge('comprehend', 'merge_comprehension')
    graph.add_edge('merge_comprehension', 'generate')
    graph.add_edge('generate', 'closed_book')
    graph.add_edge('closed_book', 'quality')
    graph.add_conditional_edges('quality', _should_retry, {
        'retry': 'bump_retry',
        'save': 'save',
    })
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
    progress_cb: Optional[Callable[[str, float], None]] = None,
) -> int:
    """Run the pipeline synchronously and return the created QuestionSet id.

    Meant to be called from a background task — total runtime is on the
    order of tens of seconds for a small PDF, minutes for a large one.
    Raises RuntimeError on any fatal state['error'].
    """
    if which_provider() is None:
        raise RuntimeError(
            'No LLM API key configured. Set GROQ_API_KEY (free at '
            'https://console.groq.com/keys).'
        )
    initial: PipelineState = {
        'pdf_path': pdf_path,
        'owner_id': owner_id,
        'quiz_name': quiz_name,
        'quiz_description': quiz_description,
        'retry_count': 0,
        'progress_cb': progress_cb,
    }
    log.info(f'run_pipeline starting: pdf={pdf_path} owner={owner_id}')
    final = _graph().invoke(initial)
    if final.get('error'):
        log.error(f'run_pipeline error: {final["error"]}')
        raise RuntimeError(final['error'])
    log.info(f'run_pipeline done: question_set_id={final.get("question_set_id")}')
    return final['question_set_id']
