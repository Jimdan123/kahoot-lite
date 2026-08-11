"""Tunables and gate functions for the evals/ package.

Deliberately independent of app/ai/langgraph_flow/config/ — same
"optional env var, zero infra unless set" convention (see
has_search_enrichment() there), but a separate implementation, since
evals/ doesn't import anything from the pipeline.
"""
from __future__ import annotations

import os

# NVIDIA NIM (integrate.api.nvidia.com) — OpenAI-compatible API, the judge
# provider as of 2026-08-07. History: Hugging Face (exhausted its
# $0.10/month free tier after ~7-12 judge calls in real use) -> DeepSeek
# (advertised a 5M-token free grant for new accounts, but the tested
# account's own /user/balance showed granted_balance == 0.00 — an
# account-side issue, not a code bug, but it blocked live testing) ->
# OpenRouter (worked, but two real problems surfaced in production use:
# (1) its free tier hard-caps at 50 requests/day *shared* across every
# OpenRouter consumer in this app — the pipeline's own tier-3 fallback,
# vision_utils.py's figure/formula/glyph calls, AND the judge all draw from
# the same 50, so a single day's calibration run could exhaust it before
# the judge ever ran; (2) the pinned free model
# (nvidia/nemotron-3-ultra-550b-a55b:free) is a reasoning model whose
# internal chain-of-thought was observed live consuming its entire
# max_tokens budget (finish_reason='length', 2270 reasoning tokens) without
# ever emitting the requested JSON — response_format=json_object reduced
# but did not eliminate this) -> NVIDIA NIM hosting z-ai/glm-5.2, verified
# live to return clean, complete JSON in ~160 completion tokens
# (finish_reason='stop', no runaway reasoning) on the same prompt that
# broke the OpenRouter model.
#
# Independence note: NVIDIA NIM is already the pipeline's own tier-2
# fallback (app/ai/langgraph_flow/config/providers.py) — same reduction
# from "fully unrelated provider" as the OpenRouter case. Two mitigations
# kept: a dedicated NVIDIA_JUDGE_API_KEY (separate from the pipeline's own
# NVIDIA_API_KEY), and a distinct model (z-ai/glm-5.2) never used in the
# pipeline's own NVIDIA_MODEL_CHAIN (nvidia/llama-3.3-nemotron-super-49b-v1,
# meta/llama-3.1-8b-instruct) — never literally the same model grading its
# own output.
DEFAULT_NVIDIA_JUDGE_MODEL = 'z-ai/glm-5.2'
NVIDIA_JUDGE_MODEL = os.environ.get('NVIDIA_JUDGE_MODEL', DEFAULT_NVIDIA_JUDGE_MODEL)

# OpenRouter judge config kept (not deleted) for reference/fallback if its
# daily quota isn't the active constraint — see history above for why it's
# no longer the default.
DEFAULT_OPENROUTER_JUDGE_MODEL = 'nvidia/nemotron-3-ultra-550b-a55b:free'
OPENROUTER_JUDGE_MODEL = os.environ.get('OPENROUTER_JUDGE_MODEL', DEFAULT_OPENROUTER_JUDGE_MODEL)

# GEval score (0-1) at/above which a question counts as "correct" per the
# judge. Used by FactualCorrectnessMetric's threshold and by the
# regression test's pass-rate assertion.
JUDGE_PASS_THRESHOLD = 0.5

# Regression suite (test_question_correctness.py): minimum fraction of a
# QuestionSet's questions that must pass the judge for the suite to pass.
MIN_PASS_RATE = 0.8


def has_nvidia_judge_key() -> bool:
    return bool(os.environ.get('NVIDIA_JUDGE_API_KEY') or os.environ.get('NVIDIA_API_KEY'))


def has_openrouter_judge_key() -> bool:
    return bool(os.environ.get('OPENROUTER_JUDGE_API_KEY') or os.environ.get('OPENROUTER_API_KEY'))


def has_mlflow() -> bool:
    return bool(os.environ.get('MLFLOW_TRACKING_URI'))


def mlflow_tracking_uri() -> str | None:
    return os.environ.get('MLFLOW_TRACKING_URI')
