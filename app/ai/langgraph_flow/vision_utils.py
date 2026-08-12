"""Narrowly-scoped vision-model calls for extraction gaps plain text can't
cover: unknown font glyphs (glyph_resolver.py's Layer 2), complex 2D
formulas, and embedded figures/diagrams.

Every call here is bounded to one small cropped region — never a full
page. That's deliberate: an earlier version of this pipeline's OCR
fallback asked a vision LLM to transcribe entire scanned pages and it was
reverted after hitting Groq's free-tier rate limit almost immediately and
hallucinating text that wasn't in the source image (see HOW_IT_WORKS.md's
extract_text node history). A tiny cropped glyph/formula/figure is a much
smaller, much less open-ended request than "transcribe this whole page",
so both failure modes are far less exposed here.

Uses OpenRouter — already an integrated, proven-working text tier in this
pipeline (see config/providers.py) — with a free vision-capable model,
rather than adding a new provider/credential just for vision. No-ops
(has_vision() == False) if OPENROUTER_API_KEY isn't set, same convention
as every other optional tier in this pipeline.
"""
from __future__ import annotations

import base64
import logging
import os
import time

from app.ai.langgraph_flow.config import LLM_TIMEOUT_SECONDS, OPENROUTER_API_BASE
from app.ai.langgraph_flow.json_utils import content_to_text

log = logging.getLogger('kahoot.ai')

# Free, vision-capable model on OpenRouter's free collection — confirmed
# by querying https://openrouter.ai/api/v1/models directly and filtering
# for id.endswith(':free') with 'image' in architecture.input_modalities
# (2026-08; an earlier guessed slug, 'google/gemma-4-31b:free', turned out
# not to exist — 400 'not a valid model ID' — so this was verified against
# the live API, not assumed from search-result summaries). Live-tested
# end to end on a real cropped glyph (a verified CMMIB10 lambda) and
# correctly returned 'λ'. google/gemma-4-31b-it:free (also in the
# verified list) hit a 429 on OpenRouter's shared free-tier pool at test
# time — kept as a documented alternative, not the default, since this
# one actually worked live. Lineup/availability shifts over time — same
# caveat config/providers.py documents for its own text-model chains.
# Override via VISION_MODEL env var without a code change.
DEFAULT_VISION_MODEL = 'nvidia/nemotron-nano-12b-v2-vl:free'
VISION_MODEL = os.environ.get('VISION_MODEL', DEFAULT_VISION_MODEL)


# Process-wide circuit breaker, mirrors llm_utils.py's _exhausted_until
# pattern for the text-model chain. Found live 2026-08-12 against a
# 121-page, formula-heavy math PDF: without this, once OpenRouter's free
# vision tier's DAILY quota (shared across every glyph/formula/figure call
# in this app, see this module's docstring) hits zero, every single
# remaining corrupted glyph on every remaining page re-attempts the exact
# same doomed API call (confirmed: the same handful of glyph codepoints —
# summation signs, big parentheses, etc. — recur across dozens of pages,
# each occurrence burning a ~2s round trip against an endpoint already
# known to return 429). A daily-quota exhaustion doesn't self-heal within
# a run the way a per-minute limit would, so there's no reason to keep
# trying once we've seen it once.
_vision_exhausted_until: float = 0.0


def _is_vision_exhausted() -> bool:
    return time.time() < _vision_exhausted_until


def _mark_vision_exhausted(exc: Exception) -> None:
    """Cache 'vision is out of quota until timestamp T' on any RateLimitError
    — never for other failures (a single bad crop, a transient timeout
    shouldn't disable vision for every remaining glyph on every remaining
    page just because ONE call happened to fail)."""
    global _vision_exhausted_until
    import openai
    if not isinstance(exc, openai.RateLimitError):
        return
    reset_ms = None
    try:
        reset_ms = int(exc.body['error']['metadata']['headers']['X-RateLimit-Reset'])
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    # If the provider's response doesn't carry a parseable reset time (a
    # different error shape, a future API change), fall back to a
    # conservative 1-hour pause rather than not backing off at all — same
    # "reasonable fallback when we can't perfectly parse the wait time"
    # spirit as llm_utils.py's _parse_retry_after 30s no-match default.
    until = reset_ms / 1000.0 if reset_ms is not None else time.time() + 3600
    if until > _vision_exhausted_until:
        _vision_exhausted_until = until
        log.warning(f'vision_utils: OpenRouter vision quota exhausted, disabling vision for the '
                    f'next {until - time.time():.0f}s — remaining glyph/formula/figure lookups '
                    f'this run will use placeholders instead of retrying a known-dead endpoint')


def has_vision() -> bool:
    return bool(os.environ.get('OPENROUTER_API_KEY')) and not _is_vision_exhausted()


def _vision_client():
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(
        model=VISION_MODEL,
        base_url=OPENROUTER_API_BASE,
        api_key=os.environ.get('OPENROUTER_API_KEY'),
        temperature=0,
        timeout=LLM_TIMEOUT_SECONDS,
    )


# Every prompt below explicitly forbids backslashes/LaTeX, but a vision
# model doesn't always comply — verified live 2026-08: asked for "the
# symbol itself or its plain text name" and got back the literal string
# "\lambda" (LaTeX command syntax) instead of "λ" or "lambda". Since these
# outputs get cached (glyph_resolver.py) and ultimately flow into the same
# JSON-producing LLM calls Part A hardened, a literal backslash here would
# reintroduce exactly the JSON-escaping hazard that fix exists to prevent.
# Defense in depth, matching Part A's own prompt-instruction-plus-repair
# pattern: strip backslashes here too, don't just ask nicely and hope.
_BACKSLASH_RE = None


def _strip_backslashes(text: str) -> str:
    global _BACKSLASH_RE
    if _BACKSLASH_RE is None:
        import re
        _BACKSLASH_RE = re.compile(r'\\')
    return _BACKSLASH_RE.sub('', text)


def _ask(image_bytes: bytes, prompt: str) -> str:
    from langchain_core.messages import HumanMessage
    b64 = base64.b64encode(image_bytes).decode('ascii')
    message = HumanMessage(content=[
        {'type': 'text', 'text': prompt},
        {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}},
    ])
    try:
        resp = _vision_client().invoke([message])
    except Exception as exc:
        _mark_vision_exhausted(exc)
        raise
    return _strip_backslashes(content_to_text(resp.content).strip())


def describe_glyph(image_bytes: bytes) -> str:
    """One cropped math/physics/chemistry symbol -> its plain-text/Unicode
    identity. Used by glyph_resolver.py only when the static glyph_cache
    has no entry for this (font, codepoint) combo."""
    return _ask(image_bytes,
        'This is a single symbol cropped from a math, physics, or '
        'chemistry textbook or lecture slide (e.g. a Greek letter, an '
        'operator, a unit symbol, a reaction arrow). Respond with ONLY '
        'the symbol itself (e.g. λ, Σ, Ω, Å, →) or its plain English name '
        'with NO backslash and NO LaTeX command (e.g. "Lambda", "Sigma", '
        '"Ohm", "Angstrom", "yields" for a reaction arrow) — never '
        '"\\lambda", "\\sqrt", or "\\rightarrow". No prose, no '
        'punctuation, no explanation.'
    )


def transcribe_formula(image_bytes: bytes) -> str:
    """One cropped 2D formula/equation/reaction region (a fraction, matrix,
    multi-line derivation, or chemistry reaction) -> a linear, plain-text
    transcription. Used when the region's spatial layout can't be trusted
    to linearize correctly from character positions alone."""
    return _ask(image_bytes,
        'This is a formula, equation, or reaction cropped from a math, '
        'physics, or chemistry textbook or lecture slide — it may be '
        'algebraic, a physics equation with units, or a chemistry '
        'reaction/structural formula. Transcribe it as a single line of '
        'plain text/ASCII notation (e.g. "sqrt(A^T A)", "sum from i=1 to '
        'n of a_i x_i", "F = m * a", "2H2 + O2 -> 2H2O", "CH3COOH"). Use '
        '"->" for a one-way reaction arrow and "<=>" for an equilibrium '
        'arrow. Spell out a unit name in full if there is no plain-ASCII '
        'symbol for it (e.g. "ohm", "joule") rather than inventing one. '
        'Never a backslash and never a LaTeX command (not "\\sqrt{}", '
        'not "\\sum", not "\\rightarrow"), not markdown. No prose, no '
        'explanation, just the transcription.'
    )


def describe_figure(image_bytes: bytes) -> str:
    """One cropped diagram/figure -> a short factual description, so
    content that plain text extraction can't see at all (a labeled
    biology diagram, a circuit diagram, a structural-formula image) can
    still ground real questions."""
    return _ask(image_bytes,
        'This is a figure or diagram cropped from a textbook or lecture '
        'slide — it may be a math chart/plot, a physics circuit or '
        'free-body diagram, a chemistry structural formula or reaction '
        'scheme, a biology diagram, or another kind of labeled '
        'illustration. Describe it factually in 1-3 sentences, focusing '
        'on any labeled parts or structural relationships a student '
        'would need to know (e.g. component names in a circuit, '
        'atoms/bonds in a structure, forces/vectors in a free-body '
        'diagram, axis labels in a chart). No speculation beyond what is '
        'visibly labeled or shown. Plain English prose only — no '
        'backslashes, no LaTeX commands.'
    )
