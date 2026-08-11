# evals/ — human-calibrated correctness checks for generated quizzes

Checks whether the questions the PDF → quiz pipeline (`app/ai/langgraph_flow/`)
generates are actually *correct* — is the labeled correct answer really
correct, are the distractors really wrong? The pipeline's own
`nodes/critic.py` only checks structural well-formedness and closed-book
leakage; this package independently fact-checks content using an LLM
judge, calibrated first against a human baseline.

**Fully decoupled from the pipeline.** This package reads already-generated
quizzes straight out of Postgres (`QuestionSet`/`Question` in
`app/models.py`); it never calls pipeline internals
(`graph.py`/`state.py`/`llm_utils.py`/anything under `nodes/`). The judge
here calls **OpenRouter** directly via the standard `openai` client
(OpenRouter is OpenAI-compatible). The pipeline also has its own
OpenRouter tier (tier 3 of 4, `app/ai/langgraph_flow/config/providers.py`)
— see "Independence note" below for how the two stay meaningfully
separate despite sharing a provider.

**Provider history** (why this isn't Hugging Face or DeepSeek, both tried
first): HF's Inference Providers free tier ($0.10/month) was confirmed
exhausted after ~7-12 judge calls in real use (2026-08). DeepSeek
advertises a 5M-token free grant for new accounts, but the tested
account's own `/user/balance` endpoint showed `granted_balance: 0.00` —
nothing on the billing dashboard either, an account-side issue that
blocked live testing, not a code bug. OpenRouter was already proven
working in this exact codebase (the pipeline's own tier-3 fallback, plus
this package's own `vision_utils.py`) with no balance issues for
`":free"`-suffixed models — so that's the default now.

## Setup

```bash
pip install -r requirements-eval.txt
```

Add to `.env` (see `.env.example`):

- **`OPENROUTER_JUDGE_API_KEY`** (required to run the judge; falls back to
  `OPENROUTER_API_KEY` if unset) — free at https://openrouter.ai/keys, no
  credit card, no balance issues for `":free"`-suffixed models. The judge
  model is pinned via `OPENROUTER_JUDGE_MODEL` (default
  `nvidia/nemotron-3-ultra-550b-a55b:free` in `evals/config.py`) so scores
  stay comparable over time — this lineup shifts, same caveat
  `app/ai/langgraph_flow/config/providers.py` documents for its own model
  chains.
- **Independence note**: this reuses the same provider as the pipeline's
  own tier-3 fallback — a real reduction from the original "fully
  unrelated provider" design (there's no separate OpenRouter account
  available here, only an optional separate key). Use a *separate*
  `OPENROUTER_JUDGE_API_KEY` (not just the `OPENROUTER_API_KEY` fallback)
  if you want usage trackable independently, and note the judge is pinned
  to a large model never used in the pipeline's own
  `OPENROUTER_MODEL_CHAIN` — never literally the same model grading its
  own output, even when the pipeline's tier-3 fires on a given question.
- **`MLFLOW_TRACKING_URI`** (optional) — unset = all MLflow logging is a
  no-op, everything else still works. Local: `MLFLOW_TRACKING_URI=./mlruns`,
  then `mlflow ui` to view runs. Production: `render.yaml` points this at
  the same Postgres DB as `DATABASE_URL`.
- **`CONFIDENT_API_KEY`** (optional) — DeepEval's hosted dashboard. Unset =
  local-only, everything still works.

Sanity-check the judge can actually reach OpenRouter before running
anything else:

```bash
python -m evals.judge
```

## Workflow

### Phase 1 — build a human baseline

Upload the ~20 curated PDFs listed in `evals/calibration_sources.md`
through the running app (fill in that table as you go), then label the
resulting questions by hand:

```bash
python -m evals.human_review --latest 20
# or: python -m evals.human_review --question-set-id 42
```

Labels accumulate in `evals/human_labels.json` (JSON-lines, append-only —
not a DB table, this is a small one-time calibration dataset).

### Phase 2 — calibrate the judge against that baseline

```bash
python -m evals.calibrate_judge
```

Prints agreement rate, and — more important — the **false-positive rate**
(judge says correct, human said incorrect: the dangerous case) and
false-negative rate. Every disagreement prints the judge's stated `reason`
next to the human's `comment`/`info_gap`, and the full trail is written to
`evals/calibration_report.json`. If agreement is poor, iterate on
`FactualCorrectnessMetric`'s criteria in `evals/metrics.py` and rerun —
this loop is manual, the tooling just makes each iteration measurable.

### Phase 3 — ongoing regression checks

```bash
python -m evals.evaluate_question_set --question-set-id 42
python -m evals.evaluate_question_set --latest 5
pytest evals/test_question_correctness.py
```

Works against production too — point `DATABASE_URL` at Render's DB.

### Set-level quality reviews (separate from Phase 1-3)

`human_labels.json` only captures per-question factual correctness — it
says nothing about duplication, question-type variety, or contradictions
between questions in the same set (e.g. two questions keying opposite
answers to the same underlying fact). None of the pipeline's own checks
catch that either (`critic.py` is structural well-formedness + closed-book
leakage; the DeepEval judge is per-question correctness).

`evals/set_quality_reviews.py` (`append_set_review()` /
`load_set_reviews()`) stores that separate axis of human feedback —
free-form but structured (uniqueness/duplication notes with the specific
`question_ids` involved, question-type "dynamics" notes, internal
-consistency conflicts) — in `evals/set_quality_reviews.json`
(JSON-lines, append-only, same convention as `human_labels.json`). Not
wired into `calibrate_judge.py`'s agreement metrics — it's meant to
surface concrete, traceable findings (e.g. "questions #142/#143 both key
Linked List for indexed insertion, which is Θ(n) not O(1)") that inform a
future pipeline change (a dedup or cross-question-consistency check in
`critic.py`), not to be consumed programmatically yet.

## What runs automatically

`app/ai/routes.py`'s upload worker calls `evals.mlflow_logging.log_question_set()`
after every real quiz generation — cheap, non-LLM stats only
(`evals/db_metrics.py`: question count, closed-book-leak rate, difficulty
breakdown). It never invokes the LLM judge (that's on-demand only, via
`evaluate_question_set.py`, since it's LLM-cost-heavy) and it's a
best-effort no-op if `MLFLOW_TRACKING_URI` is unset.

## Package layout

| File | Purpose |
|---|---|
| `config.py` | `has_mlflow()`, judge model/threshold config |
| `judge.py` | `OpenRouterJudgeLLM` — DeepEvalBaseLLM wrapper around OpenRouter |
| `metrics.py` | `FactualCorrectnessMetric` — the DeepEval GEval metric |
| `db_metrics.py` | Cheap non-LLM `QuestionSet` stats |
| `mlflow_logging.py` | No-op-if-unconfigured MLflow logging |
| `human_review.py` | Interactive CLI for Phase 1 |
| `human_labels.py` | Read/write helper for `human_labels.json` |
| `set_quality_reviews.py` | Read/write helper for `set_quality_reviews.json` (set-level quality notes, separate from per-question labels) |
| `calibrate_judge.py` | Phase 2 — judge-vs-human agreement report |
| `evaluate_question_set.py` | Phase 3 — ongoing fact-check runner + CLI |
| `test_question_correctness.py` | Pytest regression suite |
| `conftest.py` | Flask app-context fixture |
