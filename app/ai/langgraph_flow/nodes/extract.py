"""Stage 1 — pull raw text out of the uploaded PDF (with a local Tesseract
OCR fallback for scanned pages that have no text layer)."""
from __future__ import annotations

import logging

import pdfplumber
import pytesseract

from app.ai.langgraph_flow.config import MAX_OCR_PAGES, OCR_LANGUAGES, OCR_RESOLUTION
from app.ai.langgraph_flow.progress import emit
from app.ai.langgraph_flow.state import PipelineState

log = logging.getLogger('kahoot.ai')


def extract_text(state: PipelineState) -> dict:
    emit(state, 'Reading PDF…', 0.1)
    try:
        with pdfplumber.open(state['pdf_path']) as pdf:
            pages = [p.extract_text() or '' for p in pdf.pages]
        text = '\n\n'.join(pages).strip()
        page_count = len(pages)
        if not text:
            emit(state, 'No text layer found — running local OCR on scanned pages…', 0.15)
            text = _ocr_via_tesseract(state['pdf_path'])
    except pytesseract.TesseractNotFoundError:
        log.error('tesseract binary not found on PATH')
        return {'error': (
            'This server cannot OCR scanned PDFs: the "tesseract" binary '
            'is not installed. Install tesseract-ocr (see README) or '
            'upload a PDF that has a real text layer instead.'
        )}
    except Exception as exc:  # corrupt PDF, wrong file type, OCR call failed, etc.
        log.error(f'PDF extract failed: {exc!r}')
        return {'error': f'Could not read PDF: {exc}'}
    log.info(f'extract_text: {len(text)} chars from {page_count} pages')
    if not text:
        return {'error': 'PDF contained no extractable text, even after OCR '
                          '(image quality too low, or a blank/handwritten document?)'}
    return {'raw_text': text}


def _ocr_via_tesseract(pdf_path: str) -> str:
    """
    Fallback for scanned/image-only PDFs: pdfplumber found no text layer, so
    render each page to an image and run it through local Tesseract OCR.
    """
    with pdfplumber.open(pdf_path) as pdf:
        ocr_pages = pdf.pages[:MAX_OCR_PAGES]
        page_texts = []
        for i, page in enumerate(ocr_pages):
            image = page.to_image(resolution=OCR_RESOLUTION).original.convert('L')
            try:
                page_text = pytesseract.image_to_string(image, lang=OCR_LANGUAGES).strip()
            except pytesseract.TesseractError as exc:
                log.error(f'tesseract failed on page {i + 1}: {exc!r}')
                raise RuntimeError(
                    f'OCR failed on page {i + 1} (lang="{OCR_LANGUAGES}"): {exc}. '
                    'Is the matching tesseract-ocr-<lang> data package installed?'
                ) from exc
            log.info(f'ocr_via_tesseract: page {i + 1}/{len(ocr_pages)} -> {len(page_text)} chars')
            page_texts.append(page_text)
    return '\n\n'.join(page_texts).strip()
