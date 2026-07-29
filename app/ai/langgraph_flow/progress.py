"""Progress-callback plumbing, split out so every node module can import it
without reaching back into graph.py (which would create a circular import)."""
from __future__ import annotations

from app.ai.langgraph_flow.state import PipelineState


def emit(state: PipelineState, message: str, progress: float) -> None:
    cb = state.get('progress_cb')
    if cb:
        try:
            cb(message, progress)
        except Exception:
            pass
