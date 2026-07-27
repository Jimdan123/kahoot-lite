"""
LangGraph pipeline for Part 1.2 — PDF study material → high-quality quiz set.

Planned graph (to be implemented):

  1. extract_text     — pdfplumber
  2. chunk_by_topic   — split into ~500-word sections
  3. generate_questions — LLM: 3 MCQs per chunk
  4. quality_check    — LLM validates each; conditional edge retries on fail (max 2x)
  5. dedupe & format  — remove near-duplicates, normalize option letters
  6. save             — creates a QuestionSet the host can review + edit

See LangGraph Quickstart: https://langchain-ai.github.io/langgraph/tutorials/introduction/
"""
from typing import TypedDict


class PipelineState(TypedDict, total=False):
    pdf_path: str
    owner_id: int
    raw_text: str
    chunks: list
    draft_questions: list
    quality_passed: list
    retry_count: int
    question_set_id: int


def run_pipeline(pdf_path: str, owner_id: int) -> int:
    """Placeholder — will return the created QuestionSet id."""
    raise NotImplementedError('Part 1.2 not yet implemented')
