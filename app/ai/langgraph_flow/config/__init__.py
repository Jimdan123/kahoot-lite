"""Tunables and provider selection for the quiz-generation pipeline.

Split by concern into sibling modules; re-exported here so existing call
sites (`from app.ai.langgraph_flow.config import X`) are unaffected.
"""
from __future__ import annotations

from app.ai.langgraph_flow.config.chunking import (
    CHUNK_WORDS,
    MAX_CHUNKS_PER_RUN,
    MAX_LINKS_PER_CHUNK,
)
from app.ai.langgraph_flow.config.enrichment import (
    HIGH_LEAK_RATIO,
    MAX_SEARCH_RESULTS,
    MIN_DEFINITIONS_FOR_ENRICHMENT,
    MIN_DRAFTS_FOR_LEAK_CHECK,
    THIN_DOCUMENT_DEFINITION_RATIO,
    has_search_enrichment,
)
from app.ai.langgraph_flow.config.generation import (
    FALLBACK_TIME_LIMIT,
    LLM_MAX_TOKENS,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_SECONDS,
    MAX_OCR_PAGES,
    MAX_RETRIES,
    MAX_TIME_LIMIT,
    MIN_ACCEPTED_QUESTIONS,
    MIN_TIME_LIMIT,
    OCR_LANGUAGES,
    OCR_RESOLUTION,
    QUESTIONS_PER_CHUNK,
)
from app.ai.langgraph_flow.config.providers import (
    DEFAULT_GROQ_MODEL,
    GROQ_MODEL_CHAIN,
    NVIDIA_API_BASE,
    NVIDIA_MODEL_CHAIN,
    OPENROUTER_API_BASE,
    OPENROUTER_MODEL_CHAIN,
    has_nvidia_fallback,
    has_openrouter_fallback,
    which_provider,
)
from app.ai.langgraph_flow.config.topic_practice import (
    MAX_PRACTICE_QUESTIONS_PER_DIFFICULTY,
    MIN_PRACTICE_QUESTIONS_PER_DIFFICULTY,
    PRACTICE_QUESTIONS_PER_DIFFICULTY,
)

__all__ = [
    'CHUNK_WORDS',
    'DEFAULT_GROQ_MODEL',
    'FALLBACK_TIME_LIMIT',
    'GROQ_MODEL_CHAIN',
    'HIGH_LEAK_RATIO',
    'LLM_MAX_TOKENS',
    'LLM_TEMPERATURE',
    'LLM_TIMEOUT_SECONDS',
    'MAX_CHUNKS_PER_RUN',
    'MAX_LINKS_PER_CHUNK',
    'MAX_OCR_PAGES',
    'MAX_PRACTICE_QUESTIONS_PER_DIFFICULTY',
    'MAX_RETRIES',
    'MAX_SEARCH_RESULTS',
    'MAX_TIME_LIMIT',
    'MIN_ACCEPTED_QUESTIONS',
    'MIN_DEFINITIONS_FOR_ENRICHMENT',
    'MIN_DRAFTS_FOR_LEAK_CHECK',
    'MIN_PRACTICE_QUESTIONS_PER_DIFFICULTY',
    'MIN_TIME_LIMIT',
    'NVIDIA_API_BASE',
    'NVIDIA_MODEL_CHAIN',
    'OCR_LANGUAGES',
    'OCR_RESOLUTION',
    'OPENROUTER_API_BASE',
    'OPENROUTER_MODEL_CHAIN',
    'PRACTICE_QUESTIONS_PER_DIFFICULTY',
    'QUESTIONS_PER_CHUNK',
    'THIN_DOCUMENT_DEFINITION_RATIO',
    'has_nvidia_fallback',
    'has_openrouter_fallback',
    'has_search_enrichment',
    'which_provider',
]
