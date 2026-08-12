"""LLM provider selection and fallback chains for the quiz-generation pipeline."""
from __future__ import annotations

import os
from typing import Optional

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

# NVIDIA NIM (build.nvidia.com) — optional fallback tier, tried only once
# every Groq model above is exhausted/blocked. Entirely optional: if
# NVIDIA_API_KEY isn't set, the rotation chain simply skips this tier.
# OpenAI-compatible endpoint, see llm_utils.py. Free API key at
# https://build.nvidia.com — override via the NVIDIA_MODEL_CHAIN env var
# (comma-separated).
NVIDIA_API_BASE = 'https://integrate.api.nvidia.com/v1'
# meta/llama-3.3-70b-instruct is in the NIM catalog but hangs/times out on
# this account (verified — 120s+, no response) — excluded. Both of these
# verified fast (~1-15s) with clean JSON output.
NVIDIA_MODEL_CHAIN = ['nvidia/llama-3.3-nemotron-super-49b-v1', 'meta/llama-3.1-8b-instruct']

# OpenRouter (openrouter.ai) — optional last-resort fallback tier, tried
# only once every Groq AND NVIDIA model above is exhausted/blocked.
# Entirely optional: if OPENROUTER_API_KEY isn't set, the rotation chain
# simply skips this tier. OpenAI-compatible endpoint, see llm_utils.py.
# Free API key at https://openrouter.ai/keys — override via the
# OPENROUTER_MODEL_CHAIN env var (comma-separated). All three below are
# free-tier models, verified fast (1-8s) with clean JSON output.
OPENROUTER_API_BASE = 'https://openrouter.ai/api/v1'
OPENROUTER_MODEL_CHAIN = [
    'openai/gpt-oss-20b:free',
    'nvidia/nemotron-3-nano-30b-a3b:free',
    'inclusionai/ling-3.0-flash:free',
]


# DeepSeek — optional last-resort fallback tier, tried only once every
# Groq AND NVIDIA AND OpenRouter model above is exhausted/blocked.
# Entirely optional: if DEEPSEEK_API_KEY isn't set, the rotation chain
# simply skips this tier. OpenAI-compatible endpoint, see llm_utils.py.
# New accounts get a 5M-token free grant (~$8.40 value, no credit card,
# valid 30 days as of 2026-08); after that, deepseek-v4-flash is
# $0.14/$0.28 per 1M input/output tokens — see
# https://api-docs.deepseek.com/quick_start/pricing (pricing/lineup
# shifts over time, same caveat as the other tiers here). Free key at
# https://platform.deepseek.com/api_keys — override via the
# DEEPSEEK_MODEL_CHAIN env var (comma-separated).
DEEPSEEK_API_BASE = 'https://api.deepseek.com'
DEEPSEEK_MODEL_CHAIN = ['deepseek-v4-flash']


# The 7 below extend the known-provider list beyond the original 4 — added
# so BYOK users on the Settings -> API Keys page have a ready-made dropdown
# of well-known OpenAI-compatible providers instead of only Groq/NVIDIA/
# OpenRouter/DeepSeek (custom entries with an arbitrary base_url remain
# available for anything not covered here). Each base_url verified live
# against the provider's own current docs 2026-08 — not assumed. Same
# "optional, entirely skipped if the matching *_API_KEY env var isn't set"
# shape as the 4 above applies if the server ALSO wants to use one of these
# as its own fallback tier; BYOK usage doesn't depend on that env var at
# all, since a user's own key comes from the DB, not the environment.

OPENAI_API_BASE = 'https://api.openai.com/v1'
OPENAI_MODEL_CHAIN = ['gpt-4o-mini']

TOGETHER_API_BASE = 'https://api.together.ai/v1'
TOGETHER_MODEL_CHAIN = ['meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo']

MISTRAL_API_BASE = 'https://api.mistral.ai/v1'
MISTRAL_MODEL_CHAIN = ['mistral-large-latest']

# xAI's own docs give 'grok-4-0709' as the current flagship — a dated pin,
# not a floating 'latest' alias, so this is the same "lineup shifts over
# time, override via env var without a code change" caveat as every other
# tier here.
XAI_API_BASE = 'https://api.x.ai/v1'
XAI_MODEL_CHAIN = ['grok-4-0709']

# llama-3.3-70b is flagged deprecated in Cerebras's own docs (2026-08) —
# llama3.1-8b is not, so that's the default rather than the larger model.
CEREBRAS_API_BASE = 'https://api.cerebras.ai/v1'
CEREBRAS_MODEL_CHAIN = ['llama3.1-8b']

FIREWORKS_API_BASE = 'https://api.fireworks.ai/inference/v1'
FIREWORKS_MODEL_CHAIN = ['accounts/fireworks/models/llama-v3p1-8b-instruct']

# Perplexity's OpenAI-compatible base has NO /v1 suffix (confirmed against
# their own docs) — every other provider here does; do not "fix" this to
# match the others, it would break real requests.
PERPLEXITY_API_BASE = 'https://api.perplexity.ai'
PERPLEXITY_MODEL_CHAIN = ['sonar']


def which_provider() -> Optional[str]:
    if os.environ.get('GROQ_API_KEY'):
        return 'groq'
    return None


def has_nvidia_fallback() -> bool:
    return bool(os.environ.get('NVIDIA_API_KEY'))


def has_openrouter_fallback() -> bool:
    return bool(os.environ.get('OPENROUTER_API_KEY'))


def has_deepseek_fallback() -> bool:
    return bool(os.environ.get('DEEPSEEK_API_KEY'))


def has_openai_fallback() -> bool:
    return bool(os.environ.get('OPENAI_API_KEY'))


def has_together_fallback() -> bool:
    return bool(os.environ.get('TOGETHER_API_KEY'))


def has_mistral_fallback() -> bool:
    return bool(os.environ.get('MISTRAL_API_KEY'))


def has_xai_fallback() -> bool:
    return bool(os.environ.get('XAI_API_KEY'))


def has_cerebras_fallback() -> bool:
    return bool(os.environ.get('CEREBRAS_API_KEY'))


def has_fireworks_fallback() -> bool:
    return bool(os.environ.get('FIREWORKS_API_KEY'))


def has_perplexity_fallback() -> bool:
    return bool(os.environ.get('PERPLEXITY_API_KEY'))
