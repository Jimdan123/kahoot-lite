"""Construction of the LLM client used by every node."""
from __future__ import annotations

import logging
import os

from app.ai.langgraph_flow.config import (
    GROQ_MODEL_CHAIN,
    LLM_MAX_TOKENS,
    LLM_TIMEOUT_SECONDS,
    LLM_TEMPERATURE,
    NVIDIA_API_BASE,
    NVIDIA_MODEL,
    has_nvidia_fallback,
    which_provider,
)

log = logging.getLogger('kahoot.ai')


class _RotatingLLM:
    """Wraps a chain of (provider, model) pairs and falls back to the next
    one if the current one's rate limit / daily token quota is exhausted,
    the org's console doesn't have it enabled, or (NVIDIA only) the API key
    is missing/invalid — groq.RateLimitError/PermissionDeniedError or the
    equivalent openai.* errors (NVIDIA's endpoint is OpenAI-compatible).

    Once a call falls back to a later entry, this instance keeps using it
    for the rest of its calls instead of re-hitting the exhausted one every
    time — but a fresh make_llm() call (the next pipeline node) starts back
    at the front of the chain, since per-minute limits may have reset by
    then.
    """

    def __init__(self, chain: list, temperature: float, timeout: float):
        self._chain = chain  # list of (provider, model) tuples
        self._temperature = temperature
        self._timeout = timeout
        self._clients: dict = {}
        self._idx = 0

    def _client_for(self, idx: int):
        provider, model = self._chain[idx]
        key = (provider, model)
        if key not in self._clients:
            if provider == 'groq':
                from langchain_groq import ChatGroq
                self._clients[key] = ChatGroq(
                    model=model,
                    temperature=self._temperature,
                    max_tokens=LLM_MAX_TOKENS,
                    timeout=self._timeout,
                )
            elif provider == 'nvidia':
                from langchain_openai import ChatOpenAI
                self._clients[key] = ChatOpenAI(
                    model=model,
                    base_url=NVIDIA_API_BASE,
                    api_key=os.environ.get('NVIDIA_API_KEY'),
                    temperature=self._temperature,
                    max_tokens=LLM_MAX_TOKENS,
                    timeout=self._timeout,
                )
            else:
                raise ValueError(f'unknown LLM provider {provider!r}')
        return self._clients[key]

    def invoke(self, messages):
        import groq
        import openai
        transient = (groq.RateLimitError, groq.PermissionDeniedError,
                     openai.RateLimitError, openai.PermissionDeniedError, openai.AuthenticationError)
        last_exc = None
        for idx in range(self._idx, len(self._chain)):
            provider, model = self._chain[idx]
            try:
                resp = self._client_for(idx).invoke(messages)
            except transient as exc:
                last_exc = exc
                log.warning(f'llm rotation: {provider}:{model} unavailable '
                            f'({type(exc).__name__}: {exc}), trying next in chain')
                continue
            if idx != self._idx:
                log.warning(f'llm rotation: switched to {provider}:{model} '
                            f'(chain index {idx}) for the rest of this call')
                self._idx = idx
            return resp
        raise last_exc


def make_llm(temperature: float = LLM_TEMPERATURE, model: str = None, timeout: float = None):
    """Build the LLM client used by pipeline nodes. Requires GROQ_API_KEY.
    See .env.example.

    `model` pins one specific Groq model, bypassing rotation — use only
    when a node genuinely needs a particular model. Without it, returns a
    client that transparently rotates through GROQ_MODEL_CHAIN (or the
    GROQ_MODEL_CHAIN env var, comma-separated), then — if NVIDIA_API_KEY is
    set — falls back to NVIDIA NIM as a last resort once every Groq model
    in the chain is exhausted/blocked.
    """
    if which_provider() != 'groq':
        raise RuntimeError(
            'No LLM API key configured. Set GROQ_API_KEY (get one free at '
            'https://console.groq.com/keys).'
        )
    resolved_timeout = timeout or LLM_TIMEOUT_SECONDS
    if model:
        from langchain_groq import ChatGroq
        return ChatGroq(
            model=model,
            temperature=temperature,
            max_tokens=LLM_MAX_TOKENS,
            timeout=resolved_timeout,
        )

    single_override = os.environ.get('GROQ_MODEL')
    if single_override:
        groq_models = [single_override]
    else:
        env_chain = os.environ.get('GROQ_MODEL_CHAIN')
        groq_models = [m.strip() for m in env_chain.split(',') if m.strip()] if env_chain else GROQ_MODEL_CHAIN

    chain = [('groq', m) for m in groq_models]
    if has_nvidia_fallback():
        nvidia_model = os.environ.get('NVIDIA_MODEL', NVIDIA_MODEL)
        chain.append(('nvidia', nvidia_model))

    return _RotatingLLM(chain, temperature, resolved_timeout)
