"""Helpers for turning an LLM chat response into parsed JSON."""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger('kahoot.ai')

_JSON_FENCE_RE = re.compile(r'```(?:json)?\s*(.*?)```', re.DOTALL)
# Reasoning models (e.g. Groq's qwen/qwen3.6-27b) wrap scratch-work in
# <think>...</think> before the real answer — and that scratch-work often
# contains its own draft JSON/code fences. Strip it first, or the fence/
# bracket search below can grab a fragment from the draft instead of the
# actual final answer.
_THINK_RE = re.compile(r'<think>.*?</think>', re.DOTALL)


def content_to_text(raw) -> str:
    """Normalize a LangChain message .content value to plain text.

    Some models/providers return content as a list of block dicts
    (e.g. [{'type': 'text', 'text': '...'}]) instead of a plain string —
    without this, str(raw) on the list produces a Python-repr string
    (single-quoted, escaped) that can never parse as JSON. Kept
    provider-agnostic on purpose: whichever LLM client this pipeline uses
    next may or may not have the same quirk.
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for block in raw:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(block.get('text', ''))
        return ''.join(parts)
    return str(raw)


def parse_llm_json(raw):
    """Strip common LLM wrapper cruft (code fences, prose) and return the parsed JSON.
    Falls back to an empty list rather than raising, so one bad response doesn't kill the run."""
    text = content_to_text(raw).strip()
    text = _THINK_RE.sub('', text).strip()
    if '<think>' in text:
        # Truncated mid-thought (hit max_tokens before the real answer) —
        # an unclosed tag means there's no actual answer past this point,
        # so drop it rather than let the bracket search below grab a
        # fragment of the draft and mistake it for the final JSON.
        text = text.split('<think>')[0].strip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    # If the model prefixed something like "Here are the questions:", find the
    # earliest top-level bracket and strip everything before it. Must compare
    # '[' and '{' together (not check one, then the other) — otherwise an
    # array starting at index 0 falls through to the '{' check and gets
    # truncated at its first nested object instead of left alone.
    candidates = [i for i in (text.find('['), text.find('{')) if i != -1]
    if candidates and min(candidates) > 0:
        text = text[min(candidates):]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        # The two real causes look identical from the caller's side (an empty
        # list) unless we log the actual parse error here: either the
        # response got cut off before the JSON closed (exc.pos lands at/near
        # the end of `text` — raise LLM_MAX_TOKENS) or the model slipped an
        # invalid escape (e.g. raw LaTeX "\Var") into a string value
        # (exc.pos lands mid-string). Logging a window around exc.pos
        # distinguishes them at a glance instead of guessing.
        window = text[max(0, exc.pos - 40):exc.pos + 40]
        log.warning(f'parse_llm_json: {exc} — near {window!r} (total {len(text)} chars)')
        return []
