"""Stage 2 — split the extracted text into bounded word-count chunks."""
from __future__ import annotations

import logging

from app.ai.langgraph_flow.config import CHUNK_WORDS, MAX_CHUNKS_PER_RUN
from app.ai.langgraph_flow.progress import emit
from app.ai.langgraph_flow.state import PipelineState

log = logging.getLogger('kahoot.ai')


def chunk_by_topic(state: PipelineState) -> dict:
    emit(state, 'Splitting into sections…', 0.15)
    words = state['raw_text'].split()
    chunks = [
        ' '.join(words[i:i + CHUNK_WORDS])
        for i in range(0, len(words), CHUNK_WORDS)
    ]
    chunks = [c for c in chunks if len(c.split()) > 40]
    # Hard cap so a 100-page PDF doesn't burn 100 LLM calls
    chunks = chunks[:MAX_CHUNKS_PER_RUN]
    log.info(f'chunk_by_topic produced {len(chunks)} chunks')
    if not chunks:
        return {'error': 'PDF text too short to generate questions from '
                         '(need at least a paragraph of extractable text).'}
    return {'chunks': chunks}
