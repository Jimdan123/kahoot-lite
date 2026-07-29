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
# Time limit is decided per-question by the LLM (a multi-step stats
# calculation needs longer than a one-line concept check) — these are only
# a sanity clamp on whatever it comes back with, never the value itself.
MIN_TIME_LIMIT = 10                      # seconds
MAX_TIME_LIMIT = 120                     # seconds
FALLBACK_TIME_LIMIT = 30                 # used only if the LLM omits/mangles time_limit
LLM_TEMPERATURE = 0.4                    # bit of variety for retry to actually differ
LLM_TIMEOUT_SECONDS = 45                 # cap on any single LLM call
MAX_OCR_PAGES = 15                       # cap on pages OCR'd per document (bounds per-job
                                          # CPU time — local Tesseract has no per-page cost)
OCR_RESOLUTION = 300                     # dpi for page-image rendering; Tesseract's
                                          # recommended default for good accuracy
OCR_LANGUAGES = 'eng+vie'                # tesseract language data — needs the matching
                                          # tesseract-ocr-eng/tesseract-ocr-vie packages
LLM_MAX_TOKENS = 4096                    # generous headroom for 3 MCQs w/ math notation —
                                          # 2048 was tight enough to truncate mid-question

# Groq — see https://console.groq.com/docs/models for the current lineup;
# check there before changing these, models get deprecated/renamed over time.
DEFAULT_GROQ_MODEL = 'llama-3.3-70b-versatile'         # text generation + grading

# Fallback order when a model's rate limit or daily token quota is
# exhausted (groq.RateLimitError) or the org's Groq console doesn't have it
# enabled (groq.PermissionDeniedError) — see llm_utils._RotatingLLM.
# Keep this in sync with Settings -> Limits in the Groq console: only
# models with a green checkmark there are actually callable for this org.
# Override with the GROQ_MODEL_CHAIN env var (comma-separated) without a
# code change if the org's allowed models change.
GROQ_MODEL_CHAIN = ['llama-3.3-70b-versatile', 'qwen/qwen3.6-27b']

# NVIDIA NIM (build.nvidia.com) — optional last-resort fallback tier, used
# only if BOTH Groq models above are exhausted/blocked. Entirely optional:
# if NVIDIA_API_KEY isn't set, the rotation chain simply stops at Groq.
# OpenAI-compatible endpoint, see llm_utils.py. Free API key at
# https://build.nvidia.com — override the model via NVIDIA_MODEL env var.
NVIDIA_API_BASE = 'https://integrate.api.nvidia.com/v1'
# meta/llama-3.3-70b-instruct is in the NIM catalog but hangs/times out on
# this account (verified — 120s+, no response); nemotron responds in ~1-15s.
NVIDIA_MODEL = 'nvidia/llama-3.3-nemotron-super-49b-v1'

# ---- Topic-practice track ------------------------------------------------
# Separate from the document's own two-hop comprehension questions (see
# nodes/practice.py).
PRACTICE_QUESTIONS_PER_DIFFICULTY = 2     # easy, medium, hard each get this many


def which_provider() -> Optional[str]:
    if os.environ.get('GROQ_API_KEY'):
        return 'groq'
    return None


def has_nvidia_fallback() -> bool:
    return bool(os.environ.get('NVIDIA_API_KEY'))
