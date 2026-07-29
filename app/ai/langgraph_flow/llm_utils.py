"""Construction of the LLM client used by every node."""
from __future__ import annotations

import os

from app.ai.langgraph_flow.config import (
    DEFAULT_GROQ_MODEL,
    DEFAULT_GROQ_VISION_MODEL,
    LLM_MAX_TOKENS,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_SECONDS,
    which_provider,
)


def make_llm(temperature: float = LLM_TEMPERATURE, vision: bool = False):
    """
    Build the Groq chat model. Requires GROQ_API_KEY. See .env.example.
    `vision=True` selects the (currently sole) vision-capable Groq model,
    used only by the scanned-PDF OCR fallback.
    """
    if which_provider() == 'groq':
        from langchain_groq import ChatGroq
        default_model = DEFAULT_GROQ_VISION_MODEL if vision else DEFAULT_GROQ_MODEL
        env_var = 'GROQ_VISION_MODEL' if vision else 'GROQ_MODEL'
        return ChatGroq(
            model=os.environ.get(env_var, default_model),
            temperature=temperature,
            max_tokens=LLM_MAX_TOKENS,
            timeout=LLM_TIMEOUT_SECONDS,
        )
    raise RuntimeError(
        'No LLM API key configured. Set GROQ_API_KEY (get one free at '
        'https://console.groq.com/keys).'
    )
