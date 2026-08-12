"""Question-generation and OCR tunables for the quiz-generation pipeline."""
from __future__ import annotations

QUESTIONS_PER_CHUNK = 3                  # baseline asked of the LLM per chunk
# Per-chunk quota is scaled up from the baseline for short documents and for
# chunks that score as more important (see nodes/generate.py's
# _allocate_quotas) — this caps how high either adjustment can push a single
# chunk's quota, so a one-paragraph PDF doesn't get asked for 15 questions
# from the same short passage.
MAX_QUESTIONS_PER_CHUNK = 6
# generate_questions targets a total draft pool of roughly
# MIN_ACCEPTED_QUESTIONS * this factor, spread across chunks — a safety
# margin against quality_check's own rejection rate (structural issues,
# decorative hops, ambiguity, dedup). Confirmed necessary live: a 3-chunk
# document with no scaling settled at 3 accepted questions, under its own
# 5-question floor, after exhausting every retry.
SHORT_DOC_QUOTA_SAFETY_FACTOR = 2.0
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
