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

Requires ANTHROPIC_API_KEY in the environment. See .env.example.
"""
from __future__ import annotations

import json
import os
import re
from typing import Callable, List, Optional, TypedDict

import pdfplumber
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph


# ---- Tunables --------------------------------------------------------------

CHUNK_WORDS = 500                        # ~500 words per chunk
QUESTIONS_PER_CHUNK = 3                  # asked of the LLM per chunk
MIN_ACCEPTED_QUESTIONS = 5               # else we retry the generate step
MAX_RETRIES = 2
DEFAULT_TIME_LIMIT = 20                  # seconds per generated question
LLM_MODEL = 'claude-sonnet-4-6'          # fast + good enough for MCQ drafting
LLM_TEMPERATURE = 0.4                    # bit of variety for retry to actually differ


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
    except Exception as exc:  # corrupt PDF, wrong file type, etc.
        return {'error': f'Could not read PDF: {exc}'}
    if not text:
        return {'error': 'PDF contained no extractable text (scanned image?)'}
    return {'raw_text': text}


# ---- Node: chunk -----------------------------------------------------------

def _chunk_by_topic(state: PipelineState) -> dict:
    _emit(state, 'Splitting into sections…', 0.2)
    words = state['raw_text'].split()
    chunks = [
        ' '.join(words[i:i + CHUNK_WORDS])
        for i in range(0, len(words), CHUNK_WORDS)
    ]
    # Only keep chunks with at least a paragraph of content
    chunks = [c for c in chunks if len(c.split()) > 40]
    if not chunks:
        return {'error': 'PDF text too short to generate questions from'}
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
    _emit(state, 'Drafting questions…', 0.4)
    llm = _make_llm()
    drafts: List[dict] = []
    for i, chunk in enumerate(state['chunks']):
        try:
            resp = llm.invoke([
                SystemMessage(content=_GENERATE_SYSTEM.format(n=QUESTIONS_PER_CHUNK)),
                HumanMessage(content=f'Passage:\n\n{chunk}'),
            ])
            drafts.extend(_parse_llm_json(resp.content))
        except Exception:
            # One bad chunk doesn't kill the whole run.
            continue
        _emit(state, f'Drafting questions… ({i + 1}/{len(state["chunks"])})',
              0.4 + 0.3 * (i + 1) / len(state['chunks']))
    return {'draft_questions': drafts}


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
    if not os.environ.get('ANTHROPIC_API_KEY'):
        raise RuntimeError(
            'ANTHROPIC_API_KEY is not set. Add it to .env or the Render '
            'environment before using the PDF pipeline.'
        )
    initial: PipelineState = {
        'pdf_path': pdf_path,
        'owner_id': owner_id,
        'quiz_name': quiz_name,
        'quiz_description': quiz_description,
        'retry_count': 0,
        'progress_cb': progress_cb,
    }
    final = _graph().invoke(initial)
    if final.get('error'):
        raise RuntimeError(final['error'])
    return final['question_set_id']


# ---- Helpers ---------------------------------------------------------------

def _make_llm(temperature: float = LLM_TEMPERATURE):
    return ChatAnthropic(model=LLM_MODEL, temperature=temperature, max_tokens=2048)


def _emit(state: PipelineState, message: str, progress: float) -> None:
    cb = state.get('progress_cb')
    if cb:
        try:
            cb(message, progress)
        except Exception:
            pass


_JSON_FENCE_RE = re.compile(r'```(?:json)?\s*(.*?)```', re.DOTALL)


def _parse_llm_json(raw):
    """Strip common LLM wrapper cruft (code fences, prose) and return the parsed JSON.
    Falls back to an empty list rather than raising, so one bad response doesn't kill the run."""
    if not isinstance(raw, str):
        raw = str(raw)
    text = raw.strip()
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    # If the model prefixed something like "Here are the questions:", find the first bracket.
    for opener in ('[', '{'):
        idx = text.find(opener)
        if idx > 0:
            text = text[idx:]
            break
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []
