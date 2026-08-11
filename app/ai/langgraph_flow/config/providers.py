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
