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
    NVIDIA_MODEL_CHAIN,
    OPENROUTER_API_BASE,
    OPENROUTER_MODEL_CHAIN,
    has_nvidia_fallback,
    has_openrouter_fallback,
    which_provider,
)

log = logging.getLogger('kahoot.ai')

# Both NVIDIA NIM and OpenRouter are OpenAI-compatible endpoints — only the
# base URL and API key env var differ, so they share one code path in
# _RotatingLLM._client_for below. Groq gets its own branch since it uses the
# dedicated ChatGroq client instead.
_OPENAI_COMPATIBLE_PROVIDERS = {
    'nvidia': NVIDIA_API_BASE,
    'openrouter': OPENROUTER_API_BASE,
}
_PROVIDER_API_KEY_ENV = {
    'nvidia': 'NVIDIA_API_KEY',
    'openrouter': 'OPENROUTER_API_KEY',
}


class _RotatingLLM:
    """Wraps a chain of (provider, model) pairs and falls back to the next
    one if the current one's rate limit / daily token quota is exhausted,
    the org's console doesn't have it enabled, or the API key is
    missing/invalid — groq.RateLimitError/PermissionDeniedError or the
    equivalent openai.* errors (NVIDIA and OpenRouter are both
    OpenAI-compatible).

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
            elif provider in _OPENAI_COMPATIBLE_PROVIDERS:
                from langchain_openai import ChatOpenAI
                self._clients[key] = ChatOpenAI(
                    model=model,
                    base_url=_OPENAI_COMPATIBLE_PROVIDERS[provider],
                    api_key=os.environ.get(_PROVIDER_API_KEY_ENV[provider]),
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


def _resolve_chain(env_var: str, default_chain: list) -> list:
    env_val = os.environ.get(env_var)
    return [m.strip() for m in env_val.split(',') if m.strip()] if env_val else default_chain


def make_llm(temperature: float = LLM_TEMPERATURE, model: str = None, timeout: float = None):
    """Build the LLM client used by pipeline nodes. Requires GROQ_API_KEY.
    See .env.example.

    `model` pins one specific Groq model, bypassing rotation — use only
    when a node genuinely needs a particular model. Without it, returns a
    client that transparently rotates through GROQ_MODEL_CHAIN, then — if
    NVIDIA_API_KEY is set — NVIDIA_MODEL_CHAIN, then — if
    OPENROUTER_API_KEY is set — OPENROUTER_MODEL_CHAIN, trying each tier
    only once every model ahead of it in the chain is exhausted/blocked.
    Every *_MODEL_CHAIN can also be overridden via the matching env var
    (comma-separated), without a code change.
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
    groq_models = [single_override] if single_override else _resolve_chain('GROQ_MODEL_CHAIN', GROQ_MODEL_CHAIN)

    chain = [('groq', m) for m in groq_models]
    if has_nvidia_fallback():
        nvidia_models = _resolve_chain('NVIDIA_MODEL_CHAIN', NVIDIA_MODEL_CHAIN)
        chain += [('nvidia', m) for m in nvidia_models]
    if has_openrouter_fallback():
        openrouter_models = _resolve_chain('OPENROUTER_MODEL_CHAIN', OPENROUTER_MODEL_CHAIN)
        chain += [('openrouter', m) for m in openrouter_models]

    return _RotatingLLM(chain, temperature, resolved_timeout)
