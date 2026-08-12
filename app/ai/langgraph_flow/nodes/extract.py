"""Stage 1 — pull raw text out of the uploaded PDF (with a local Tesseract
OCR fallback for scanned pages that have no text layer).

Beyond plain text extraction, this also covers three other gaps plain
`pdfplumber.extract_text()` has (see the plan's Part B):
  - glyph_resolver.resolve_page_glyphs(): fixes font-encoding-corrupted
    characters (Greek letters, bracket pieces) before text is extracted.
  - _formula_annotations(): for genuinely 2D math regions (matrices,
    multi-line derivations) where linearized text can still lose meaning
    even with every individual glyph correctly resolved, a narrowly-
    scoped vision transcription of that region is appended as a
    supplementary annotation.
  - _figure_annotations(): embedded diagrams/images are otherwise
    completely invisible to text extraction (not corrupted — just never
    looked at) — a short vision description is appended so labeled
    figures (e.g. a biology cell diagram) can still ground real questions.
  - _table_annotations(): pdfplumber's dedicated table-structure API
    (unused by plain extract_text) reformats detected tables as Markdown,
    excluded from the plain-text pass so the same content doesn't appear
    twice in two different (one scrambled) forms.
"""
from __future__ import annotations

import logging

import gevent
import pdfplumber
import pytesseract
from pdfplumber.utils import get_bbox_overlap

from app.ai.langgraph_flow.config import MAX_OCR_PAGES, OCR_LANGUAGES, OCR_RESOLUTION
from app.ai.langgraph_flow.glyph_resolver import resolve_page_glyphs
from app.ai.langgraph_flow.progress import emit
from app.ai.langgraph_flow.state import PipelineState

log = logging.getLogger('kahoot.ai')

# Minimum image area (points^2) to be considered a real figure rather than
# a decorative icon/bullet/rule line. Calibrated against real calibration
# PDFs this session: genuine figures ranged ~11,000-182,000; decorative
# elements (thin bars, tiny icons) topped out around 3,500. Set well
# between the two, not at either boundary.
MIN_FIGURE_AREA = 8000
# If the same (width, height) appears on at least this many pages of one
# document, treat it as a repeated template/background graphic (e.g. a
# slide deck's logo bar on every page) rather than unique content — found
# live this session: one math slide deck had an identical 363x272 image
# on all 10 pages.
TEMPLATE_REPEAT_THRESHOLD = 3

# Formula-region clustering: characters from math-symbol fonts within this
# vertical/horizontal gap (points) of each other are treated as one
# region. Not a rigorous 2D-layout algorithm — a pragmatic, data-driven
# proxy (built from where glyph_resolver already found corrupted/math
# characters) for "this looks like one formula", used only to scope a
# vision transcription request to a small region — never the whole page
# (MUST rule 2).
_CLUSTER_Y_GAP = 20
_CLUSTER_X_GAP = 60
# Safety cap: never send a region wider than this fraction of the page to
# vision transcription — if characters are scattered across nearly the
# whole page (common on slides with many small separate formulas), that
# is not "one formula", and cropping it would violate the same
# never-a-full-page rule glyph resolution's vision fallback follows.
_MAX_REGION_AREA_FRACTION = 0.5

# Vector-diagram clustering: same idea as _CLUSTER_Y_GAP/_CLUSTER_X_GAP
# above, but for page.curves/lines/rects instead of page.chars — finds
# diagrams (chemistry structures, circuit diagrams) drawn as PDF vector
# primitives rather than embedded raster images. Placeholder, uncalibrated
# against a real vector-diagram PDF — started equal to the char-clustering
# gaps as the simplest defensible default; diagram components (wires, bond
# lines) may legitimately sit farther apart than glyph characters do.
_SHAPE_CLUSTER_Y_GAP = 20
_SHAPE_CLUSTER_X_GAP = 60
# Minimum curve/line/stroked-rect primitives before a cluster counts as a
# diagram, not a stray underline/axis line. Placeholder, uncalibrated.
_MIN_DIAGRAM_SHAPES = 4
# points^2. Deliberately a separate constant from MIN_FIGURE_AREA even
# though it starts at the same value — this measures a stroke-cluster
# bbox, not a raster image's own dimensions, and should be tunable
# independently. Placeholder, uncalibrated (contrast with MIN_FIGURE_AREA
# above, which IS calibrated against real PDFs).
_MIN_DIAGRAM_AREA = 8000


def extract_text(state: PipelineState) -> dict:
    emit(state, 'Reading PDF…', 0.1)
    try:
        with pdfplumber.open(state['pdf_path']) as pdf:
            pages = []
            n_glyphs_resolved = 0
            n_formula_regions = 0
            n_figures = 0
            n_vector_figures = 0
            n_tables = 0
            template_sizes = _find_template_image_sizes(pdf.pages)
            # Gated behind has_vision(): the only consumer of this data is
            # _vector_figure_annotations, itself a no-op without vision, so
            # computing it otherwise would force pdfplumber to parse every
            # page's curves/lines/rects (real, measured CPU cost — see
            # _diagram_candidate_bboxes's docstring) for nothing. Unlike
            # _find_template_image_sizes above (page.images was already
            # accessed elsewhere pre-existing this feature; no new cost).
            from app.ai.langgraph_flow.vision_utils import has_vision
            template_shape_sizes = _find_template_shape_sizes(pdf.pages) if has_vision() else set()

            for page in pdf.pages:
                # Cooperative yield: extract_text() runs inside a single
                # gevent worker shared with all other traffic (live games,
                # WebSocket pings). Per-page work here is CPU-bound (PDF
                # parsing, image rendering, shape clustering) with no I/O of
                # its own to yield on, so without an explicit yield a
                # figure/formula/diagram-heavy document can starve the event
                # loop for the page loop's entire duration — see
                # Dockerfile's --timeout comment and this bug's diagnosis
                # (measured: one continuous 2.4s starvation window on a
                # 5-page synthetic test document, zero yields, before this
                # fix). MUST be a small positive duration, not
                # gevent.sleep(0) — verified live: sleep(0) only hands off to
                # greenlets already ready at that exact instant, so a
                # timer-scheduled greenlet (e.g. another request's periodic
                # wait) that isn't ready yet never actually gets to run; a
                # tiny positive sleep unconditionally cedes control to the
                # hub for that long, which is what actually lets other
                # greenlets progress.
                gevent.sleep(0.001)

                n_corrupted, resolved_chars = resolve_page_glyphs(page)
                n_glyphs_resolved += n_corrupted

                table_bboxes, table_annotations = _table_annotations(page)
                n_tables += len(table_annotations)

                if table_bboxes:
                    text_page = page
                    for bbox in table_bboxes:
                        text_page = text_page.outside_bbox(bbox)
                    page_text = text_page.extract_text() or ''
                else:
                    page_text = page.extract_text() or ''

                formula_annotations = _formula_annotations(page, resolved_chars)
                n_formula_regions += len(formula_annotations)

                figure_annotations = _figure_annotations(page, template_sizes)
                n_figures += len(figure_annotations)

                vector_figure_annotations = _vector_figure_annotations(
                    page, table_bboxes, template_shape_sizes)
                n_vector_figures += len(vector_figure_annotations)

                combined = '\n'.join([page_text, *table_annotations,
                                       *formula_annotations, *figure_annotations,
                                       *vector_figure_annotations])
                pages.append(combined)

        # warning, not info: this app's root logger defaults to WARNING (no
        # basicConfig sets INFO anywhere, confirmed live) — info-level here
        # would be silently swallowed, violating the "log distinctly, never
        # silently" convention json_utils.py's repair logging already follows.
        if n_glyphs_resolved:
            log.warning(f'extract_text: resolved {n_glyphs_resolved} corrupted '
                         f'glyph(s) across {len(pages)} pages (see glyph_resolver.py)')
        if n_formula_regions or n_figures or n_vector_figures or n_tables:
            log.warning(f'extract_text: {n_formula_regions} formula region(s), '
                         f'{n_figures} figure(s), {n_vector_figures} vector diagram(s), '
                         f'{n_tables} table(s) annotated')
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


def _find_template_image_sizes(pages) -> set[tuple[int, int]]:
    """Pre-scan: (width, height) sizes appearing on >= TEMPLATE_REPEAT_THRESHOLD
    pages, across the whole document — see TEMPLATE_REPEAT_THRESHOLD.

    The gevent.sleep(0.001) below is the real fix for this pipeline's
    WORKER TIMEOUT crashes (diagnosed 2026-08-12), not the yields added
    elsewhere in this file: pdfplumber/pdfminer lazily parses a page's
    full object model (images, chars, curves, lines, rects together) on
    FIRST access to ANY of them, and this loop's `page.images` access —
    called here, unconditionally, before anything else in extract_text()
    — is that first access for every page of every uploaded PDF. Measured
    live: 2.4s of continuous, unyielded parsing across just 5 pages of a
    synthetic chart-heavy test document, with ZERO gevent scheduling
    opportunities for any other traffic (other users' live games,
    WebSocket pings) for that entire span — confirmed by instrumenting
    yields elsewhere in this file (extract_text()'s main loop,
    _find_template_shape_sizes) and finding they fired too late: by the
    time those run, this function has already forced the parse for every
    page, so their own per-page cost measures as near-zero. Must be a
    small positive duration, not gevent.sleep(0) — see extract_text()'s
    per-page yield comment for why sleep(0) alone doesn't actually cede
    control to other greenlets."""
    from collections import Counter
    counts = Counter()
    for page in pages:
        gevent.sleep(0.001)
        seen_this_page = set()
        for im in page.images:
            size = (round(im['width']), round(im['height']))
            if size not in seen_this_page:
                counts[size] += 1
                seen_this_page.add(size)
    return {size for size, n in counts.items() if n >= TEMPLATE_REPEAT_THRESHOLD}


def _figure_annotations(page, template_sizes: set[tuple[int, int]]) -> list[str]:
    from app.ai.langgraph_flow.vision_utils import describe_figure, has_vision
    if not has_vision():
        return []
    annotations = []
    for im in page.images:
        w, h = im['width'], im['height']
        if w * h < MIN_FIGURE_AREA:
            continue
        if (round(w), round(h)) in template_sizes:
            continue
        try:
            bbox = (max(0, im['x0']), max(0, im['top']),
                    min(page.width, im['x1']), min(page.height, im['bottom']))
            crop = _crop_page_region(page, bbox)
            description = describe_figure(crop)
            annotations.append(f'[Figure: {description}]')
        except Exception as exc:
            log.warning(f'extract_text: figure description failed: {exc!r}')
    return annotations


def _stroked_rects(rects: list[dict]) -> list[dict]:
    """Filter page.rects to bordered/stroked rects only. A rectangular
    diagram border (e.g. a boxed circuit or reaction-scheme diagram) is
    real content; a filled highlight/background box (stroke=False, pure
    fill color) is not — excluding those here prevents every colored
    callout box on a slide from being misdetected as a diagram. Both
    'stroke' (bool) and 'linewidth' (number) are confirmed-present keys
    on pdfplumber rect dicts (verified live against the vendored
    pdfplumber source: rects go through the same LTCurve attribute path
    as lines/curves, which carries these fields)."""
    return [r for r in rects if r.get('stroke') and (r.get('linewidth') or 0) > 0]


def _cluster_shapes(shapes: list[dict], y_gap: float, x_gap: float) -> list[list[dict]]:
    """Group vector shapes (page.curves/lines/stroked-rects) into spatial
    clusters via symmetric bounding-box proximity — how close two shapes'
    boxes are in each axis, regardless of relative direction. Deliberately
    NOT _cluster_by_position/_cluster_chars's directional heuristic (a new
    item's x0 near a previous item's x1, same top): that's built for
    left-to-right reading-order text, where it also happens to chain
    matrix/formula rows together because aligned columns repeat similar
    x-positions across rows. A diagram's shapes (a box outline's four
    edges, a molecule's bonds) point in arbitrary directions relative to
    each other with no such repeating alignment — verified live: the
    directional heuristic failed to cluster a synthetic 4-edge box outline
    into one group at all (each edge came out as its own singleton
    cluster), while this symmetric check clusters it correctly."""
    if not shapes:
        return []

    def _near(a: dict, b: dict) -> bool:
        x_gap_between = max(a['x0'], b['x0']) - min(a['x1'], b['x1'])
        y_gap_between = max(a['top'], b['top']) - min(a['bottom'], b['bottom'])
        return x_gap_between <= x_gap and y_gap_between <= y_gap

    ordered = sorted(shapes, key=lambda s: (s['top'], s['x0']))
    clusters = [[ordered[0]]]
    for s in ordered[1:]:
        cluster = clusters[-1]
        if any(_near(s, o) for o in cluster):
            cluster.append(s)
        else:
            clusters.append([s])
    return clusters


def _find_template_shape_sizes(pages) -> set[tuple[int, int]]:
    """Pre-scan analog of _find_template_image_sizes, for vector-drawn
    shape clusters instead of raster images — see TEMPLATE_REPEAT_THRESHOLD.
    Deliberately does NOT exclude table regions here (table_bboxes aren't
    known this early — find_tables() hasn't run yet per-page); a repeated
    table-grid cluster being counted toward this template set is harmless
    either way, since it would already be excluded from the final
    per-page result by _diagram_candidate_bboxes's table-bbox check
    regardless."""
    from collections import Counter
    counts = Counter()
    for page in pages:
        # Cheap by the time this runs — _find_template_image_sizes above
        # already forced the full per-page parse (see its docstring for
        # why); kept for defense in depth on a document shape where that
        # ordering assumption ever stops holding.
        gevent.sleep(0.001)
        shapes = page.curves + page.lines + _stroked_rects(page.rects)
        seen_this_page = set()
        for cluster in _cluster_shapes(shapes, _SHAPE_CLUSTER_Y_GAP, _SHAPE_CLUSTER_X_GAP):
            if len(cluster) < _MIN_DIAGRAM_SHAPES:
                continue
            x0 = min(s['x0'] for s in cluster)
            x1 = max(s['x1'] for s in cluster)
            top = min(s['top'] for s in cluster)
            bottom = max(s['bottom'] for s in cluster)
            size = (round(x1 - x0), round(bottom - top))
            if size not in seen_this_page:
                counts[size] += 1
                seen_this_page.add(size)
    return {size for size, n in counts.items() if n >= TEMPLATE_REPEAT_THRESHOLD}


def _diagram_candidate_bboxes(page, table_bboxes: list[tuple],
                               template_shape_sizes: set[tuple[int, int]]) -> list[tuple]:
    """Pure geometry: which curve/line/stroked-rect clusters on this page
    qualify as standalone vector-drawn diagrams? Excludes clusters that
    overlap a detected table's own bbox — find_tables()'s "lines"
    strategy builds table grids from these same curves/lines/rects
    (confirmed by reading pdfplumber's table.py:TableFinder.get_edges()),
    so without this exclusion a table's grid gets double-detected as a
    "diagram" — as well as clusters below the min-shape-count/min-area
    thresholds, and clusters matching a repeated-template size. Returns
    each surviving cluster's bbox. Kept separate from
    _vector_figure_annotations so this logic is testable without
    has_vision()/a real page render — see tests/test_extract_shapes.py."""
    shapes = page.curves + page.lines + _stroked_rects(page.rects)
    candidates = []
    for cluster in _cluster_shapes(shapes, _SHAPE_CLUSTER_Y_GAP, _SHAPE_CLUSTER_X_GAP):
        if len(cluster) < _MIN_DIAGRAM_SHAPES:
            continue
        x0 = min(s['x0'] for s in cluster)
        x1 = max(s['x1'] for s in cluster)
        top = min(s['top'] for s in cluster)
        bottom = max(s['bottom'] for s in cluster)
        bbox = (max(0, x0), max(0, top), min(page.width, x1), min(page.height, bottom))
        area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        if area < _MIN_DIAGRAM_AREA:
            continue
        if any(get_bbox_overlap(bbox, t) is not None for t in table_bboxes):
            continue  # this cluster is a table's own grid, not a standalone diagram
        size = (round(bbox[2] - bbox[0]), round(bbox[3] - bbox[1]))
        if size in template_shape_sizes:
            continue
        candidates.append(bbox)
    return candidates


def _vector_figure_annotations(page, table_bboxes: list[tuple],
                                template_shape_sizes: set[tuple[int, int]]) -> list[str]:
    from app.ai.langgraph_flow.vision_utils import describe_figure, has_vision
    if not has_vision():
        return []
    annotations = []
    for bbox in _diagram_candidate_bboxes(page, table_bboxes, template_shape_sizes):
        try:
            crop = _crop_page_region(page, bbox)
            description = describe_figure(crop)
            annotations.append(f'[Figure: {description}]')
        except Exception as exc:
            log.warning(f'extract_text: vector diagram description failed: {exc!r}')
    return annotations


def _formula_annotations(page, resolved_chars: list[dict]) -> list[str]:
    from app.ai.langgraph_flow.vision_utils import has_vision, transcribe_formula
    if not has_vision() or not resolved_chars:
        return []
    annotations = []
    page_area = page.width * page.height
    for cluster in _cluster_by_position(resolved_chars, _CLUSTER_Y_GAP, _CLUSTER_X_GAP):
        if len(cluster) < 3:
            continue  # a single stray symbol isn't "a formula region"
        x0 = min(c['x0'] for c in cluster)
        x1 = max(c['x1'] for c in cluster)
        top = min(c['top'] for c in cluster)
        bottom = max(c['bottom'] for c in cluster)
        pad = 15
        bbox = (max(0, x0 - pad), max(0, top - pad),
                min(page.width, x1 + pad), min(page.height, bottom + pad))
        region_area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
        if region_area > page_area * _MAX_REGION_AREA_FRACTION:
            continue  # too big to be "one formula" — see _MAX_REGION_AREA_FRACTION
        try:
            crop = _crop_page_region(page, bbox)
            transcription = transcribe_formula(crop)
            annotations.append(f'[Formula: {transcription}]')
        except Exception as exc:
            log.warning(f'extract_text: formula transcription failed: {exc!r}')
    return annotations


def _cluster_by_position(items: list[dict], y_gap: float, x_gap: float) -> list[list[dict]]:
    """Group items into spatial clusters by simple proximity, using each
    item's x0/top/x1/bottom — see _CLUSTER_Y_GAP/_CLUSTER_X_GAP and the
    module docstring for why this is a pragmatic proxy, not a rigorous
    2D-layout algorithm. Shared by formula-region character clustering
    (page.chars dicts) and vector-diagram shape clustering (page.curves/
    lines/rects dicts) — both operate on structurally identical
    x0/top/x1/bottom dicts, so one parameterized algorithm covers both."""
    if not items:
        return []
    ordered = sorted(items, key=lambda i: (i['top'], i['x0']))
    clusters = [[ordered[0]]]
    for it in ordered[1:]:
        cluster = clusters[-1]
        near_any = any(
            abs(it['top'] - o['top']) <= y_gap
            and abs(it['x0'] - o['x1']) <= x_gap
            for o in cluster
        )
        if near_any:
            cluster.append(it)
        else:
            clusters.append([it])
    return clusters


def _table_annotations(page) -> tuple[list[tuple], list[str]]:
    try:
        tables = page.find_tables()
    except Exception as exc:
        log.warning(f'extract_text: table detection failed: {exc!r}')
        return [], []
    bboxes, annotations = [], []
    for table in tables:
        rows = table.extract()
        if not rows or len(rows) < 2:
            continue  # a 1-row "table" is almost always a false-positive detection
        x0, top, x1, bottom = table.bbox
        # find_tables() can return a bbox that slightly overhangs the page
        # (detected border geometry extending past the edge) — clamp like
        # _figure_annotations/_formula_annotations already do, since
        # outside_bbox() below rejects anything not fully within the page.
        bbox = (max(0, x0), max(0, top), min(page.width, x1), min(page.height, bottom))
        bboxes.append(bbox)
        annotations.append(_table_to_markdown(rows))
    return bboxes, annotations


def _table_to_markdown(rows: list[list]) -> str:
    def _cell(v) -> str:
        return (v or '').replace('\n', ' ').replace('|', '/').strip()

    lines = ['[Table:']
    header = [_cell(v) for v in rows[0]]
    lines.append('| ' + ' | '.join(header) + ' |')
    lines.append('|' + '|'.join(['---'] * len(header)) + '|')
    for row in rows[1:]:
        cells = [_cell(v) for v in row]
        cells += [''] * (len(header) - len(cells))
        lines.append('| ' + ' | '.join(cells[:len(header)]) + ' |')
    lines.append(']')
    return '\n'.join(lines)


def _crop_page_region(page, bbox: tuple, resolution: int = 300) -> bytes:
    import io
    scale = resolution / 72
    im = page.to_image(resolution=resolution)
    crop = im.original.crop(tuple(c * scale for c in bbox))
    buf = io.BytesIO()
    crop.save(buf, format='PNG')
    return buf.getvalue()


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
