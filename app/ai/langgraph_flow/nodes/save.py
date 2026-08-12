"""Final stage — persist validated questions as a QuestionSet owned by the host."""
from __future__ import annotations

from app.ai.langgraph_flow.config import FALLBACK_TIME_LIMIT, MAX_TIME_LIMIT, MIN_TIME_LIMIT
from app.ai.langgraph_flow.progress import emit
from app.ai.langgraph_flow.state import PipelineState


def save(state: PipelineState) -> dict:
    emit(state, 'Saving question set…', 0.95)
    # Local import to avoid a circular import at module load time
    from app.extensions import db
    from app.models import QuestionSet, Question

    # Two independent tracks merge here: the document-grounded two-hop
    # questions (validated_questions) and the web-searched topic-practice
    # questions (practice_questions, nodes/practice.py) — see graph.py.
    combined = (state.get('validated_questions') or []) + (state.get('practice_questions') or [])
    if not combined:
        return {'error': 'No usable questions were produced'}

    qs = QuestionSet(
        name=state.get('quiz_name') or 'Generated from PDF',
        description=(state.get('quiz_description')
                     or 'Auto-generated from an uploaded PDF (please review).'),
        owner_id=state['owner_id'],
        total_tokens=(state.get('token_usage') or {}).get('total_tokens'),
    )
    db.session.add(qs)
    db.session.flush()  # get qs.id

    for i, q in enumerate(combined):
        db.session.add(Question(
            question_set_id=qs.id,
            position=i,
            text=(q.get('question') or '').strip(),
            option_a=(q.get('A') or '').strip(),
            option_b=(q.get('B') or '').strip(),
            option_c=(q.get('C') or '').strip() or None,
            option_d=(q.get('D') or '').strip() or None,
            correct_option=(q.get('correct') or 'A').upper()[:1],
            time_limit=_time_limit(q),
            difficulty=q.get('difficulty') if q.get('difficulty') in ('easy', 'medium', 'hard') else None,
            source=q.get('source') if q.get('source') in ('closed_book',) else None,
        ))
    db.session.commit()
    emit(state, 'Done.', 1.0)
    return {'question_set_id': qs.id}


def _time_limit(q: dict) -> int:
    """Every question reaching here should already carry an LLM-reasoned
    time_limit (generate.py / practice.py both normalize it) — this is only
    a last-ditch safety net against a malformed/missing value."""
    try:
        value = int(q.get('time_limit'))
    except (TypeError, ValueError):
        return FALLBACK_TIME_LIMIT
    return max(MIN_TIME_LIMIT, min(MAX_TIME_LIMIT, value))
