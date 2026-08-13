# What the critic actually checks

Source: `app/ai/langgraph_flow/nodes/critic.py`. Two separate mechanisms live here, checking two
different things — easy to conflate, so kept explicit throughout.

## 1. `closed_book_check` (Stage 6) — "can this be answered without the document?"

A **second, context-free LLM call**, given only the question text and its 4 options (no document,
no `hop_a`/`hop_b`, no comprehension record):

```
For each question below, answer using ONLY your general knowledge — you have not
read whatever source document these came from, and won't see it.
```

It returns `{"answer": "A|B|C|D|unknown", "confidence": "high|medium|low"}` per question.

**Leak criteria** (computed in Python, `_is_closed_book_leak`, not asked of any LLM):
```python
cb.get('confidence') in ('high', 'medium') and cb.get('answer') == draft.get('correct')
```
Low confidence, or a wrong guess, does **not** count as a leak — only a *confident, correct*
guess with zero document access does.

**What happens on a leak**: historically an automatic reject; as of the current code, it is
**not** — the question is kept if it passes `quality_check` below, just tagged
`source: "closed_book"` on save so it's honestly labeled rather than silently discarded (see the
module docstring: *"the two-hop reasoning is often still genuine even when the specific answer
happens to also be common knowledge"*).

This same leak signal is reused by `nodes/enrich.py`'s `_high_leak_rate()` to decide whether a
*retry* should fetch external context first (see `graph.py`'s `_should_retry`).

If the closed-book LLM call itself fails, every result is marked `{"answer": "unknown",
"confidence": "low"}` — deliberately neither "leaked" (would reject everything) nor "safe" (would
skip the check silently).

## 2. `quality_check` (Stage 7) — the actual "is this a well-formed question" critic

A **third LLM call**, given each draft's question/options/correct answer plus its claimed
`hop_a`/`hop_b`/`tests`. Explicitly told **not** to consider closed-book answerability — that's
judged separately (item 1 above) and is "NOT one of your rejection tests."

Five rejection tests, applied **in order**, reject on first failure, otherwise accept:

| # | Test | Rejects when |
|---|---|---|
| 1 | **STRUCTURAL** | more than one option looks correct, the correct option isn't among A-D, or an option repeats the stem verbatim |
| 2 | **PURE DEFINITION PAIRING** | both `hop_a`/`hop_b` are dictionary/glossary-style definitions with no narrative fact, mechanism, or quantity connecting them to an actual situation |
| 3 | **EXTERNAL-ONLY PAIRING** | both hops are attributed to EXTERNAL CONTEXT (web search) — no hop is actually grounded in the document itself |
| 4 | **DECORATIVE HOP** | the question is fully answerable while ignoring `hop_b` |
| 5 | **AMBIGUOUS OR TRIVIAL** | unclear, admits more than one defensible answer, or is trivial despite claiming two hops |

Returns `{"verdict": "accept"|"reject", "reason": "..."}` per question.

**If this LLM call itself fails** (`quality_check`'s `except` branch), it falls back to
`_structurally_valid()` — a much weaker Python-only check: `correct` is one of A-D, and all four
option strings are non-empty. None of tests 2-5 apply in this fallback path.

**Deterministic dedup** (Python, not the LLM): accepted questions are deduped by exact
lowercased question text, seeded with everything already accumulated from earlier retry attempts
— a retry's fresh drafts can't re-add a near-duplicate of a question a prior attempt already kept.

## What neither check verifies

Explicitly out of scope for both mechanisms, confirmed against the actual prompts above and
against a real generated question in `docs/gen-pipeline-test-log.md`:

- **Whether the claimed correct answer is actually correct.** Neither check re-reads the source
  document to verify `hop_a`/`hop_b` are true statements about it, or that the causal link the
  question draws between them is actually stated (vs. a reasonable-but-unstated inference) — the
  live test found exactly this: a question `quality_check` accepted had a real, if minor,
  unstated-inference gap that only an independent check against the raw source text caught.
- **Whether a `span` field from `comprehend_chunks` is a real verbatim quote.** The comprehension
  extraction prompt requires one, but nothing downstream — not `quality_check`, not anything else
  in the pipeline — programmatically confirms the LLM's claimed span actually appears in the chunk.
- **General factual correctness independent of the document.** That's `evals/`'s job
  (`FactualCorrectnessMetric` in `evals/metrics.py`) — and even that only checks against the
  judge's own general knowledge, never against the source text either (see
  `docs/gen-pipeline-test-log.md`'s takeaway).
