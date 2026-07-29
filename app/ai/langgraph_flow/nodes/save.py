"""Final stage — persist validated questions as a QuestionSet owned by the host."""
from __future__ import annotations

from app.ai.langgraph_flow.config import DEFAULT_TIME_LIMIT
from app.ai.langgraph_flow.progress import emit
from app.ai.langgraph_flow.state import PipelineState


def save(state: PipelineState) -> dict:
    emit(state, 'Saving question set…', 0.95)
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
    emit(state, 'Done.', 1.0)
    return {'question_set_id': qs.id}
