"""Regression tests for the WORKER TIMEOUT bug diagnosed 2026-08-12: a
gevent-worker-starving lack of cooperative yields in extract.py's per-page
PDF parsing. See app/ai/langgraph_flow/nodes/extract.py's
_find_template_image_sizes docstring for the full root-cause writeup.

Root cause (confirmed live, not theoretical): pdfplumber/pdfminer lazily
parses a page's full object model (images, chars, curves, lines, rects
together) on first access to ANY of them. _find_template_image_sizes's
`page.images` access — unconditional, called first thing in extract_text(),
before any yield existed anywhere in this file — was that first access for
every page of every uploaded PDF. Measured: 2.4s of continuous, unyielded
parsing across 5 pages of a synthetic chart-heavy test document, with ZERO
gevent scheduling opportunities for any other traffic (other users' live
games, WebSocket pings) for that entire span.

These tests are deterministic (no wall-clock/timing assertions, which would
be flaky across machines) — they verify the FIX MECHANISM directly: that a
positive-duration gevent.sleep() call fires once per page in the functions
that do the expensive parsing, and that the vector-shape pre-scan is skipped
entirely when vision isn't configured. The actual event-loop-starvation
demonstration (a real gevent heartbeat greenlet measuring scheduling gaps
during a real extract_text() call against a synthetic stress PDF) was done
manually during diagnosis and is not re-encoded here as a committed test —
it needs a slow-enough-to-matter PDF and gevent's monkey-patching active,
both awkward to make fast/deterministic/CI-safe. This file instead locks
down the specific code-level fix so it can't silently regress.

Run: pytest tests/test_extract_gevent_yield.py -v
"""
import types
from unittest.mock import patch

from app.ai.langgraph_flow.nodes import extract as extract_module
from app.ai.langgraph_flow.nodes.extract import (
    _find_template_image_sizes,
    _find_template_shape_sizes,
)

# Minimal valid one-page PDF, hand-written — avoids needing a PDF-writing
# library (none is a real project dependency; matplotlib, used to build the
# stress-test PDF during live diagnosis, is only a transitive dependency of
# mlflow's eval-only tooling, not requirements.txt).
_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << >> >>
endobj
xref
0 4
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
trailer
<< /Size 4 /Root 1 0 R >>
startxref
201
%%EOF"""


def _mock_page(**overrides):
    defaults = {'images': [], 'chars': [], 'curves': [], 'lines': [], 'rects': []}
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


class TestFindTemplateImageSizesYields:
    """This is the actual fix for the reported bug — see module docstring."""

    def test_yields_once_per_page_with_positive_duration(self):
        pages = [_mock_page() for _ in range(4)]
        with patch.object(extract_module, 'gevent') as mock_gevent:
            _find_template_image_sizes(pages)
        assert mock_gevent.sleep.call_count == 4
        for call in mock_gevent.sleep.call_args_list:
            (duration,), _ = call
            assert duration > 0, 'gevent.sleep(0) does not actually yield — see extract_text()'

    def test_yield_happens_before_the_expensive_access(self):
        # A page whose .images access raises if called before the yield —
        # proves the yield is genuinely first, not just present somewhere.
        call_order = []

        class TracingImages(list):
            def __iter__(self):
                call_order.append('images_accessed')
                return super().__iter__()

        page = _mock_page(images=TracingImages())

        def tracing_sleep(duration):
            call_order.append('yielded')

        with patch.object(extract_module.gevent, 'sleep', side_effect=tracing_sleep):
            _find_template_image_sizes([page])

        assert call_order == ['yielded', 'images_accessed']

    def test_empty_pages_list_is_a_noop(self):
        with patch.object(extract_module, 'gevent') as mock_gevent:
            result = _find_template_image_sizes([])
        assert result == set()
        mock_gevent.sleep.assert_not_called()


class TestFindTemplateShapeSizesYields:
    def test_yields_once_per_page_with_positive_duration(self):
        pages = [_mock_page() for _ in range(3)]
        with patch.object(extract_module, 'gevent') as mock_gevent:
            _find_template_shape_sizes(pages)
        assert mock_gevent.sleep.call_count == 3
        for call in mock_gevent.sleep.call_args_list:
            (duration,), _ = call
            assert duration > 0


class TestVectorShapeScanGatedBehindVision:
    """_find_template_shape_sizes computes data only ever consumed by
    vision-gated code — it must not run (and pay the parsing cost) when
    vision isn't configured. Not itself the WORKER TIMEOUT fix (that's the
    yields above, which apply regardless of vision), but a real, necessary
    correctness/efficiency fix found during the same diagnosis."""

    # The minimal fixture PDF is a genuinely blank page (no content stream
    # at all) — extract_text() correctly reports "no extractable text" for
    # it even after the OCR fallback (also correctly finds nothing on a
    # blank page). That's expected, not a failure: these tests only care
    # whether _find_template_shape_sizes got called, not whether the PDF
    # "succeeds" — asserting the exact expected error message (rather than
    # ignoring it) still catches an unrelated crash turning into some other
    # error path.
    _EXPECTED_BLANK_PDF_ERROR = (
        'PDF contained no extractable text, even after OCR '
        '(image quality too low, or a blank/handwritten document?)'
    )

    def test_skipped_entirely_when_vision_unavailable(self, tmp_path):
        pdf_path = tmp_path / 'minimal.pdf'
        pdf_path.write_bytes(_MINIMAL_PDF)

        with patch('app.ai.langgraph_flow.vision_utils.has_vision', return_value=False), \
             patch.object(extract_module, '_find_template_shape_sizes') as mock_fn:
            result = extract_module.extract_text({'pdf_path': str(pdf_path)})

        mock_fn.assert_not_called()
        assert result.get('error') == self._EXPECTED_BLANK_PDF_ERROR

    def test_runs_when_vision_available(self, tmp_path):
        pdf_path = tmp_path / 'minimal.pdf'
        pdf_path.write_bytes(_MINIMAL_PDF)

        with patch('app.ai.langgraph_flow.vision_utils.has_vision', return_value=True), \
             patch.object(extract_module, '_find_template_shape_sizes',
                           wraps=extract_module._find_template_shape_sizes) as mock_fn:
            result = extract_module.extract_text({'pdf_path': str(pdf_path)})

        mock_fn.assert_called_once()
        assert result.get('error') == self._EXPECTED_BLANK_PDF_ERROR
