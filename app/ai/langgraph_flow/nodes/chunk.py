"""Stage 2 — split the extracted text into bounded word-count chunks.

extract.py appends [Table: ...]/[Formula: ...]/[Figure: ...] annotation
blocks inline into raw_text (see nodes/extract.py's module docstring).
Plain word-count slicing has no awareness of those blocks and can bisect
one mid-way (e.g. a table cut apart mid-row) — _split_into_segments/
_build_chunks below treat each block as an atomic unit that always lands
intact inside a single chunk, while preserving the original slicing
behavior exactly for text with no annotation blocks (see the equivalence
test in tests/test_chunk_segments.py).
"""
from __future__ import annotations

import logging
import re

from app.ai.langgraph_flow.config import CHUNK_WORDS, MAX_CHUNKS_PER_RUN
from app.ai.langgraph_flow.progress import emit
from app.ai.langgraph_flow.state import PipelineState

log = logging.getLogger('kahoot.ai')

# Matches a whole annotation block extract.py appends. The Table branch's
# lazy `.*?` is safe from early termination: _table_to_markdown's `_cell()`
# helper sanitizes every cell (strips embedded newlines, escapes `|`)
# before building each row, so no cell content can ever produce an
# intermediate line that is just `]` — the match can only end at the
# table's own real closing-bracket line. The Formula/Figure branch only
# matches a block that stays on one line (their normal shape, since
# vision_utils.py's prompts request single-line/short output) — a
# response with an embedded newline simply won't match and falls back to
# ordinary word-level tokenization for that block, no crash.
_ANNOTATION_BLOCK_RE = re.compile(
    r'^\[Table:.*?\n\]$'
    r'|^\[(?:Formula|Figure): [^\n]*\]$',
    re.MULTILINE | re.DOTALL,
)


def _split_into_segments(raw_text: str) -> list[tuple[str, str]]:
    """Split raw_text into an ordered sequence of ('block', text) /
    ('text', text) segments. 'block' entries are whole annotation blocks
    (never split across a chunk boundary); 'text' entries are everything
    else (chunked at plain word granularity, as before). Invariant:
    concatenating every segment's text in order reconstructs raw_text
    exactly."""
    segments = []
    pos = 0
    for m in _ANNOTATION_BLOCK_RE.finditer(raw_text):
        if m.start() > pos:
            segments.append(('text', raw_text[pos:m.start()]))
        segments.append(('block', m.group(0)))
        pos = m.end()
    if pos < len(raw_text):
        segments.append(('text', raw_text[pos:]))
    return segments


def _build_chunks(segments: list[tuple[str, str]]) -> list[tuple[str, bool]]:
    """Accumulate segments into ~CHUNK_WORDS-word chunks. A 'block'
    segment is atomic — appended whole, never split. Returns
    (chunk_text, is_single_block) pairs; is_single_block is True only
    when a chunk consists of exactly one atomic block and nothing else,
    so callers can exempt a genuinely meaningful annotation block from a
    word-count floor that was built to reject terse prose fragments, not
    real content.

    Exact policy:
      - If adding a block to the current (non-empty) chunk would push it
        over CHUNK_WORDS, the current chunk is flushed FIRST (ending up
        under CHUNK_WORDS, which is fine), then the block starts a fresh
        chunk.
      - A block that alone is >= CHUNK_WORDS words becomes its own
        oversized chunk (flushed immediately after being added) rather
        than ever being truncated.
      - Plain-text words accumulate and flush at exactly CHUNK_WORDS,
        identical to the original slicing behavior when no blocks are
        present (see the equivalence test)."""
    chunks: list[tuple[str, bool]] = []
    current: list[str] = []
    current_count = 0
    current_is_single_block = False

    def flush():
        nonlocal current, current_count, current_is_single_block
        if current:
            chunks.append((' '.join(current), current_is_single_block))
        current, current_count, current_is_single_block = [], 0, False

    for kind, text in segments:
        if kind == 'block':
            block_wc = len(text.split())
            if current_count and current_count + block_wc > CHUNK_WORDS:
                flush()
            current_is_single_block = current_count == 0
            current.append(text)
            current_count += block_wc
            if current_count >= CHUNK_WORDS:
                flush()
        else:
            for word in text.split():
                current.append(word)
                current_count += 1
                current_is_single_block = False
                if current_count >= CHUNK_WORDS:
                    flush()
    flush()
    return chunks


def chunk_by_topic(state: PipelineState) -> dict:
    emit(state, 'Splitting into sections…', 0.15)
    segments = _split_into_segments(state['raw_text'])
    built = _build_chunks(segments)
    # A single-block chunk (a whole [Table:]/[Formula:]/[Figure:] block
    # landing alone) is exempted from the word-count floor below — it's
    # inherently meaningful regardless of raw word count, unlike the
    # terse prose fragments the floor was built to reject.
    chunks = [c for c, is_single_block in built if is_single_block or len(c.split()) > 40]
    # Hard cap so a 100-page PDF doesn't burn 100 LLM calls
    chunks = chunks[:MAX_CHUNKS_PER_RUN]
    log.info(f'chunk_by_topic produced {len(chunks)} chunks')
    if not chunks:
        return {'error': 'PDF text too short to generate questions from '
                         '(need at least a paragraph of extractable text).'}
    return {'chunks': chunks}
