"""Stage 1 — pull raw text out of the uploaded PDF (with a vision-model OCR
fallback for scanned pages that have no text layer)."""
from __future__ import annotations

import logging

import pdfplumber
from langchain_core.messages import HumanMessage

from app.ai.langgraph_flow.config import MAX_OCR_PAGES, OCR_RESOLUTION
from app.ai.langgraph_flow.llm_utils import make_llm
from app.ai.langgraph_flow.progress import emit
from app.ai.langgraph_flow.state import PipelineState

log = logging.getLogger('kahoot.ai')

_OCR_PROMPT = (
    'Transcribe all readable text from this page image, verbatim, in reading '
    'order. Output only the transcribed text — no commentary, no markdown '
    'fences. If the page has no readable text, output nothing.'
)


def extract_text(state: PipelineState) -> dict:
    emit(state, 'Reading PDF…', 0.1)
    try:
        with pdfplumber.open(state['pdf_path']) as pdf:
            pages = [p.extract_text() or '' for p in pdf.pages]
        text = '\n\n'.join(pages).strip()
        page_count = len(pages)
        if not text:
            emit(state, 'No text layer found — reading scanned pages via vision model…', 0.15)
            text = _ocr_via_vision(state['pdf_path'])
    except Exception as exc:  # corrupt PDF, wrong file type, LLM call failed, etc.
        log.error(f'PDF extract failed: {exc!r}')
        return {'error': f'Could not read PDF: {exc}'}
    log.info(f'extract_text: {len(text)} chars from {page_count} pages')
    if not text:
        return {'error': 'PDF contained no extractable text, even after OCR '
                          '(image quality too low, or a blank/handwritten document?)'}
    return {'raw_text': text}


def _ocr_via_vision(pdf_path: str) -> str:
    """
    Fallback for scanned/image-only PDFs: pdfplumber found no text layer, so
    render each page to an image and ask Groq's vision model to read it.
    """
    import base64
    from io import BytesIO

    llm = make_llm(temperature=0.0, vision=True)
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
