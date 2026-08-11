"""Unit tests for app/ai/langgraph_flow/nodes/chunk.py's atomic-block-aware
splitting. Deliberately does NOT cover chunk_by_topic end-to-end (would
require a full PipelineState/emit callback) — tested at the
_split_into_segments/_build_chunks unit level instead, which is where all
the new logic lives.

Run: pytest tests/test_chunk_segments.py -v
"""
from app.ai.langgraph_flow.config import CHUNK_WORDS
from app.ai.langgraph_flow.nodes.chunk import _build_chunks, _split_into_segments


def _words(n, prefix='word'):
    return ' '.join(f'{prefix}{i}' for i in range(n))


class TestSplitIntoSegments:
    def test_plain_text_only(self):
        text = 'just some ordinary prose with no annotation blocks at all.'
        segments = _split_into_segments(text)
        assert segments == [('text', text)]

    def test_multiline_table_block_isolated_with_roundtrip(self):
        text = (
            'Some prose before.\n\n'
            '[Table:\n'
            '| A | B |\n'
            '|---|---|\n'
            '| 1 | 2 |\n'
            ']\n\n'
            'Some prose after.'
        )
        segments = _split_into_segments(text)
        kinds = [k for k, _ in segments]
        assert 'block' in kinds
        block_text = next(t for k, t in segments if k == 'block')
        assert block_text == '[Table:\n| A | B |\n|---|---|\n| 1 | 2 |\n]'
        # round-trip invariant: concatenating every segment reconstructs raw_text exactly
        assert ''.join(t for _, t in segments) == text

    def test_formula_and_figure_blocks_isolated(self):
        text = (
            'Apply the rule:\n\n'
            '[Formula: F = m * a]\n\n'
            'See the diagram:\n\n'
            '[Figure: A free-body diagram showing forces on a block.]\n\n'
            'End of section.'
        )
        segments = _split_into_segments(text)
        blocks = [t for k, t in segments if k == 'block']
        assert blocks == [
            '[Formula: F = m * a]',
            '[Figure: A free-body diagram showing forces on a block.]',
        ]
        assert ''.join(t for _, t in segments) == text

    def test_two_adjacent_blocks_both_isolated(self):
        text = '[Formula: a + b = c]\n[Figure: A triangle labeled a, b, c.]'
        segments = _split_into_segments(text)
        blocks = [t for k, t in segments if k == 'block']
        assert blocks == [
            '[Formula: a + b = c]',
            '[Figure: A triangle labeled a, b, c.]',
        ]
        assert ''.join(t for _, t in segments) == text


class TestBuildChunksEquivalence:
    """Highest-priority regression guard: for text with no annotation
    blocks, output must be byte-identical to the original plain word-count
    slicing (' '.join(words[i:i+CHUNK_WORDS]) for i in range(0, len(words),
    CHUNK_WORDS))."""

    def test_no_annotation_input_matches_original_slicing(self):
        text = _words(CHUNK_WORDS * 3 + 137)
        words = text.split()
        expected = [
            ' '.join(words[i:i + CHUNK_WORDS])
            for i in range(0, len(words), CHUNK_WORDS)
        ]
        segments = _split_into_segments(text)
        built = _build_chunks(segments)
        actual = [c for c, _is_single_block in built]
        assert actual == expected

    def test_short_no_annotation_input_single_chunk(self):
        text = _words(50)
        segments = _split_into_segments(text)
        built = _build_chunks(segments)
        assert len(built) == 1
        assert built[0] == (text, False)


class TestBuildChunksAtomicBlocks:
    def test_block_straddling_boundary_stays_intact(self):
        # ~490 words of prose, then a 30-word block — under plain
        # tokenization this block would straddle the 500-word boundary.
        prose = _words(CHUNK_WORDS - 10)
        block_text = '[Formula: ' + ' '.join(f't{i}' for i in range(28)) + ']'
        text = prose + '\n\n' + block_text
        segments = _split_into_segments(text)
        built = _build_chunks(segments)
        # the block must appear whole inside exactly one chunk
        matches = [c for c, _ in built if block_text in c]
        assert len(matches) == 1

    def test_oversized_block_becomes_its_own_chunk(self):
        block_text = '[Table:\n' + '\n'.join(
            f'| {i} | {i * 2} |' for i in range(CHUNK_WORDS)
        ) + '\n]'
        segments = _split_into_segments(block_text)
        built = _build_chunks(segments)
        assert len(built) == 1
        chunk_text, is_single_block = built[0]
        assert chunk_text == block_text
        assert is_single_block is True

    def test_single_block_only_chunk_flagged(self):
        # a small block that lands alone at the very end, right after a flush
        prose = _words(CHUNK_WORDS)
        small_block = '[Formula: x = y]'
        text = prose + '\n\n' + small_block
        segments = _split_into_segments(text)
        built = _build_chunks(segments)
        assert len(built) == 2
        assert built[0][1] is False  # the full prose chunk
        assert built[1] == (small_block, True)


class TestChunkByTopicWordFloorExemption:
    def test_single_block_chunk_exempt_from_40_word_floor(self):
        # simulate the >40-word filter chunk_by_topic applies, confirming
        # a small (<40 word) single-block chunk survives it
        small_block = '[Formula: x = y]'
        segments = _split_into_segments(small_block)
        built = _build_chunks(segments)
        assert len(small_block.split()) <= 40
        survivors = [c for c, is_single_block in built if is_single_block or len(c.split()) > 40]
        assert survivors == [small_block]
