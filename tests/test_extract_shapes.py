"""Pure-geometry unit tests for the vector-drawn diagram detection added to
app/ai/langgraph_flow/nodes/extract.py. Deliberately does NOT cover
_vector_figure_annotations, _figure_annotations, _formula_annotations, or
anything touching describe_figure/has_vision — those stay verified
live/manually against a real PDF, consistent with this codebase's existing
live-calibration convention (see extract.py's MIN_FIGURE_AREA comment).

Run: pytest tests/test_extract_shapes.py -v
"""
import types

from app.ai.langgraph_flow.nodes.extract import (
    _cluster_by_position,
    _cluster_shapes,
    _diagram_candidate_bboxes,
    _stroked_rects,
)


def _shape(x0, top, x1, bottom, **extra):
    return {'x0': x0, 'top': top, 'x1': x1, 'bottom': bottom, **extra}


def _page(curves=(), lines=(), rects=(), width=600, height=800):
    return types.SimpleNamespace(
        curves=list(curves), lines=list(lines), rects=list(rects),
        width=width, height=height,
    )


class TestClusterByPosition:
    def test_empty_input(self):
        assert _cluster_by_position([], y_gap=20, x_gap=60) == []

    def test_items_within_gap_merge(self):
        a = _shape(0, 0, 10, 10)
        b = _shape(12, 2, 20, 12)  # x0=12 is within 60 of a's x1=10; top within 20
        clusters = _cluster_by_position([a, b], y_gap=20, x_gap=60)
        assert len(clusters) == 1
        assert len(clusters[0]) == 2

    def test_items_outside_gap_separate(self):
        a = _shape(0, 0, 10, 10)
        b = _shape(500, 700, 510, 710)
        clusters = _cluster_by_position([a, b], y_gap=20, x_gap=60)
        assert len(clusters) == 2

    def test_used_for_char_like_dicts(self):
        # char dicts carry extra keys (text, fontname) alongside x0/top/x1/bottom
        chars = [
            {'x0': 0, 'top': 0, 'x1': 5, 'bottom': 10, 'text': 'a', 'fontname': 'CMMI10'},
            {'x0': 6, 'top': 1, 'x1': 11, 'bottom': 11, 'text': 'b', 'fontname': 'CMMI10'},
        ]
        clusters = _cluster_by_position(chars, y_gap=20, x_gap=60)
        assert len(clusters) == 1
        assert len(clusters[0]) == 2


class TestClusterShapes:
    """_cluster_shapes uses symmetric bbox-gap proximity (not
    _cluster_by_position's directional x0-near-x1 heuristic), since
    diagram shapes point in arbitrary directions relative to each other
    unlike left-to-right reading-order text/aligned matrix columns."""

    def test_empty_input(self):
        assert _cluster_shapes([], y_gap=20, x_gap=60) == []

    def test_overlapping_shapes_merge(self):
        a = _shape(0, 0, 10, 10)
        b = _shape(5, 5, 15, 15)  # overlaps a
        clusters = _cluster_shapes([a, b], y_gap=20, x_gap=60)
        assert len(clusters) == 1

    def test_shapes_far_apart_separate(self):
        a = _shape(0, 0, 10, 10)
        b = _shape(500, 700, 510, 710)
        clusters = _cluster_shapes([a, b], y_gap=20, x_gap=60)
        assert len(clusters) == 2

    def test_box_outline_four_edges_cluster_together(self):
        # A box outline's edges point in perpendicular directions relative
        # to each other — this is exactly the case _cluster_by_position's
        # directional heuristic fails on (see TestDiagramCandidateBboxes'
        # regression comment) and _cluster_shapes must handle correctly.
        edges = [
            _shape(0, 0, 100, 5),      # top
            _shape(0, 5, 5, 100),      # left
            _shape(95, 5, 100, 100),   # right
            _shape(0, 95, 100, 100),   # bottom
        ]
        clusters = _cluster_shapes(edges, y_gap=20, x_gap=60)
        assert len(clusters) == 1
        assert len(clusters[0]) == 4


class TestStrokedRects:
    def test_keeps_stroked_bordered_rect(self):
        rects = [_shape(0, 0, 10, 10, stroke=True, linewidth=1)]
        assert _stroked_rects(rects) == rects

    def test_drops_unstroked_fill_only_rect(self):
        rects = [_shape(0, 0, 10, 10, stroke=False, linewidth=1)]
        assert _stroked_rects(rects) == []

    def test_drops_stroked_zero_linewidth_rect(self):
        rects = [_shape(0, 0, 10, 10, stroke=True, linewidth=0)]
        assert _stroked_rects(rects) == []


class TestDiagramCandidateBboxes:
    def _diagram_shapes(self, x0=0, top=0):
        """>= _MIN_DIAGRAM_SHAPES lines forming a bbox area >= _MIN_DIAGRAM_AREA."""
        return [
            _shape(x0, top, x0 + 100, top + 5),
            _shape(x0, top + 5, x0 + 5, top + 100),
            _shape(x0 + 95, top + 5, x0 + 100, top + 100),
            _shape(x0, top + 95, x0 + 100, top + 100),
        ]

    def test_success_case_returns_bbox(self):
        page = _page(lines=self._diagram_shapes())
        result = _diagram_candidate_bboxes(page, table_bboxes=[], template_shape_sizes=set())
        assert len(result) == 1
        x0, top, x1, bottom = result[0]
        assert x1 - x0 == 100 and bottom - top == 100

    def test_excludes_cluster_overlapping_table_bbox(self):
        shapes = self._diagram_shapes()
        page = _page(lines=shapes)
        # table bbox fully overlapping the diagram's own bbox (0,0)-(100,100)
        result = _diagram_candidate_bboxes(
            page, table_bboxes=[(0, 0, 100, 100)], template_shape_sizes=set())
        assert result == []

    def test_excludes_cluster_below_min_shape_count(self):
        # only 2 shapes, below _MIN_DIAGRAM_SHAPES (4)
        shapes = [_shape(0, 0, 100, 5), _shape(0, 95, 100, 100)]
        page = _page(lines=shapes)
        result = _diagram_candidate_bboxes(page, table_bboxes=[], template_shape_sizes=set())
        assert result == []

    def test_excludes_cluster_below_min_area(self):
        # 4 shapes but bbox area well under _MIN_DIAGRAM_AREA (8000 pts^2)
        shapes = [
            _shape(0, 0, 5, 1), _shape(0, 1, 1, 5),
            _shape(4, 1, 5, 5), _shape(0, 4, 5, 5),
        ]
        page = _page(lines=shapes)
        result = _diagram_candidate_bboxes(page, table_bboxes=[], template_shape_sizes=set())
        assert result == []

    def test_excludes_cluster_matching_template_size(self):
        shapes = self._diagram_shapes()
        page = _page(lines=shapes)
        result = _diagram_candidate_bboxes(
            page, table_bboxes=[], template_shape_sizes={(100, 100)})
        assert result == []

    def test_includes_stroked_rects_excludes_unstroked(self):
        rects = [
            _shape(0, 0, 100, 5, stroke=True, linewidth=1),
            _shape(0, 5, 5, 100, stroke=True, linewidth=1),
            _shape(95, 5, 100, 100, stroke=True, linewidth=1),
            _shape(0, 95, 100, 100, stroke=True, linewidth=1),
            _shape(20, 20, 80, 80, stroke=False, linewidth=0),  # fill-only highlight, excluded
        ]
        page = _page(rects=rects)
        result = _diagram_candidate_bboxes(page, table_bboxes=[], template_shape_sizes=set())
        assert len(result) == 1
