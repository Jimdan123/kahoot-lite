"""Shared state bag threaded through every node in the pipeline graph."""
from __future__ import annotations

from typing import Callable, List, Optional, TypedDict


class PipelineState(TypedDict, total=False):
    """Shared bag of values that flows through the graph."""
    pdf_path: str                        # input
    owner_id: int                        # input
    quiz_name: str                       # input
    quiz_description: str                # input
    practice_questions_per_difficulty: int  # input — see config.PRACTICE_QUESTIONS_PER_DIFFICULTY

    raw_text: str                        # after extract
    chunks: List[str]                    # after chunk_by_topic

    chunk_records: List[dict]            # after comprehend_chunks — one record per chunk
    comprehension: dict                  # after merge_comprehension — deduped + cross-chunk links
    search_context: List[dict]           # after enrich_context — optional external "why" facts;
                                          # [] if the doc wasn't thin or TAVILY_API_KEY is unset

    draft_questions: List[dict]          # after generate_questions
    closed_book_results: List[dict]      # after closed_book_check, aligned with draft_questions
    validated_questions: List[dict]      # after quality_check

    practice_questions: List[dict]       # after generate_practice_questions (separate track)

    retry_count: int                     # driven by conditional edge
    progress_cb: Optional[Callable[[str, float], None]]  # UI progress hook

    question_set_id: int                 # final result
    error: str                           # fatal-error marker
