"""Construction of the LLM client used by every node."""
from __future__ import annotations

import logging
import os
import re
import time

from app.ai.langgraph_flow.config import (
    DEEPSEEK_API_BASE,
    DEEPSEEK_MODEL_CHAIN,
    GROQ_MODEL_CHAIN,
    LLM_MAX_TOKENS,
    LLM_TIMEOUT_SECONDS,
    LLM_TEMPERATURE,
    NVIDIA_API_BASE,
    NVIDIA_MODEL_CHAIN,
    OPENROUTER_API_BASE,
    OPENROUTER_MODEL_CHAIN,
    has_deepseek_fallback,
    has_nvidia_fallback,
    has_openrouter_fallback,
    which_provider,
)

log = logging.getLogger('kahoot.ai')

# Both NVIDIA NIM and OpenRouter are OpenAI-compatible endpoints — only the
# base URL and API key env var differ, so they share one builder
# (_build_openai_compatible below). Groq gets its own builder since it uses
# the dedicated ChatGroq client instead. Adding another OpenAI-compatible
# fallback tier is a one-line addition to each of these two dicts — no new
# branch needed in _client_for.
_OPENAI_COMPATIBLE_PROVIDERS = {
    'nvidia': NVIDIA_API_BASE,
    'openrouter': OPENROUTER_API_BASE,
    'deepseek': DEEPSEEK_API_BASE,
}
_PROVIDER_API_KEY_ENV = {
    'nvidia': 'NVIDIA_API_KEY',
    'openrouter': 'OPENROUTER_API_KEY',
    'deepseek': 'DEEPSEEK_API_KEY',
}


def _build_groq_client(model: str, temperature: float, timeout: float):
    from langchain_groq import ChatGroq
    kwargs = dict(
        model=model,
        temperature=temperature,
        max_tokens=LLM_MAX_TOKENS,
        timeout=timeout,
    )
    if 'qwen' in model.lower():
        # Qwen3 reasoning models burn part of max_tokens on a hidden
        # <think> scratchpad before the real answer. With our budget that
        # scratchpad alone can exhaust max_tokens, leaving nothing for the
        # actual JSON — json_utils._THINK_RE then correctly discards the
        # truncated fragment, but parse_llm_json sees an empty response
        # (verified live: without this, the qwen fallback tier was
        # returning 0-char parseable JSON on ~every call). Groq lets us
        # turn reasoning off entirely for these models; verified this
        # still returns clean JSON in <1s.
        kwargs['reasoning_effort'] = 'none'
    return ChatGroq(**kwargs)


def _build_openai_compatible(base_url: str, api_key_env: str):
    """Returns a (model, temperature, timeout) -> ChatOpenAI builder bound
    to one OpenAI-compatible endpoint (NVIDIA NIM, OpenRouter, ...)."""
    def build(model: str, temperature: float, timeout: float):
        from langchain_openai import ChatOpenAI
        kwargs = dict(
            model=model,
            base_url=base_url,
            api_key=os.environ.get(api_key_env),
            temperature=temperature,
            max_tokens=LLM_MAX_TOKENS,
            timeout=timeout,
        )
        if 'gpt-oss' in model.lower():
            # gpt-oss is a reasoning model that burns completion tokens on a
            # hidden reasoning pass before the visible answer — verified live
            # against this pipeline's actual two-hop generate prompt: with no
            # override, the DEFAULT reasoning effort consumed 2524 of a
            # 3307-token completion (76%) on reasoning alone, and on a longer
            # real PDF chunk that reliably blew the whole max_tokens budget
            # with nothing left for the answer (0-char response, unparseable
            # — the exact failure this fixes).
            #
            # 'low' effort cut reasoning by 87% (2524 -> 320 tokens) but was
            # rejected as making the model too shallow for a prompt whose
            # whole point is genuine two-hop reasoning, not just valid JSON.
            # 'medium' only trims it ~14% (2524 -> 2180) and verified-produced
            # well-formed questions with real hop_a/hop_b combination, not
            # degraded — the right tradeoff. Doubling max_tokens on top is
            # extra headroom for a harder real chunk to still not exhaust the
            # budget even at that modest trim; free-tier model, no cost
            # downside to the bigger ceiling.
            kwargs['extra_body'] = {'reasoning': {'effort': 'medium'}}
            kwargs['max_tokens'] = LLM_MAX_TOKENS * 2
        return ChatOpenAI(**kwargs)
    return build


# provider name -> (model, temperature, timeout) -> langchain chat client
_PROVIDER_BUILDERS = {
    'groq': _build_groq_client,
    **{
        provider: _build_openai_compatible(base_url, _PROVIDER_API_KEY_ENV[provider])
        for provider, base_url in _OPENAI_COMPATIBLE_PROVIDERS.items()
    },
}


def _transient_errors():
    """Exception types that mean 'this model/provider, not our request' —
    safe to silently retry on the next chain entry. Rate limit / daily quota
    exhausted, the org's console doesn't have the model enabled, the API key
    is missing/invalid, the provider timed out, couldn't be reached, or
    errored on its end (5xx). Deliberately excludes BadRequestError (400) —
    that usually means OUR request is malformed, which every other model
    would reject the same way, so rotating past it would just hide a real
    bug behind a wall of identical failures.

    groq.* and openai.* mirror each other 1:1 (NVIDIA and OpenRouter are
    both OpenAI-compatible endpoints, see _OPENAI_COMPATIBLE_PROVIDERS).
    groq.APITimeoutError/openai.APITimeoutError already subclass
    APIConnectionError, so listing the parent covers both.
    """
    import groq
    import openai
    return (
        groq.RateLimitError, groq.PermissionDeniedError, groq.AuthenticationError,
        groq.APIConnectionError, groq.InternalServerError,
        openai.RateLimitError, openai.PermissionDeniedError, openai.AuthenticationError,
        openai.APIConnectionError, openai.InternalServerError,
    )


# Process-wide, not per-_RotatingLLM: (provider, model) -> unix timestamp
# it becomes worth retrying. A daily-quota RateLimitError doesn't clear for
# hours, but make_llm() builds a FRESH _RotatingLLM (starting back at chain
# index 0) for every pipeline node — without this cache, every single node
# call would re-waste a round trip on a model already known dead for the
# rest of the day before falling through to whatever tier actually works,
# which dominates the logs and adds latency to every node for no benefit.
# Per-minute limits still self-heal normally: the cached expiry is the
# provider's own reported wait time, so once it elapses the model is tried
# again like nothing happened.
_exhausted_until: dict = {}

_RETRY_AFTER_RE = re.compile(r'try again in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s')


def _parse_retry_after(message: str) -> float:
    """Extract a "try again in <Xh><Ym>Z.zs" duration (Groq/OpenAI's
    rate-limit message shape) in seconds. A default of 30s on no match
    means a future error-message format change costs one extra wasted
    retry, not a model getting stuck uncached forever."""
    m = _RETRY_AFTER_RE.search(message)
    if not m:
        return 30.0
    hours, minutes, seconds = m.groups()
    return (int(hours or 0) * 3600) + (int(minutes or 0) * 60) + float(seconds)


def _mark_exhausted(provider: str, model: str, exc: Exception) -> None:
    """Cache (provider, model) as not-worth-retrying until the provider's
    own reported wait time elapses. Only for RateLimitError — auth/
    permission/connection errors aren't time-based, so waiting doesn't fix
    them; caching those would just delay a legitimate retry once the real
    problem (bad key, network blip) is fixed."""
    import groq
    import openai
    if not isinstance(exc, (groq.RateLimitError, openai.RateLimitError)):
        return
    wait = _parse_retry_after(str(exc))
    _exhausted_until[(provider, model)] = time.time() + wait
    log.info(f'llm rotation: caching {provider}:{model} as exhausted for {wait:.0f}s')


class _RotatingLLM:
    """Wraps a chain of (provider, model) pairs and falls back to the next
    one if the current one's rate limit / daily token quota is exhausted,
    the org's console doesn't have it enabled, the API key is
    missing/invalid, or it timed out/errored — see _transient_errors().

    Once a call falls back to a later entry, this instance keeps using it
    for the rest of its calls instead of re-hitting the exhausted one every
    time — and a fresh make_llm() call (the next pipeline node) still
    starts back at the front of the chain (a per-minute limit may have
    reset by then), but a module-level cache (_exhausted_until) skips any
    entry still inside its own reported wait time instead of re-attempting
    it for real, so a daily-quota outage doesn't cost one wasted round trip
    per node for the rest of the day.
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
            builder = _PROVIDER_BUILDERS.get(provider)
            if builder is None:
                raise ValueError(f'unknown LLM provider {provider!r}')
            self._clients[key] = builder(model, self._temperature, self._timeout)
        return self._clients[key]

    def _rotate(self, messages, accept):
        """Shared walk over the chain from self._idx, used by both invoke()
        and invoke_json(). For each response that doesn't raise a transient
        error, calls `accept(provider, model, resp)` which returns
        `(value, keep_rotating)`:
        - keep_rotating=False: stop here and return `value` (the final
          result).
        - keep_rotating=True: remember `value` as a fallback and try the
          next entry — used by invoke_json() to retry the same request on
          the next model when a response parses to nothing.

        self._idx commits to the first index that responds without raising,
        exactly once per call — a later switch (due to transient errors)
        sticks for the rest of this LLM instance's life, but invoke_json()
        bouncing between "response parsed" vs "didn't parse" on entries
        after that never re-fires the commit.

        Raises the last transient error if every entry from self._idx
        onward raises. Otherwise, if nothing ever satisfied `accept` with
        keep_rotating=False, returns the last remembered fallback.
        """
        transient = _transient_errors()
        last_exc = None
        committed = False
        fallback = None
        remaining = list(range(self._idx, len(self._chain)))
        for pos, idx in enumerate(remaining):
            provider, model = self._chain[idx]
            # Always really attempt the LAST remaining entry even if cached
            # as exhausted, so there's always at least one real attempt (and
            # therefore a real exception to raise below) if every entry is
            # cached — otherwise an all-exhausted chain would fall through
            # this loop with last_exc still None.
            is_last = pos == len(remaining) - 1
            until = _exhausted_until.get((provider, model), 0.0)
            if not is_last and until > time.time():
                log.info(f'llm rotation: skipping {provider}:{model} (known exhausted '
                         f'for {until - time.time():.0f}s more), trying next in chain')
                continue
            try:
                resp = self._client_for(idx).invoke(messages)
            except transient as exc:
                last_exc = exc
                log.warning(f'llm rotation: {provider}:{model} unavailable '
                            f'({type(exc).__name__}: {exc}), trying next in chain')
                _mark_exhausted(provider, model, exc)
                continue
            if not committed and idx != self._idx:
                log.warning(f'llm rotation: switched to {provider}:{model} '
                            f'(chain index {idx}) for the rest of this call')
                self._idx = idx
            committed = True
            value, keep_rotating = accept(provider, model, resp)
            if not keep_rotating:
                return value
            fallback = value
        if fallback is not None:
            return fallback
        raise last_exc

    def invoke(self, messages):
        return self._rotate(messages, lambda provider, model, resp: (resp, False))

    def invoke_json(self, messages):
        """Like invoke(), but a model that responds without raising can still
        fail in a way invoke() can't see: garbage or truncated JSON (e.g. a
        qwen call that burns its whole token budget on a <think> scratchpad
        and never gets to the answer — parse_llm_json then correctly returns
        []). That used to be treated as this chunk's final answer; now it
        rotates to the next model in the chain and retries the SAME request
        instead of giving up on it.

        Unlike a transient API error, a bad parse doesn't mean the model is
        exhausted/blocked, so — unlike invoke() — it does NOT permanently
        move self._idx past that entry; the next unrelated call still starts
        by trying it fresh. self._idx is only committed once, at the first
        entry that responds without raising, exactly like invoke() (see
        _rotate).

        Returns (resp, parsed). If every entry from self._idx onward either
        raises or fails to parse, returns the last (resp, parsed) obtained
        (parsed == [] in that case) so the caller can apply its own
        fallback, matching how a lone invoke()+parse_llm_json() failure used
        to be handled.
        """
        from app.ai.langgraph_flow.json_utils import parse_llm_json

        def accept(provider, model, resp):
            parsed = parse_llm_json(resp.content)
            if parsed:
                return (resp, parsed), False
            log.warning(f'llm rotation: {provider}:{model} returned unparseable/empty '
                        f'JSON, retrying this request on the next model in the chain')
            return (resp, parsed), True

        return self._rotate(messages, accept)


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
    OPENROUTER_API_KEY is set — OPENROUTER_MODEL_CHAIN, then — if
    DEEPSEEK_API_KEY is set — DEEPSEEK_MODEL_CHAIN, trying each tier
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
    if has_deepseek_fallback():
        deepseek_models = _resolve_chain('DEEPSEEK_MODEL_CHAIN', DEEPSEEK_MODEL_CHAIN)
        chain += [('deepseek', m) for m in deepseek_models]

    return _RotatingLLM(chain, temperature, resolved_timeout)


def invoke_json(llm, messages):
    """Invoke `llm` and parse its response as JSON in one call, rotating to
    the next model in the chain — and retrying this same request — if the
    response comes back unparseable/empty rather than accepting it as the
    final answer for whatever the caller was asking. See
    _RotatingLLM.invoke_json for why this is a separate path from a plain
    rate-limit/permission-error rotation.

    `llm` is whatever make_llm() returned. A pinned client (make_llm(model=...))
    isn't a _RotatingLLM — nothing to rotate to — so it just falls back to a
    plain invoke + parse.

    Returns (resp, parsed), same shape callers already got from doing
    `resp = llm.invoke(...); parsed = parse_llm_json(resp.content)` by hand.
    """
    if isinstance(llm, _RotatingLLM):
        return llm.invoke_json(messages)
    from app.ai.langgraph_flow.json_utils import parse_llm_json
    resp = llm.invoke(messages)
    return resp, parse_llm_json(resp.content)
