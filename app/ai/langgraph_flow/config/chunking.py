"""Chunking tunables for the quiz-generation pipeline."""
from __future__ import annotations

CHUNK_WORDS = 500                        # ~500 words per chunk
MAX_CHUNKS_PER_RUN = 20                  # hard cap so a big PDF doesn't cost a fortune

# ---- Comprehension ------------------------------------------------------

MAX_LINKS_PER_CHUNK = 2                  # cross-chunk links fed into generate per chunk
