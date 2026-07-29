"""Tunables and provider selection for the quiz-generation pipeline."""
from __future__ import annotations

import os
from typing import Optional

# ---- Chunking ---------------------------------------------------------------

CHUNK_WORDS = 500                        # ~500 words per chunk
MAX_CHUNKS_PER_RUN = 20                  # hard cap so a big PDF doesn't cost a fortune

# ---- Comprehension ------------------------------------------------------

MAX_LINKS_PER_CHUNK = 2                  # cross-chunk links fed into generate per chunk

# ---- Question generation -----------------------------------------------

QUESTIONS_PER_CHUNK = 3                  # asked of the LLM per chunk
MIN_ACCEPTED_QUESTIONS = 5               # else we retry the generate step
MAX_RETRIES = 2
DEFAULT_TIME_LIMIT = 20                  # seconds per generated question
LLM_TEMPERATURE = 0.4                    # bit of variety for retry to actually differ
LLM_TIMEOUT_SECONDS = 45                 # cap on any single LLM call
MAX_OCR_PAGES = 15                       # cap on pages sent through vision-model OCR
OCR_RESOLUTION = 150                     # dpi for page-image rendering
LLM_MAX_TOKENS = 4096                    # generous headroom for 3 MCQs w/ math notation —
                                          # 2048 was tight enough to truncate mid-question

# Groq — see https://console.groq.com/docs/models for the current lineup;
# check there before changing these, models get deprecated/renamed over time.
DEFAULT_GROQ_MODEL = 'llama-3.3-70b-versatile'         # text generation + grading
DEFAULT_GROQ_VISION_MODEL = 'qwen/qwen3.6-27b'         # only vision-capable model on Groq currently


def which_provider() -> Optional[str]:
    if os.environ.get('GROQ_API_KEY'):
        return 'groq'
    return None
