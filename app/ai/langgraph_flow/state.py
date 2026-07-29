"""Shared state bag threaded through every node in the pipeline graph."""
from __future__ import annotations

from typing import Callable, List, Optional, TypedDict


class PipelineState(TypedDict, total=False):
    """Shared bag of values that flows through the graph."""
    pdf_path: str                        # input
    owner_id: int                        # input
    quiz_name: str                       # input
    quiz_description: str                # input

    raw_text: str                        # after extract
    chunks: List[str]                    # after chunk_by_topic

    chunk_records: List[dict]            # after comprehend_chunks — one record per chunk
    comprehension: dict                  # after merge_comprehension — deduped + cross-chunk links

    draft_questions: List[dict]          # after generate_questions
    closed_book_results: List[dict]      # after closed_book_check, aligned with draft_questions
    validated_questions: List[dict]      # after quality_check

    retry_count: int                     # driven by conditional edge
    progress_cb: Optional[Callable[[str, float], None]]  # UI progress hook

    question_set_id: int                 # final result
    error: str                           # fatal-error marker
