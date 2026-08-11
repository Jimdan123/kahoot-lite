"""Standalone LLM judge — calls the provider directly via the standard
`openai` client (every provider used here is OpenAI-compatible; no
langchain anywhere in evals/). Wrapped in DeepEval's DeepEvalBaseLLM
interface so it can be plugged into metrics.py's FactualCorrectnessMetric.

History: originally Hugging Face (a provider fully unrelated to the
pipeline's own), then DeepSeek (after HF's $0.10/month free tier was
confirmed exhausted after ~7-12 judge calls in real use). DeepSeek's own
advertised 5M-token free grant for new accounts turned out not to be
issued on the account tested here (confirmed via DeepSeek's own
/user/balance endpoint: granted_balance == 0.00, and nothing on the
platform.deepseek.com billing dashboard either — an account-side issue,
not a code bug) — so this pivoted to OpenRouter, which worked but exposed
two real problems in production use: its free tier hard-caps at 50
requests/day *shared* across every OpenRouter consumer in this app (the
pipeline's own tier-3 fallback, vision_utils.py's figure/formula/glyph
calls, and the judge all draw from the same 50 — a single calibration
session exhausted it), and the pinned free model
(nvidia/nemotron-3-ultra-550b-a55b:free) is a reasoning model whose
internal chain-of-thought was observed live consuming its entire
max_tokens budget (finish_reason='length', 2270 reasoning tokens) without
ever emitting the requested JSON. Pivoted again, to NVIDIA NIM hosting
z-ai/glm-5.2 — verified live to return clean, complete JSON in ~160
completion tokens (finish_reason='stop', no runaway reasoning) on the same
prompt that broke the OpenRouter model.

Default judge is now NvidiaJudgeLLM. OpenRouterJudgeLLM is kept (not
deleted) for reference/fallback if OpenRouter's daily quota isn't the
active constraint.

Independence note: NVIDIA NIM is already the pipeline's own tier-2
fallback (app/ai/langgraph_flow/config/providers.py) — same reduction from
"fully unrelated provider" as the OpenRouter case before it. Two
mitigations kept: a dedicated NVIDIA_JUDGE_API_KEY (falls back to
NVIDIA_API_KEY if unset, but set a separate one to keep usage trackable),
and a distinct model (z-ai/glm-5.2) never used in the pipeline's own
NVIDIA_MODEL_CHAIN (nvidia/llama-3.3-nemotron-super-49b-v1,
meta/llama-3.1-8b-instruct) — never literally the same model grading its
own output.
"""
from __future__ import annotations

import json
import logging
import os
import re

from deepeval.models import DeepEvalBaseLLM
from openai import OpenAI
from pydantic import BaseModel

from evals.config import NVIDIA_JUDGE_MODEL, OPENROUTER_JUDGE_MODEL

log = logging.getLogger('evals')

OPENROUTER_API_BASE = 'https://openrouter.ai/api/v1'
NVIDIA_API_BASE = 'https://integrate.api.nvidia.com/v1'

# Mirrors app/ai/langgraph_flow/json_utils.py's tolerance for fenced/prefixed
# JSON, but kept fully self-contained per the decoupling requirement — no
# import from the pipeline package.
_JSON_FENCE_RE = re.compile(r'```(?:json)?\s*(.*?)```', re.DOTALL)


def _extract_json(text: str) -> dict:
    text = text.strip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    candidates = [i for i in (text.find('['), text.find('{')) if i != -1]
    if candidates and min(candidates) > 0:
        text = text[min(candidates):]
    return json.loads(text)


class _OpenAICompatibleJudgeLLM(DeepEvalBaseLLM):
    """Shared DeepEvalBaseLLM wrapper around one fixed OpenAI-compatible
    chat-completions model. Subclasses set the class attributes below —
    this exists so a fix (e.g. the max_tokens truncation bug found live
    2026-08-06) only needs to happen once, not once per provider."""

    BASE_URL: str
    API_KEY_ENV: str
    API_KEY_FALLBACK_ENV: str | None = None
    PROVIDER_NAME: str
    # Whether to request response_format={'type': 'json_object'} — only
    # useful/safe for providers confirmed to honor it; NVIDIA NIM's
    # z-ai/glm-5.2 already returns clean JSON without it (verified live),
    # so this stays off there rather than risking an unsupported-param
    # error on a passthrough-hosted third-party model.
    JSON_MODE: bool = False

    def __init__(self, model_id: str):
        self.model_id = model_id
        self._client = None

    def load_model(self):
        if self._client is None:
            token = os.environ.get(self.API_KEY_ENV)
            if not token and self.API_KEY_FALLBACK_ENV:
                token = os.environ.get(self.API_KEY_FALLBACK_ENV)
            if not token:
                env_names = self.API_KEY_ENV
                if self.API_KEY_FALLBACK_ENV:
                    env_names += f' (or {self.API_KEY_FALLBACK_ENV})'
                raise RuntimeError(
                    f'{env_names} is required to run evals/ with {self.PROVIDER_NAME}'
                )
            self._client = OpenAI(api_key=token, base_url=self.BASE_URL)
        return self._client

    def _chat(self, prompt: str, json_mode: bool = False) -> str:
        client = self.load_model()
        kwargs = {}
        if json_mode and self.JSON_MODE:
            kwargs['response_format'] = {'type': 'json_object'}
        completion = client.chat.completions.create(
            model=self.model_id,
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0,
            # GEval's own prompts ask for a detailed multi-sentence 'reason'
            # citing specific facts (see FACTUAL_CORRECTNESS_CRITERIA) —
            # 1024 was observed live truncating mid-string on OpenRouter's
            # judge model, producing invalid JSON ('Unterminated
            # string...') that crashed a whole evaluate_question_set.py
            # batch run on one bad response.
            max_tokens=2048,
            **kwargs,
        )
        return completion.choices[0].message.content or ''

    def generate(self, prompt: str, schema: type[BaseModel] | None = None):
        if schema is None:
            return self._chat(prompt)
        json_prompt = (
            f'{prompt}\n\n'
            f'Respond with ONLY a single valid JSON object matching this '
            f'schema, no prose, no markdown code fences:\n'
            f'{json.dumps(schema.model_json_schema())}'
        )
        raw = self._chat(json_prompt, json_mode=True)
        try:
            return schema(**_extract_json(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning(f'{type(self).__name__}: failed to parse judge JSON: '
                        f'{exc} — raw: {raw[:300]!r}')
            raise

    async def a_generate(self, prompt: str, schema: type[BaseModel] | None = None):
        # openai.OpenAI (sync client) is used here; DeepEval's async path
        # just needs an awaitable, not real concurrency.
        return self.generate(prompt, schema)

    def get_model_name(self) -> str:
        return self.model_id


class NvidiaJudgeLLM(_OpenAICompatibleJudgeLLM):
    """Default judge as of 2026-08-07 — see module docstring for why."""

    BASE_URL = NVIDIA_API_BASE
    API_KEY_ENV = 'NVIDIA_JUDGE_API_KEY'
    API_KEY_FALLBACK_ENV = 'NVIDIA_API_KEY'
    PROVIDER_NAME = 'NVIDIA NIM'
    JSON_MODE = False

    def __init__(self, model_id: str = NVIDIA_JUDGE_MODEL):
        super().__init__(model_id)


class OpenRouterJudgeLLM(_OpenAICompatibleJudgeLLM):
    """Kept for reference/fallback — see module docstring for why this is
    no longer the default."""

    BASE_URL = OPENROUTER_API_BASE
    API_KEY_ENV = 'OPENROUTER_JUDGE_API_KEY'
    API_KEY_FALLBACK_ENV = 'OPENROUTER_API_KEY'
    PROVIDER_NAME = 'OpenRouter'
    JSON_MODE = True

    def __init__(self, model_id: str = OPENROUTER_JUDGE_MODEL):
        super().__init__(model_id)


if __name__ == '__main__':
    # Verification step 0: `python -m evals.judge` — confirms the judge's
    # API key works and the pinned model responds before running anything
    # else.
    judge = NvidiaJudgeLLM()
    reply = judge.generate("Reply with exactly one word: 'ok'.")
    print(f'Model: {judge.get_model_name()}')
    print(f'Reply: {reply!r}')
