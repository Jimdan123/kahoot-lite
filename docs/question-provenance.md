# Where a generated question actually comes from

Traces the real data lineage from the uploaded PDF to a finished question's `hop_a`/`hop_b`/
`question`/options, using the actual field names and prompts in
`app/ai/langgraph_flow/`. The short version: **a question is not written directly from the source
text.** It's written from an LLM's own prior extraction of that text — already one step removed —
plus (for one specific pairing type) content the model invents itself.

## The chain

**1. `extract_text` (`nodes/extract.py`) → `raw_text`**
Plain PDF text, with `[Table: ...]`/`[Formula: ...]`/`[Figure: ...]` annotation blocks appended
inline for content plain text extraction can't see. This is the one point in the whole chain that's
a direct, mechanical transcription of the source document (plus vision-model descriptions for
figures/formulas/glyphs — see `docs/` for the caveat that those aren't independently verified
either).

**2. `chunk_by_topic` (`nodes/chunk.py`) → `chunks[]`**
`raw_text` sliced into ~500-word (`CHUNK_WORDS`) pieces, annotation blocks kept atomic. The
`chunk_text` a question is ultimately generated from — the literal "Passage:" text the generate LLM
call sees — is one of these chunks, verbatim, unedited by any LLM.

**3. `comprehend_chunks` (`nodes/comprehend.py`, Stage 3) → `chunk_records[]`**
**One LLM call per chunk.** This is where a question's actual factual content is born — not in the
generate step. Each call reads one chunk and extracts:

- `claims[]` — factual assertions with explanatory weight
- `definitions[]` — terms the passage defines
- `mechanisms[]` — causal/procedural chains, as ordered steps
- `quantities[]` — numbers/thresholds with what they measure

Every item **must** carry a `span`: *"the shortest verbatim quote (max 20 words) from the passage
that supports it. If you cannot produce a span, drop the item."* This is the pipeline's only
grounding mechanism for these items — and it is **never checked programmatically**. Nothing
downstream confirms the LLM's claimed span is a real substring of the chunk. It's a prompted
constraint, not an enforced one.

**4. `merge_comprehension` (`nodes/comprehend.py`, Stage 4) → `comprehension`**
**One more LLM call**, across *all* chunk records together: dedupes near-identical claims/
definitions across chunks, finds cross-chunk `links` (pairing type #1 below — the highest-value
output of this stage), extracts a short `topic` phrase, and copies up to 8 `existing_exercises`
excerpts. Falls back to `_naive_merge` (plain concatenation, no dedup, no links) if this call fails
— confirmed to actually fire in `docs/gen-pipeline-test-log.md`'s live run (qwen's per-minute token
limit was exceeded on this call).

**5. `enrich_context` (`nodes/enrich.py`, optional) → `search_context`**
Only if triggered (a thin/definitional document, or — on a retry — a high closed-book leak rate)
and `TAVILY_API_KEY` is set: adds external "why does this matter" facts from a real web search,
distilled by one more LLM call. This is the **only** point anywhere in the chain where content from
outside the document can enter a question. Skipped entirely when this node never runs (as in the
live test).

**6. `_build_context` (`nodes/generate.py`) — assembly, no LLM call**
For the *specific* chunk being asked about, pulls together plain text:
- every claim/definition from step 3 whose `chunk_index`(es) include this chunk
- every mechanism/quantity from step 3 tagged with this `chunk_index`
- every cross-chunk `link` from step 4 touching this chunk (capped at `MAX_LINKS_PER_CHUNK = 2`)
- any `search_context` facts from step 5, if present, explicitly labeled "background only"

This assembled block, plus the raw chunk text from step 2, is the entire input to the generate LLM
call. The generate model does **not** re-derive facts from the passage independently — it works
from this pre-digested record.

**7. The generate LLM call itself (`_GENERATE_SYSTEM`, `nodes/generate.py`)**
Told to pick two "hops" from the assembled context, in this preference order:

| # | Pairing | Where the content actually comes from |
|---|---|---|
| 1 | LINK | step 4's cross-chunk link — a fact from *this* chunk + a fact from *another* chunk |
| 2 | APPLICATION | a document mechanism/formula, applied by **the model itself** to new numbers/scenario it invents and solves — this hop's specific content is not in the document at all |
| 3 | mechanism + constraining quantity | both from step 3, same chunk |
| 4 | claim + the definition/assumption it depends on | both from step 3, same chunk |
| 5 | two claims that reinforce/conflict | both from step 3, same chunk |
| 6 | document definition + EXTERNAL CONTEXT "why" fact | one from step 3, one from step 5 — never both from step 5 |

**Field order is deliberate and enforced by the prompt structure**: `hop_a`/`hop_b` must be
identified *first*, then `tests` (what a one-hop student gets wrong), then the actual
`question`/options/`correct`, then `difficulty`/`time_limit` last. Per the module's own docstring:
*"an LLM filling JSON top-to-bottom that writes `question` first will happily backfill a
justification after the fact"* — so the reasoning is locked in before the prose that could
rationalize a bad pairing exists.

## Concrete trace, from the live test (`docs/gen-pipeline-test-log.md`)

The question generated there had `hop_a`/`hop_b` both tagged `"CLAIM: ..."` — pairing type #5.
That means: both facts trace back to chunk 0's `claims[]` list from step 3 (one `comprehend_chunks`
LLM call), each originally carrying an unverified `span` claim. The independent verifier's separate
check against the raw chapter text (not this pipeline) happened to confirm both spans were
near-verbatim accurate *in that one instance* — but that confirmation came from a check outside
this pipeline entirely; the pipeline itself never performs it.

## The one-sentence version

A question's content comes from: **one LLM's reading of one 500-word chunk (step 3), occasionally
combined with a second LLM's reading of the whole document for cross-chunk links (step 4),
occasionally with a third LLM's invented scenario applied to a real document procedure (pairing
#2), occasionally with a fourth LLM's distillation of live web search results (step 5) — assembled
by a fifth LLM into the final question (step 7).** Up to five separate LLM calls, zero
programmatic checks against the original source text, before a question ever reaches the critic
checks in `docs/critic-verification-criteria.md`.
