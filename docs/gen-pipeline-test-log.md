# Gen-pipeline live test log — Harry Potter Ch.1, one question, independently verified

Real end-to-end test of the question-generation pipeline (`app/ai/langgraph_flow/`), run outside
the normal Flask/DB/upload flow to make it controllable: the pipeline's node functions were called
directly against `examples/harry-potter-1-chapters/01-the-boy-who-lived.pdf`, `enrich_context` was
skipped entirely (text-only grounding, no web search), and generation was scoped to exactly one
question from one chunk instead of the pipeline's normal multi-question-per-chunk batch. The
generated question was then checked two different ways: the pipeline's own `critic.py`, and a
separate subagent with no access to the pipeline's own comprehension record — only the raw chapter
text — asked to independently confirm or dispute it.

Driver script: throwaway, not committed (session scratchpad only). Reused the pipeline's real code
paths directly (`extract_text`, `chunk_by_topic`, `comprehend_chunks`, `merge_comprehension`,
`generate.py`'s own prompt/context-building, `closed_book_check`, `quality_check`) — no
reimplementation, no mocking.

## Setup issues hit and fixed live

1. **Groq's `llama-3.3-70b-versatile` was already at today's daily token cap** (100,000 TPD, 97,315
   already used before this test even started — leftover from earlier unrelated usage) — the
   rotation chain correctly fell over to `qwen/qwen3.6-27b` per its own design.
2. **The next fallback tier (`nvidia:nvidia/llama-3.3-nemotron-super-49b-v1`) hung for the full 45s
   `LLM_TIMEOUT_SECONDS` before failing over.** Confirmed via `curl` that the endpoint itself
   responds instantly (404 on `/`, not a network block) — the chat-completions backend specifically
   was slow/dead at test time. This matches a risk `config/providers.py` already documents for a
   *different* NVIDIA model ("hangs/times out on this account") — first live confirmation that
   `nvidia/llama-3.3-nemotron-super-49b-v1` (the one NOT excluded in that comment) can do the same.
3. **Found a real gap in `llm_utils.make_llm()`'s pinned-model bypass** (`model=` param): it builds
   `ChatGroq` directly and skips `_build_groq_client`'s qwen-specific `reasoning_effort='none'`
   patch — so a pinned qwen call burns its token budget on a hidden `<think>` scratchpad instead of
   the fast path the normal rotation chain gets. This is why the first pinned-qwen attempt produced
   truncated/malformed JSON and appeared to hang. Currently dormant (the code's own comment notes
   `model=` has no real call site today), but worth fixing if that ever changes. Worked around for
   this test by calling `_build_groq_client` directly instead of `make_llm(model=...)`.
4. **`merge_comprehension` hit qwen's 8,000-tokens-per-minute limit** (`Request too large... Requested
   10050`) on the full 9-chunk merge call — and its existing `_naive_merge` fallback fired exactly as
   designed, logged the failure, and continued rather than crashing. Live confirmation that fallback
   path actually works, not just in theory.

## Run trace

| Stage | Result |
|---|---|
| `extract_text` | 25,147 chars, no OCR needed (real text layer) |
| `chunk_by_topic` | 9 chunks, 500 words each |
| `comprehend_chunks` | succeeded on all 9 chunks (7-12 claims, 0-2 mechanisms/quantities each — see table below) |
| `merge_comprehension` | LLM call failed (TPM limit) → fell back to naive concat; `topic=''`, `links=0` |
| `enrich_context` | **skipped entirely**, per test requirements |
| `generate` | 1 question generated from chunk 0, `n=1` |
| `closed_book_check` | ran on that question |
| `quality_check` | ran on that question |
| Total tokens | 17,333 prompt + 7,116 completion = 24,449 |

Per-chunk comprehension counts:

| Chunk | Type | Claims | Defs | Mechanisms | Quantities |
|---|---|---|---|---|---|
| 0 | narrative | 7 | 0 | 0 | 2 |
| 1 | narrative | 7 | 0 | 0 | 2 |
| 2 | narrative | 10 | 1 | 2 | 2 |
| 3 | narrative | 10 | 0 | 0 | 0 |
| 4 | narrative | 8 | 1 | 1 | 1 |
| 5 | narrative | 12 | 1 | 0 | 2 |
| 6 | narrative | 9 | 0 | 1 | 1 |
| 7 | narrative | 8 | 0 | 1 | 2 |
| 8 | narrative | 9 | 0 | 0 | 2 |

## The generated question

```json
{
  "hop_a": "CLAIM: Mr. Dursley initially saw a cat reading a map, but upon looking again, saw only a cat with no map.",
  "hop_b": "CLAIM: Mr. and Mrs. Dursley considered themselves perfectly normal and rejected anything strange or mysterious.",
  "tests": "A student who only knows the visual fact (the cat/map) might interpret Mr. Dursley's reaction as simple confusion or a visual error, missing the deeper psychological reason for his dismissal: his active rejection of the 'strange and mysterious' to maintain his self-image of normalcy.",
  "question": "Why does Mr. Dursley dismiss his initial sighting of a cat reading a map as a 'trick of the light' rather than investigating further?",
  "A": "He is a busy director of a drill manufacturing firm and cannot afford to stop for a stray animal.",
  "B": "He has a deep-seated need to maintain the image of being perfectly normal and rejects anything strange or mysterious.",
  "C": "He is distracted by his son Dudley's tantrum and throwing cereal at the walls.",
  "D": "He knows that cats cannot read maps, so he assumes his eyes are playing tricks on him due to the cloudy weather.",
  "correct": "B",
  "difficulty": "medium",
  "time_limit": 25
}
```

## Verdict 1 — the pipeline's own critic (`nodes/critic.py`)

- `closed_book_check`: answered **B**, confidence **high** — with *zero* access to the document.
- `quality_check`: kept the question anyway (structurally sound, real two-hop reasoning), but tagged
  it `source: "closed_book"` — an honestly-labeled leak, per that node's current design (a leak is
  no longer an automatic reject, see `nodes/critic.py`'s module docstring).

What this check actually verifies: *can a model answer this without reading the document?* It does
**not** check whether the claimed answer is actually correct, or whether hop_a/hop_b are actually
true statements about the source text.

## Verdict 2 — independent verifier (fresh subagent, chapter text only, no comprehension record)

Given only the question JSON above and the path to the raw extracted chapter text — explicitly
*not* the pipeline's own comprehension record — and instructed to ground every claim in literal
quotes from the text, not general knowledge of the book:

- **hop_a** — confirmed **near-verbatim**: *"a cat reading a map... There was a tabby cat...
  but there wasn't a map in sight."*
- **hop_b** — also confirmed **near-verbatim**, straight from the opening lines: *"perfectly
  normal... last people you'd expect to be involved in anything strange or mysterious."*
- **The catch**: the question's causal link — "he dismissed the cat *because of* that need for
  normalcy" — is never actually stated in the text. The trait is given in paragraph 1; the dismissal
  is narrated in paragraph 6; there's no explicit connecting sentence. A reasonable inference, not a
  documented fact.
- **Distractors A and C** both reference real details from elsewhere in the same chapter (his job,
  Dudley's tantrum) — chronologically/causally wrong as the reason for *this* dismissal, but real
  enough to be mildly tempting rather than cleanly wrong.

**VERDICT: PARTIALLY-SUPPORTED**

## Takeaway

Neither of the pipeline's two built-in checks (`closed_book_check`'s leak detection,
`quality_check`'s structural critic) nor `evals/`'s own judge (`FactualCorrectnessMetric` — general
knowledge only, never touches the source text, see `docs/` note in `evals/README.md`) would have
caught the inferential leap in this question. Only grounding the check directly against the source
chapter text surfaced it. This is a live, concrete instance of the verification gap flagged at the
start of this investigation — not a hypothetical.
