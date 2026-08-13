# Live token count during question generation

## Context

The PDF → question-set pipeline (Part 1.2, `app/ai/langgraph_flow/`) already tracks cumulative LLM token usage across a run (`PipelineState['token_usage']`, mutated in place by `llm_utils.py`'s `_accumulate_usage`) and saves the final total on `QuestionSet.total_tokens`, shown once generation finishes on the quiz detail page (`app/templates/quiz/detail.html:5-6`, "Generated using ~N,NNN tokens"). Users only see this after the fact — the processing page (`app/templates/ai/processing.html`) shows a progress bar and status message but no running token count while the pipeline is actually burning them.

Investigation found the existing progress-reporting pipe is a clean fit for this, requiring no new architecture:

- Every pipeline node calls `emit(state, message, progress)` (`app/ai/langgraph_flow/progress.py`) at its own milestones (17 call sites across 8 node files) — `emit()` already receives the full `state` dict, which includes the live, in-place-mutated `token_usage` dict.
- `emit()` invokes `state['progress_cb']`, which `app/ai/routes.py`'s `upload()` wires to a closure that calls `jobs.update(job_id, status='running', message=..., progress=...)` — an in-memory `JobState` (`app/ai/jobs.py`).
- The browser already polls `GET /ai/status/<job_id>` every 1.5s (`app/templates/ai/processing.html`) and renders `message`/`progress` into the page.

Because `emit()` already has `state` in scope, the running token total can be read and threaded through this exact pipe without touching any of the 8 node files that call `emit()`.

## Decisions

- Live count appears as a small muted line under the progress bar on the processing page, matching the style of the existing post-generation line on the detail page.
- Update granularity matches the progress bar's existing granularity — one value per `emit()` call (roughly one per pipeline node/step), not per individual LLM call. No new instrumentation inside `llm_utils.py`'s `invoke_json()`.
- The line is hidden while the running total is 0 (nothing burned yet — e.g. during the initial "Reading PDF…" step, before any LLM call has happened).
- Formatting mirrors the existing detail-page line: `~{tokens, comma-separated} tokens used so far`.
- The existing post-generation summary on the quiz detail page is untouched — this is additive, not a replacement.

## 1. `app/ai/langgraph_flow/progress.py`

`emit()` reads the running total off `state` and passes it as a third positional argument to the callback:

```python
def emit(state: PipelineState, message: str, progress: float) -> None:
    cb = state.get('progress_cb')
    if cb:
        tokens = (state.get('token_usage') or {}).get('total_tokens', 0)
        try:
            cb(message, progress, tokens)
        except Exception:
            pass
```

## 2. `app/ai/jobs.py`

`JobState` gets one new field, defaulting to 0 so already-running jobs (mid-deploy) degrade gracefully instead of erroring:

```python
    tokens: int = 0                       # running total_tokens as of the last progress update
```

## 3. `app/ai/routes.py`

The `progress` closure inside `upload()`'s `_worker()` accepts the new third argument and forwards it:

```python
def progress(message, fraction, tokens=0):
    jobs.update(job.job_id, status='running',
                message=message, progress=fraction, tokens=tokens)
```

The `default=0` keyword keeps this call signature backward-compatible with any other caller that doesn't pass tokens (none currently exist, but this avoids a silent `TypeError` if one is added later).

`/ai/status/<job_id>`'s JSON response adds one key:

```python
return jsonify({
    'status': job.status,
    'message': job.message,
    'progress': job.progress,
    'tokens': job.tokens,
    ...
})
```

## 4. `app/templates/ai/processing.html`

A new element under the progress bar, hidden by default:

```html
<div id="token-count" class="text-muted small d-none"></div>
```

In the poll loop, after updating `msg`/`bar`:

```js
const tokEl = document.getElementById('token-count');
if (data.tokens > 0) {
    tokEl.textContent = `~${data.tokens.toLocaleString()} tokens used so far`;
    tokEl.classList.remove('d-none');
}
```

`toLocaleString()` gives the same comma-grouped formatting as the Python `'{:,}'.format(...)` used on the detail page, so the two displays read consistently.

## Verification

- Start a PDF upload/generation run locally, watch the processing page: confirm the token line stays hidden during the initial "Reading PDF…" step (before any LLM call), then appears and increases at each subsequent step (comprehension, generation, critic checks), reaching a final value consistent with what the detail page shows after redirect.
- Confirm a page load immediately after starting a job (before any `emit()` with tokens > 0 has fired) doesn't show a stray "~0 tokens" line.
- Confirm no change to the final `QuestionSet.total_tokens` value or the detail-page summary line — this feature only adds a live view during the run.
