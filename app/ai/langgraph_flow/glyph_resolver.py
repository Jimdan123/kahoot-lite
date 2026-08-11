"""Resolves PDF glyphs that extract as Unicode Private-Use-Area codepoints
or raw ASCII control characters — a font-encoding artifact common in
LaTeX/Beamer-generated PDFs whose embedded math fonts (Computer Modern:
CMR10, CMBX10, CMMI10, CMMIB10, CMSY10, CMEX10, ...) lack a ToUnicode
CMap, so pdfplumber extracts the font's raw internal glyph-slot code
instead of a real character (verified live 2026-08 against a real Strang
linear-algebra slide deck: e.g. CMR10's codepoint 3 renders as Λ, but
CMEX10's codepoint 20 — same kind of raw code, different font — renders
as a meaningless bracket-fragment piece; the two must never be resolved
by codepoint alone).

Three layers, in order:
  1. glyph_cache.json — a static, per-"font:codepoint" lookup, seeded only
     with entries actually verified by cropping and visually inspecting
     the glyph (never guessed from "standard encoding" memory alone — see
     this plan's MUST rules).
  2. Vision fallback (vision_utils.describe_glyph) — only for cache
     misses; crops just that one character's bounding box, asks a
     narrowly-scoped question, and appends the answer back into the cache
     so the system self-improves: the same font/glyph combo showing up in
     a completely different document (Computer Modern fonts are used
     across huge numbers of real LaTeX-generated PDFs) is already cached
     next time, no repeat vision call.
  3. Placeholder — if vision is unavailable (OPENROUTER_API_KEY unset) or
     the vision call itself fails, an honest "[unrecognized symbol]"
     marker rather than silent deletion (corrupts meaning) or leaving the
     raw control character (breaks JSON downstream).

Callers must NOT hand-roll their own text reassembly after resolution —
mutate the character dicts in `page.chars` in place (they're the same
objects pdfplumber's own page.objects returns, not copies — verified
live) and call the page's own `extract_text()`, which reuses pdfplumber's
real line/word reconstruction. See resolve_page_glyphs below.

BOUNDARY THIS MODULE CANNOT CROSS (found live 2026-08, same slide deck):
a `sqrt()` radical visible in the rendered page turned out to have NO
corresponding object anywhere in `page.chars`, `page.curves`, `page.lines`,
or `page.rects` — confirmed by checking all four in the exact region it
renders. Some PDF generators draw certain symbols in a way pdfplumber's
object model doesn't capture at all (as opposed to capturing it but
mis-encoding it, which is what this module fixes). No amount of
character-level resolution can recover content that was never
represented as a character or vector shape in the first place — that
gap needs pixel-level image analysis of the formula's region (see this
plan's Part B2, formula-region vision transcription), not this module.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger('kahoot.ai')

CACHE_PATH = Path(__file__).parent / 'glyph_cache.json'
PLACEHOLDER = '[unrecognized symbol]'

# Render resolution (DPI) used when a crop is needed for a vision lookup —
# independent of OCR_RESOLUTION (config/generation.py), which is for
# whole-page OCR of scanned documents, a different code path.
CROP_RESOLUTION = 300
CROP_PADDING_POINTS = 3  # a little context around the glyph's own bbox

# Unicode ranges that indicate a font-encoding artifact rather than a real
# character: the Private Use Area (fonts without a ToUnicode CMap often
# get their raw glyph index reported here) and C0 control characters
# (excluding real whitespace, which IS meaningful).
_PUA_MIN, _PUA_MAX = 0xE000, 0xF8FF
_CONTROL_MIN, _CONTROL_MAX = 0x00, 0x1F
_ALLOWED_CONTROL = {'\n', '\t', '\r'}

# Fonts confirmed (by actually cropping and looking, not by assuming a
# whole font family behaves alike) to alias at least one glyph onto an
# ordinary printable-ASCII codepoint, invisible to the PUA/control-char
# check above. So far only CMEX10 — its codepoint 0x70 ('p') turned out
# to be an invisible spacing/sizing artifact, not the letter p (verified
# live 2026-08: the line it sits in reads correctly with no visible gap
# either way). Do NOT add a font here speculatively — CMSY8/CMBSY8 were
# tried and reverted after cropping their codepoint 0x2212 and finding it
# was already a correctly-encoded, real minus sign ("Q^-1") — treating a
# whole font family as suspect without per-glyph verification breaks
# already-correct text. CMMI/CMMIB (Math Italic) are excluded for the
# same reason: they mostly encode real Latin/Greek letters as themselves
# (confirmed live — "X", "A", "q" extracted correctly from CMMIB10 on the
# same page), with only isolated special-purpose codepoints repurposed
# (e.g. CMMIB10:21 -> λ) — the PUA/control-char check alone is the right
# level of suspicion for them.
_SYMBOL_ONLY_FONT_PREFIXES = ('CMEX',)


def is_corrupted_char(ch: str, base_font: str = '') -> bool:
    if len(ch) != 1:
        return False
    if base_font.startswith(_SYMBOL_ONLY_FONT_PREFIXES):
        return True
    cp = ord(ch)
    if _PUA_MIN <= cp <= _PUA_MAX:
        return True
    if _CONTROL_MIN <= cp <= _CONTROL_MAX and ch not in _ALLOWED_CONTROL:
        return True
    return False


def base_font_name(fontname: str) -> str:
    """Strip the random subset-tag PDF generators prepend when embedding
    only the glyphs a document actually uses, e.g. 'CZLODL+CMEX10' ->
    'CMEX10'. Font-blind resolution is exactly the mistake this module
    exists to avoid, so this is always applied before any cache lookup."""
    return fontname.split('+', 1)[-1] if '+' in fontname else fontname


def _load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    with CACHE_PATH.open() as f:
        return json.load(f)


def _append_cache(key: str, resolved: str) -> None:
    cache = _load_cache()
    cache[key] = resolved
    with CACHE_PATH.open('w') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')


def _crop_bytes(page, char: dict) -> bytes:
    import io
    scale = CROP_RESOLUTION / 72
    pad = CROP_PADDING_POINTS
    bbox = (
        max(0, char['x0'] - pad), max(0, char['top'] - pad),
        min(page.width, char['x1'] + pad), min(page.height, char['bottom'] + pad),
    )
    im = page.to_image(resolution=CROP_RESOLUTION)
    crop = im.original.crop(tuple(c * scale for c in bbox))
    buf = io.BytesIO()
    crop.save(buf, format='PNG')
    return buf.getvalue()


def resolve_page_glyphs(page) -> tuple[int, list]:
    """Mutates page.chars in place, resolving every corrupted character
    via the three-layer resolver above. Returns (n_corrupted,
    resolved_chars) — the count, and the actual character dicts that were
    corrupted (their positions are also what Part B2's formula-region
    detection uses, since math-symbol-font characters cluster spatially
    around actual formulas). (0, []) means the page had none and needed
    no work.

    Deliberately does NOT reassemble the page's text itself — call
    page.extract_text() afterward for that, so pdfplumber's own
    reconstruction logic is what runs, not a hand-rolled one (see the
    module docstring and this plan's MUST rules)."""
    cache = _load_cache()
    resolved_chars = []
    n_vision_calls = 0

    for char in page.chars:
        ch = char.get('text', '')
        font = base_font_name(char.get('fontname', ''))
        if not is_corrupted_char(ch, font):
            continue
        resolved_chars.append(char)
        key = f'{font}:{ord(ch)}'

        if key in cache:
            char['text'] = cache[key]
            continue

        resolved = None
        from app.ai.langgraph_flow.vision_utils import describe_glyph, has_vision
        if has_vision():
            try:
                image_bytes = _crop_bytes(page, char)
                resolved = describe_glyph(image_bytes)
                n_vision_calls += 1
                _append_cache(key, resolved)
                cache[key] = resolved
                log.warning(f'glyph_resolver: vision-resolved new glyph {key!r} -> {resolved!r}')
            except Exception as exc:
                log.warning(f'glyph_resolver: vision lookup failed for {key!r}: {exc!r}')

        char['text'] = resolved if resolved is not None else PLACEHOLDER
        if resolved is None:
            log.warning(f'glyph_resolver: no resolution for {key!r} — using placeholder')

    if resolved_chars:
        # warning, not info: this app's root logger defaults to WARNING (no
        # basicConfig sets INFO anywhere) — info-level would be silently
        # swallowed, violating the "log distinctly, never silently"
        # convention json_utils.py's repair logging already follows.
        log.warning(f'glyph_resolver: {len(resolved_chars)} corrupted char(s) on page, '
                     f'{n_vision_calls} new vision lookup(s)')
    return len(resolved_chars), resolved_chars
