"""
LangGraph pipeline for Part 1.2 — PDF study material → high-quality quiz set.

Graph shape (matches HOW_IT_WORKS.md §12):

     extract_text
          ↓
     chunk_by_topic
          ↓
     generate_questions ◄──────────┐
          ↓                        │
     quality_check                 │  retry if too few
          │                        │  validated Qs and
          ├─ enough pass ─► save   │  retry budget left
          └─ too few ─────────────┘

The retry edge uses `retry_count` in state (max 2 loops) so a bad first
generation gets another shot at the LLM before we give up.

State flows one direction through the graph; each node returns a partial
dict that gets merged into the accumulated state.

Requires GOOGLE_API_KEY (Google Gemini) in the environment. See .env.example.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Callable, List, Optional, TypedDict

import pdfplumber
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph

log = logging.getLogger('kahoot.ai')
if not log.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter('[ai] %(message)s'))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)


# ---- Tunables --------------------------------------------------------------

CHUNK_WORDS = 500                        # ~500 words per chunk
QUESTIONS_PER_CHUNK = 3                  # asked of the LLM per chunk
MIN_ACCEPTED_QUESTIONS = 5               # else we retry the generate step
MAX_RETRIES = 2
DEFAULT_TIME_LIMIT = 20                  # seconds per generated question
LLM_TEMPERATURE = 0.4                    # bit of variety for retry to actually differ
LLM_TIMEOUT_SECONDS = 45                 # cap on any single LLM call
MAX_CHUNKS_PER_RUN = 20                  # hard cap so a big PDF doesn't cost a fortune
MAX_OCR_PAGES = 15                       # cap on pages sent through vision-model OCR
OCR_RESOLUTION = 150                     # dpi for page-image rendering
LLM_MAX_TOKENS = 4096                    # generous headroom for 3 MCQs w/ math notation —
                                          # 2048 was tight enough to truncate mid-question

# Groq — see https://console.groq.com/docs/models for the current lineup;
# check there before changing these, models get deprecated/renamed over time.
DEFAULT_GROQ_MODEL = 'llama-3.3-70b-versatile'         # text generation + grading
DEFAULT_GROQ_VISION_MODEL = 'qwen/qwen3.6-27b'         # only vision-capable model on Groq currently


def _which_provider() -> Optional[str]:
    if os.environ.get('GROQ_API_KEY'):
        return 'groq'
    return None


# ---- State -----------------------------------------------------------------

class PipelineState(TypedDict, total=False):
    """Shared bag of values that flows through the graph."""
    pdf_path: str                        # input
    owner_id: int                        # input
    quiz_name: str                       # input
    quiz_description: str                # input

    raw_text: str                        # after extract
    chunks: List[str]                    # after chunk_by_topic

    draft_questions: List[dict]          # after generate
    validated_questions: List[dict]      # after quality_check

    retry_count: int                     # driven by conditional edge
    progress_cb: Optional[Callable[[str, float], None]]  # UI progress hook

    question_set_id: int                 # final result
    error: str                           # fatal-error marker


# ---- Node: extract ---------------------------------------------------------

def _extract_text(state: PipelineState) -> dict:
    _emit(state, 'Reading PDF…', 0.1)
    try:
        with pdfplumber.open(state['pdf_path']) as pdf:
            pages = [p.extract_text() or '' for p in pdf.pages]
        text = '\n\n'.join(pages).strip()
        page_count = len(pages)
        if not text:
            _emit(state, 'No text layer found — reading scanned pages via vision model…', 0.15)
            text = _ocr_via_vision(state['pdf_path'])
    except Exception as exc:  # corrupt PDF, wrong file type, LLM call failed, etc.
        log.error(f'PDF extract failed: {exc!r}')
        return {'error': f'Could not read PDF: {exc}'}
    log.info(f'extract_text: {len(text)} chars from {page_count} pages')
    if not text:
        return {'error': 'PDF contained no extractable text, even after OCR '
                          '(image quality too low, or a blank/handwritten document?)'}
    return {'raw_text': text}


_OCR_PROMPT = (
    'Transcribe all readable text from this page image, verbatim, in reading '
    'order. Output only the transcribed text — no commentary, no markdown '
    'fences. If the page has no readable text, output nothing.'
)


def _ocr_via_vision(pdf_path: str) -> str:
    """
    Fallback for scanned/image-only PDFs: pdfplumber found no text layer, so
    render each page to an image and ask Groq's vision model to read it.
    """
    import base64
    from io import BytesIO

    llm = _make_llm(temperature=0.0, vision=True)
    with pdfplumber.open(pdf_path) as pdf:
        ocr_pages = pdf.pages[:MAX_OCR_PAGES]
        page_texts = []
        for i, page in enumerate(ocr_pages):
            buf = BytesIO()
            page.to_image(resolution=OCR_RESOLUTION).original.save(buf, format='PNG')
            b64 = base64.b64encode(buf.getvalue()).decode('utf-8')
            resp = llm.invoke([HumanMessage(content=[
                {'type': 'text', 'text': _OCR_PROMPT},
                # OpenAI-compatible multimodal shape (Groq follows it) needs
                # image_url as {"url": ...}, not a bare string — Gemini's
                # integration was lenient about this, Groq's is not.
                {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}},
            ])])
            page_text = (resp.content or '').strip()
            log.info(f'ocr_via_vision: page {i + 1}/{len(ocr_pages)} -> {len(page_text)} chars')
            page_texts.append(page_text)
    return '\n\n'.join(page_texts).strip()


# ---- Node: chunk -----------------------------------------------------------

def _chunk_by_topic(state: PipelineState) -> dict:
    _emit(state, 'Splitting into sections…', 0.2)
    words = state['raw_text'].split()
    chunks = [
        ' '.join(words[i:i + CHUNK_WORDS])
        for i in range(0, len(words), CHUNK_WORDS)
    ]
    chunks = [c for c in chunks if len(c.split()) > 40]
    # Hard cap so a 100-page PDF doesn't burn 100 LLM calls
    chunks = chunks[:MAX_CHUNKS_PER_RUN]
    log.info(f'chunk_by_topic produced {len(chunks)} chunks')
    if not chunks:
        return {'error': 'PDF text too short to generate questions from '
                         '(need at least a paragraph of extractable text).'}
    return {'chunks': chunks}


# ---- Node: generate --------------------------------------------------------

_GENERATE_SYSTEM = """You write multiple-choice quiz questions for classroom
use. Given a passage of study material, produce {n} clear multiple-choice
questions (MCQs) that test factual understanding of it.

Rules:
- Each question has exactly 4 options, labelled A, B, C, D.
- Exactly one option is correct.
- Wrong options must be plausible, not silly — distractors should be things a
  student who half-remembers the material might pick.
- Question wording is standalone. Do NOT reference "the passage" or "the
  text" — the student won't see the source.
- Language of the questions matches the language of the passage.

Respond with a single JSON array. No prose, no code fences. Each element:
  {{"question": "...", "A": "...", "B": "...", "C": "...", "D": "...", "correct": "A"}}
"""


def _generate_questions(state: PipelineState) -> dict:
    n_chunks = len(state['chunks'])
    _emit(state, f'Connecting to Groq (0/{n_chunks})…', 0.3)
    try:
        llm = _make_llm()
    except Exception as exc:
        # Fatal: can't make the client at all (bad model name, missing/invalid key).
        log.error(f'ChatGroq init failed: {exc!r}')
        return {'error': f'Could not initialize Groq client: {exc}'}

    drafts: List[dict] = []
    first_error: Optional[str] = None
    fail_count = 0

    for i, chunk in enumerate(state['chunks']):
        _emit(state, f'Drafting questions ({i}/{n_chunks})…',
              0.35 + 0.35 * i / max(n_chunks, 1))
        try:
            resp = llm.invoke([
                SystemMessage(content=_GENERATE_SYSTEM.format(n=QUESTIONS_PER_CHUNK)),
                HumanMessage(content=f'Passage:\n\n{chunk}'),
            ])
            parsed = _parse_llm_json(resp.content)
            log.info(f'chunk {i + 1}/{n_chunks}: parsed {len(parsed)} draft questions')
            if not parsed:
                # _parse_llm_json already logged the real JSONDecodeError and
                # the text around the failure point (see its warning above) —
                # without this branch the failure mode is invisible and the
                # run just quietly starves.
                raise ValueError(
                    f'Groq response was not parseable as JSON '
                    f'({len(_content_to_text(resp.content))} chars received, '
                    f'see the _parse_llm_json warning above for exactly where it broke)'
                )
            drafts.extend(parsed)
        except Exception as exc:
            # One bad chunk doesn't kill the run, but keep the FIRST error
            # so we can surface something useful if EVERY chunk fails.
            fail_count += 1
            if first_error is None:
                first_error = f'{type(exc).__name__}: {exc}'
            log.warning(f'chunk {i + 1}/{n_chunks} failed: {first_error}')
            continue

    _emit(state, f'Drafted {len(drafts)} candidate questions.', 0.7)

    # If every chunk failed we should tell the user WHY, not just silently
    # return an empty draft list that a retry loop will also fail on.
    if not drafts and first_error and fail_count == n_chunks:
        return {'draft_questions': [], 'error': f'All LLM calls failed. First error: {first_error}'}

    # Explicitly clear a stale error from an earlier failed attempt — LangGraph
    # merges returned dicts into state and does not drop keys we don't return,
    # so without this a retry that succeeds after a fully-failed first pass
    # would still carry the old error through to run_pipeline's final check.
    return {'draft_questions': drafts, 'error': None}


# ---- Node: quality check ---------------------------------------------------

_QUALITY_SYSTEM = """You grade quiz questions written by another model. For
each question decide PASS or FAIL. FAIL if any of:
- more than one option is correct
- correct option is not among A-D
- question is unclear, ambiguous, or too trivial
- an option repeats the answer verbatim from the question
- distractors are obviously nonsense (e.g. "cheese" as a distractor for a math question)

Respond with a JSON array of booleans, same length as the input, in the same
order. No prose."""


def _quality_check(state: PipelineState) -> dict:
    _emit(state, 'Checking quality…', 0.75)
    drafts = state.get('draft_questions') or []
    if not drafts:
        return {'validated_questions': []}
    llm = _make_llm(temperature=0.0)  # deterministic grading
    try:
        resp = llm.invoke([
            SystemMessage(content=_QUALITY_SYSTEM),
            HumanMessage(content=json.dumps(drafts, ensure_ascii=False)),
        ])
        verdicts = _parse_llm_json(resp.content)
        # verdicts may be booleans, or {"pass": true} objects — accept either
        keep = [
            q for q, v in zip(drafts, verdicts)
            if v is True or (isinstance(v, dict) and v.get('pass'))
        ]
    except Exception:
        # If the grader fails, keep the drafts rather than dropping everything.
        keep = drafts
    # Dedupe by exact question text (case-insensitive)
    seen: set = set()
    unique: List[dict] = []
    for q in keep:
        key = (q.get('question') or '').strip().lower()
        if key and key not in seen:
            seen.add(key)
            unique.append(q)
    return {'validated_questions': unique}


# ---- Conditional edge ------------------------------------------------------

def _should_retry(state: PipelineState) -> str:
    validated = state.get('validated_questions') or []
    retries = state.get('retry_count', 0)
    if len(validated) < MIN_ACCEPTED_QUESTIONS and retries < MAX_RETRIES:
        return 'retry'
    return 'save'


def _bump_retry(state: PipelineState) -> dict:
    _emit(state, 'Not enough good questions — retrying…', 0.65)
    return {'retry_count': state.get('retry_count', 0) + 1}


# ---- Node: save ------------------------------------------------------------

def _save(state: PipelineState) -> dict:
    """Persist validated questions as a QuestionSet owned by the host."""
    _emit(state, 'Saving question set…', 0.95)
    # Local import to avoid a circular import at module load time
    from app.extensions import db
    from app.models import QuestionSet, Question

    validated = state.get('validated_questions') or []
    if not validated:
        return {'error': 'No usable questions were produced'}

    qs = QuestionSet(
        name=state.get('quiz_name') or 'Generated from PDF',
        description=(state.get('quiz_description')
                     or 'Auto-generated from an uploaded PDF (please review).'),
        owner_id=state['owner_id'],
    )
    db.session.add(qs)
    db.session.flush()  # get qs.id

    for i, q in enumerate(validated):
        db.session.add(Question(
            question_set_id=qs.id,
            position=i,
            text=(q.get('question') or '').strip(),
            option_a=(q.get('A') or '').strip(),
            option_b=(q.get('B') or '').strip(),
            option_c=(q.get('C') or '').strip() or None,
            option_d=(q.get('D') or '').strip() or None,
            correct_option=(q.get('correct') or 'A').upper()[:1],
            time_limit=DEFAULT_TIME_LIMIT,
        ))
    db.session.commit()
    _emit(state, 'Done.', 1.0)
    return {'question_set_id': qs.id}


# ---- Build the graph -------------------------------------------------------

def _build_graph():
    graph = StateGraph(PipelineState)
    graph.add_node('extract', _extract_text)
    graph.add_node('chunk', _chunk_by_topic)
    graph.add_node('generate', _generate_questions)
    graph.add_node('quality', _quality_check)
    graph.add_node('bump_retry', _bump_retry)
    graph.add_node('save', _save)

    graph.set_entry_point('extract')
    graph.add_edge('extract', 'chunk')
    graph.add_edge('chunk', 'generate')
    graph.add_edge('generate', 'quality')
    graph.add_conditional_edges('quality', _should_retry, {
        'retry': 'bump_retry',
        'save': 'save',
    })
    graph.add_edge('bump_retry', 'generate')
    graph.add_edge('save', END)
    return graph.compile()


_COMPILED_GRAPH = None


def _graph():
    global _COMPILED_GRAPH
    if _COMPILED_GRAPH is None:
        _COMPILED_GRAPH = _build_graph()
    return _COMPILED_GRAPH


# ---- Public entrypoint -----------------------------------------------------

def run_pipeline(
    pdf_path: str,
    owner_id: int,
    quiz_name: str = '',
    quiz_description: str = '',
    progress_cb: Optional[Callable[[str, float], None]] = None,
) -> int:
    """Run the pipeline synchronously and return the created QuestionSet id.

    Meant to be called from a background task — total runtime is on the
    order of tens of seconds for a small PDF, minutes for a large one.
    Raises RuntimeError on any fatal state['error'].
    """
    if _which_provider() is None:
        raise RuntimeError(
            'No LLM API key configured. Set GROQ_API_KEY (free at '
            'https://console.groq.com/keys).'
        )
    initial: PipelineState = {
        'pdf_path': pdf_path,
        'owner_id': owner_id,
        'quiz_name': quiz_name,
        'quiz_description': quiz_description,
        'retry_count': 0,
        'progress_cb': progress_cb,
    }
    log.info(f'run_pipeline starting: pdf={pdf_path} owner={owner_id}')
    final = _graph().invoke(initial)
    if final.get('error'):
        log.error(f'run_pipeline error: {final["error"]}')
        raise RuntimeError(final['error'])
    log.info(f'run_pipeline done: question_set_id={final.get("question_set_id")}')
    return final['question_set_id']


# ---- Helpers ---------------------------------------------------------------

def _make_llm(temperature: float = LLM_TEMPERATURE, vision: bool = False):
    """
    Build the Groq chat model. Requires GROQ_API_KEY. See .env.example.
    `vision=True` selects the (currently sole) vision-capable Groq model,
    used only by the scanned-PDF OCR fallback.
    """
    if _which_provider() == 'groq':
        from langchain_groq import ChatGroq
        default_model = DEFAULT_GROQ_VISION_MODEL if vision else DEFAULT_GROQ_MODEL
        env_var = 'GROQ_VISION_MODEL' if vision else 'GROQ_MODEL'
        return ChatGroq(
            model=os.environ.get(env_var, default_model),
            temperature=temperature,
            max_tokens=LLM_MAX_TOKENS,
            timeout=LLM_TIMEOUT_SECONDS,
        )
    raise RuntimeError(
        'No LLM API key configured. Set GROQ_API_KEY (get one free at '
        'https://console.groq.com/keys).'
    )


def _emit(state: PipelineState, message: str, progress: float) -> None:
    cb = state.get('progress_cb')
    if cb:
        try:
            cb(message, progress)
        except Exception:
            pass


_JSON_FENCE_RE = re.compile(r'```(?:json)?\s*(.*?)```', re.DOTALL)


def _content_to_text(raw) -> str:
    """Normalize a LangChain message .content value to plain text.

    Newer Gemini models return content as a list of block dicts
    (e.g. [{'type': 'text', 'text': '...'}]) instead of a plain string —
    without this, str(raw) on the list produces a Python-repr string
    (single-quoted, escaped) that can never parse as JSON.
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


def _parse_llm_json(raw):
    """Strip common LLM wrapper cruft (code fences, prose) and return the parsed JSON.
    Falls back to an empty list rather than raising, so one bad response doesn't kill the run."""
    text = _content_to_text(raw).strip()
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
        log.warning(f'_parse_llm_json: {exc} — near {window!r} (total {len(text)} chars)')
        return []
