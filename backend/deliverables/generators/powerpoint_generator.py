"""
PowerPoint Generator for Client Materials — IPC Template edition.

Uses IPC_PPT_Template_2026.pptx as the primary generation source.
The original file is NEVER modified; generation always works on an
in-memory copy.

Architecture (template-first):
  1. Load the IPC template into memory and snapshot every slide's XML element.
  2. Clear all slides from the in-memory copy.
  3. For each slide in the deck spec: find the matching IPC slide index from
     SLIDE_REGISTRY, inject the snapshot XML into a newly-added slide, then
     replace only the text placeholders with Claude-generated content.
  4. The design (layout, positioning, typography, spacing, colours) comes
     entirely from the template.  Claude supplies content only.
  5. Append a sources slide (copy of S15 two-col-small) and the end slide (S29).

IPC Template slide registry (49 slides, 26.67" × 15.00"):
  ── Text layouts ──────────────────────────────────────────────────────────
  Slide 01  Cover, cyan            TEXT_BOX 'Title 2' for deck title,
                                   idx=12 contact, idx=13 subtitle
  Slide 03  Contents (Agenda)      left col idx=10, right col idx=11,
                                   title idx=4294967295
  Slide 04  Section divider        title idx=4294967295
  Slide 05  Large text             statement idx=0
  Slide 06  Data, 2 callouts (V)   stat-A body idx=4294967295, metric idx=13,
                                   stat-B body idx=11, metric idx=14
  Slide 07  Data, 3 callouts (V)   headline idx=0, stat bodies idx=12/13/14,
                                   stat labels idx=15/16/17
  Slide 10  Text, 4 col headlines  bodies idx=12/13/14, heads idx=15/16/17
  Slide 13  Text, 2 col large title  title idx=0, col heads idx=16/17,
                                     bodies idx=18/19
  Slide 15  Text, 2 col small title  title idx=0, col heads idx=16/17,
                                     bodies idx=18/19
  Slide 18  Boxes, 4 stacked LT    title idx=0, boxes idx=16/17/18/19
  Slide 20  Boxes, 4 horizontal LT title idx=0, boxes idx=11/12/13/14
  Slide 22  Boxes, 6 stacked       title idx=0, boxes idx=20/16/17/12/13/14
  Slide 29  End slide              (no text placeholders)
  ── Diagram / consulting layouts ──────────────────────────────────────────
  Slide 34  Process diagram        idx=0 title; 7 named TEXT_BOX shapes
                                   (3 top row + 4 bottom row); each box has
                                   Para 0 = step heading, Para 1 = description
  Slide 35  Technical architecture idx=0 title; visual = embedded GROUP shape;
                                   idx=13 source line
  Slide 37  Timeline               idx=0 title; visual = embedded GROUP shape;
                                   idx=13 source line
  Slide 38  Hierarchy              idx=0 title; idx=14 left body panel (5.4×10in);
                                   visual = embedded GROUP on right; idx=13 source
  Slide 41  Value tree             idx=0 title; idx=14 left body panel;
                                   visual = embedded diagram on right; idx=13 source
  Slide 42  RACI                   idx=0 title; idx=14 left body panel;
                                   visual = embedded table + GROUP; idx=13 source

Quality principles enforced here:
  - _clean_bullet() only strips [Source:] tags — never truncates or appends "…".
    Complete bullets are enforced upstream at the LLM prompt level.
  - Each layout has a hard capacity limit; bullets over the limit go to speaker notes.
  - Auto-split: if the LLM sends too many bullets for a layout, the generator
    creates a continuation slide rather than silently discarding content.
  - Sources consolidated onto one final slide — never on individual bullets.
  - validate_bullets() logs a warning for any bullet ending with … so regressions
    are immediately visible in server logs.
"""
from __future__ import annotations

import copy
import io
import logging
import re
import warnings
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.util import Pt

logger = logging.getLogger(__name__)

IPC_TEMPLATE_PATH = (
    Path(__file__).parent.parent
    / "templates"
    / "IPC_PPT_Template_2026.pptx"
)

# Fallback to IBM Asset Kit if IPC template not found
FALLBACK_TEMPLATE_PATH = (
    Path(__file__).parent.parent
    / "templates"
    / "IBM Asset Kit and Assistant Pack Kit Guidance_2025.pptx"
)

# ── Placeholder index used by the section-divider slide title ────────────────
# python-pptx reports this as 4294967295 (== uint32 max == -1 signed)
_IDX_TITLE_UNSIGNED = 4294967295

# ── IPC slide registry: JSON layout key → 0-based slide index in IPC template ─
# These indices correspond to slide positions in IPC_PPT_Template_2026.pptx
SLIDE_REGISTRY: dict[str, int] = {
    # ── Text layouts ─────────────────────────────────────────────────────────
    "cover":                 0,   # S01 — Cover, cyan
    "agenda":                2,   # S03 — Contents
    "section_divider":       3,   # S04 — Section divider
    "large_text":            4,   # S05 — Large text
    "data_2_callouts":       5,   # S06 — Data, 2 callouts, vertical
    "callout_stat":          6,   # S07 — Data, 3 callouts, vertical (repurposed)
    "four_column_headlines": 9,   # S10 — Text, 4 columns, dividers, headlines
    "two_col_dividers":      12,  # S13 — Text, 2 columns, dividers, large title
    "two_column":            14,  # S15 — Text, 2 columns, dividers, small title
    "title_content":         14,  # S15 — same slide; left col = body, right col unused
    "four_boxes_stacked":    17,  # S18 — Boxes, 4 stacked, large title
    "four_boxes_wide":       19,  # S20 — Boxes, 4 horizontal, large title
    "six_boxes":             21,  # S22 — Boxes, 6 stacked
    "end_slide":             28,  # S29 — End slide (no text)
    # ── Diagram / consulting layouts ─────────────────────────────────────────
    "process_diagram":       33,  # S34 — Process diagram (7 TextBox steps)
    "technical_architecture": 34, # S35 — Technical diagram (GROUP visual)
    "timeline":              36,  # S37 — Timeline (GROUP visual)
    "hierarchy":             37,  # S38 — Hierarchy (GROUP + left body)
    "value_tree":            40,  # S41 — Value tree (diagram + left body)
    "raci":                  41,  # S42 — RACI (table + left body)
}

# ── Ordered text-box names for S34 Process diagram ──────────────────────────
# Sorted by visual position: top row (y≈1.7in) left→right, bottom row (y≈8.3in) left→right
# Top row = 3 boxes at x≈5.2, 11.9, 18.6; Bottom row = 4 boxes at x≈1.9, 8.6, 15.2, 21.9
_PROCESS_TEXTBOX_NAMES: list[str] = [
    "TextBox 39",  # top-left
    "TextBox 40",  # top-centre
    "TextBox 41",  # top-right
    "TextBox 33",  # bottom-left
    "TextBox 35",  # bottom-centre-left
    "TextBox 36",  # bottom-centre-right
    "TextBox 38",  # bottom-right
]

# Per-layout bullet/item capacity (overflow → speaker notes or auto-split)
CAPACITY: dict[str, int] = {
    "title_content":         7,
    "callout_stat":          5,
    "two_column":            5,   # per column (total 10)
    "two_col_dividers":      5,   # per column body
    "four_column_headlines": 4,   # per column body
    "four_boxes_wide":       3,
    "four_boxes_stacked":    3,
    "six_boxes":             2,
    "data_2_callouts":       4,
    "large_text":            1,
    "agenda":                8,
    "section_divider":       0,
    "end_slide":             0,
    # Diagram slides: step/item capacity
    "process_diagram":       7,   # 7 text boxes in total (3 top + 4 bottom)
    "technical_architecture": 0,  # visual-only; title + source only
    "timeline":              0,   # visual-only; title + source only
    "hierarchy":             5,   # left body panel bullet list
    "value_tree":            5,   # left body panel bullet list
    "raci":                  5,   # left body panel bullet list
}

# Font sizes
FONT_TITLE_COVER   = 32
FONT_TITLE_SECTION = 28
FONT_TITLE_CONTENT = 20
FONT_BODY          = 14
FONT_SMALL         = 13
FONT_BOX           = 12
FONT_SOURCES       = 11

# Strip [Source: ...] inline citations
_SOURCE_TAG_RE = re.compile(r"\s*\[Source:[^\]]*\]", re.IGNORECASE)


# ── Quality validators ────────────────────────────────────────────────────────

def validate_bullets(slides: list[dict]) -> None:
    """Log a warning for every bullet that ends with '...' or '…'."""
    for i, slide in enumerate(slides, 1):
        for j, bullet in enumerate(slide.get("bullets", []), 1):
            if isinstance(bullet, str) and (
                bullet.rstrip().endswith("...") or bullet.rstrip().endswith("…")
            ):
                logger.warning(
                    "Slide %d bullet %d ends with ellipsis (truncated sentence): %r",
                    i, j, bullet[:80],
                )


def _clean_bullet(text: str) -> str:
    """Strip [Source: ...] tags. Never truncate or append ellipsis."""
    return _SOURCE_TAG_RE.sub("", text).strip()


# ── Template placeholder-leak elimination ──────────────────────────────────────
#
# Every content slide is a clone of an IPC template slide.  The builders only
# overwrite the shapes they know about; any shape left untouched keeps its
# design-time demo text ("Lorem ipsum…", "68/84pt title", "Firstname Lastname",
# "Source: 1. Lorem source name", …).  That demo text is what leaks into the
# final deck and screams "AI-generated / unfinished".
#
# _TEMPLATE_LEAK_PATTERNS matches ONLY template-authored demo signatures — never
# anything a real consulting sentence would contain — so scrubbing a matching
# shape is always safe.  _scrub_template_text() blanks matching shapes (recursing
# into groups and tables); _scan_leaks() is the post-build safety net that hard-
# fails generation if any signature survives.
_TEMPLATE_LEAK_PATTERNS: list[re.Pattern] = [
    re.compile(r"lorem\s+ipsum", re.IGNORECASE),
    re.compile(r"\blorem\b", re.IGNORECASE),
    re.compile(r"click to edit", re.IGNORECASE),
    re.compile(r"master text styles", re.IGNORECASE),
    re.compile(r"\d{2,3}/\d{2,3}(?:/\d{2,3})?\s*pt", re.IGNORECASE),  # 68/84pt, 28/36/42pt
    re.compile(r"\b\d{2,3}pt\b", re.IGNORECASE),                      # 168pt
    re.compile(r"firstname lastname", re.IGNORECASE),
    re.compile(r"email\.address@ibm\.com", re.IGNORECASE),
    re.compile(r"source:\s*if applicable", re.IGNORECASE),
    re.compile(r"lorem source name", re.IGNORECASE),
    re.compile(r"numbers are optional", re.IGNORECASE),
    re.compile(r"go\s+simple and big", re.IGNORECASE),
    re.compile(r"add superscript number", re.IGNORECASE),
    re.compile(r"\bsection (?:one|two|three|four|five|six)\b", re.IGNORECASE),
    re.compile(r"describe source origin", re.IGNORECASE),
    re.compile(r"sentence case", re.IGNORECASE),
    re.compile(r"lines maximum", re.IGNORECASE),
    re.compile(r"click to add", re.IGNORECASE),      # empty-placeholder prompt text
    re.compile(r"place imagery", re.IGNORECASE),
]


def _looks_like_leak(text: str) -> bool:
    """True if `text` contains any template demo signature."""
    if not text or not text.strip():
        return False
    return any(p.search(text) for p in _TEMPLATE_LEAK_PATTERNS)


def _iter_all_shapes(shapes):
    """Yield every shape recursively, descending into GROUP shapes."""
    for shape in shapes:
        yield shape
        if shape.shape_type == 6:  # MSO_SHAPE_TYPE.GROUP
            try:
                yield from _iter_all_shapes(shape.shapes)
            except Exception:
                pass


def _blank_text_frame(shape) -> None:
    """Clear all text from a shape's text frame, leaving the frame intact."""
    try:
        tf = shape.text_frame
        tf.clear()
        tf.paragraphs[0].text = ""
    except Exception:
        pass


def _scrub_template_text(slide) -> int:
    """
    Blank every shape (incl. group children and table cells) whose text is a
    leftover template demo signature.  Returns the number of shapes scrubbed.
    """
    scrubbed = 0
    for shape in _iter_all_shapes(slide.shapes):
        # Table cells
        try:
            if shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        if _looks_like_leak(cell.text):
                            cell.text = ""
                            scrubbed += 1
                continue
        except Exception:
            pass
        # Text frames
        try:
            if shape.has_text_frame and _looks_like_leak(shape.text_frame.text):
                _blank_text_frame(shape)
                scrubbed += 1
        except Exception:
            pass
    return scrubbed


def _scan_leaks(prs) -> list[tuple[int, str, str]]:
    """
    Walk the finished presentation and return (slide_index, shape_name, text)
    for every surviving template demo signature.  Used as a hard-fail gate.
    """
    leaks: list[tuple[int, str, str]] = []
    for s_idx, slide in enumerate(prs.slides, 1):
        for shape in _iter_all_shapes(slide.shapes):
            try:
                if shape.has_table:
                    for row in shape.table.rows:
                        for cell in row.cells:
                            if _looks_like_leak(cell.text):
                                leaks.append((s_idx, f"{shape.name}[cell]", cell.text[:80]))
                    continue
            except Exception:
                pass
            try:
                if shape.has_text_frame and _looks_like_leak(shape.text_frame.text):
                    leaks.append((s_idx, shape.name, shape.text_frame.text[:80]))
            except Exception:
                pass
    return leaks


_SOURCES_TITLE_RE = re.compile(r"^\s*sources?\b", re.IGNORECASE)


def _is_sources_title(title: str) -> bool:
    """True for LLM-generated slides titled 'Sources' / 'Sources & Evidence'."""
    return bool(title) and bool(_SOURCES_TITLE_RE.match(title.strip()))


def _remove_empty_placeholders(slide) -> int:
    """
    Delete unused PLACEHOLDER shapes whose text frame is empty.

    Setting a placeholder's text to "" is NOT enough — PowerPoint renders the
    layout's prompt ("Click to add text") and a dashed border for any empty
    placeholder.  The only way to make it disappear is to remove the shape.

    Only PLACEHOLDER shapes with an empty text frame are removed.  Lines,
    pictures, groups, tables, free text boxes, and any populated shape are left
    untouched, so this never deletes real content or template decoration.
    Returns the number of placeholders removed.
    """
    removed = 0
    for shape in list(slide.shapes):
        try:
            if not shape.is_placeholder:
                continue
            if shape.has_text_frame and shape.text_frame.text.strip() == "":
                sp = shape._element
                sp.getparent().remove(sp)
                removed += 1
        except Exception:
            pass
    return removed


def _scan_empty_placeholders(prs) -> list[tuple[int, str]]:
    """Post-QA: return (slide_index, shape_name) for any empty placeholder that
    survived removal (would render a 'Click to add text' prompt)."""
    out: list[tuple[int, str]] = []
    for s_idx, slide in enumerate(prs.slides, 1):
        for shape in slide.shapes:
            try:
                if shape.is_placeholder and shape.has_text_frame \
                        and shape.text_frame.text.strip() == "":
                    out.append((s_idx, shape.name))
            except Exception:
                pass
    return out


# ── Template snapshot manager ─────────────────────────────────────────────────

class _TemplateSnapshots:
    """
    Holds deep-copy snapshots of every slide element in the IPC template.
    Built once per process (lazy init) so we don't re-read from disk on every
    generate_pptx() call.

    The template file is read once; each subsequent call to get_slide_element()
    returns a fresh deep copy of the pre-captured XML element.
    """

    def __init__(self, template_path: Path):
        self._path = template_path
        self._elements: dict[int, Any] = {}
        self._layouts: dict[int, Any] = {}
        self._prs: Presentation | None = None

    def _ensure_loaded(self) -> None:
        if self._prs is not None:
            return
        logger.info("[powerpoint] Loading IPC template from: %s", self._path)
        self._prs = Presentation(io.BytesIO(self._path.read_bytes()))
        for i, slide in enumerate(self._prs.slides):
            self._elements[i] = copy.deepcopy(slide._element)
            self._layouts[i] = slide.slide_layout
        logger.info("[powerpoint] Template loaded: %d slides snapshotted", len(self._elements))

    def get_element(self, slide_idx: int) -> Any:
        """Return a fresh deep copy of slide N's XML element."""
        self._ensure_loaded()
        if slide_idx not in self._elements:
            raise ValueError(f"Slide index {slide_idx} not in IPC template (0-based, max {max(self._elements)})")
        return copy.deepcopy(self._elements[slide_idx])

    def get_layout(self, slide_idx: int) -> Any:
        """Return the slide layout for slide N (shared ref — do not mutate)."""
        self._ensure_loaded()
        return self._layouts.get(slide_idx, list(self._layouts.values())[0])

    def get_blank_presentation(self) -> Presentation:
        """
        Return an in-memory Presentation loaded from the IPC template with all
        slides cleared.  The master, layouts, and theme are preserved.
        """
        self._ensure_loaded()
        prs = Presentation(io.BytesIO(self._path.read_bytes()))
        _clear_slides(prs)
        return prs


# Process-level singleton — loaded once on first generate_pptx() call.
_template_snapshots: _TemplateSnapshots | None = None


def _get_snapshots() -> _TemplateSnapshots:
    global _template_snapshots
    if _template_snapshots is None:
        path = IPC_TEMPLATE_PATH if IPC_TEMPLATE_PATH.exists() else FALLBACK_TEMPLATE_PATH
        if not path.exists():
            raise FileNotFoundError(
                f"IPC template not found at:\n  {IPC_TEMPLATE_PATH}\n"
                "Place IPC_PPT_Template_2026.pptx in backend/deliverables/templates/"
            )
        _template_snapshots = _TemplateSnapshots(path)
    return _template_snapshots


# ── Slide management ──────────────────────────────────────────────────────────

def _clear_slides(prs: Presentation) -> None:
    """
    Remove all existing slides, preserving master, layouts, and theme.

    Clears <p:sldIdLst> XML first, then pops relationship entries so
    iter_parts() does not visit old slide objects during serialisation.
    """
    prs_elem = prs.part._element
    sld_id_lst = prs_elem.find(
        ".//{http://schemas.openxmlformats.org/presentationml/2006/main}sldIdLst"
    )
    if sld_id_lst is None:
        return

    NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    rids: list[str] = []
    for sld_id in list(sld_id_lst):
        rid = sld_id.get(f"{{{NS_R}}}id")
        if rid:
            rids.append(rid)
        sld_id_lst.remove(sld_id)

    for rid in rids:
        if rid in prs.part._rels:
            prs.part._rels.pop(rid)


def _add_slide_from_template(prs: Presentation, slide_idx: int) -> Any:
    """
    Duplicate an IPC template slide into `prs` and return the new Slide object.

    Approach:
      1. add_slide(layout) — creates a new blank slide wired to the correct
         layout and relationship entry.
      2. Replace the new slide's XML element (and its Part's element) with a
         deep copy of the pre-captured template slide element.  This preserves
         all shapes, colours, fonts, and decorative elements from the original
         slide while leaving the new relationship entry intact.
    """
    snaps = _get_snapshots()
    layout = snaps.get_layout(slide_idx)
    new_slide = prs.slides.add_slide(layout)

    # Replace the element — must update both the Part and the Slide proxy
    cloned_element = snaps.get_element(slide_idx)
    new_slide.part._element = cloned_element
    new_slide._element = new_slide.part._element

    # CRITICAL: add_slide() accesses slide.shapes internally (to clone the
    # layout's placeholders), which caches the `shapes` lazyproperty against the
    # pre-swap (blank) shape tree.  Without invalidating that cache, every
    # subsequent slide.shapes access — _write_shape_by_name(), the scrub, the
    # leak scan — would operate on the stale empty tree and silently no-op.
    # Drop the cached lazyproperties so they recompute from the swapped element.
    for _cached in ("shapes", "placeholders"):
        new_slide.__dict__.pop(_cached, None)

    return new_slide


# ── Placeholder helpers ───────────────────────────────────────────────────────

def _ph_by_idx(slide, idx: int):
    """Return a placeholder by placeholder_format.idx, or None."""
    for ph in slide.placeholders:
        try:
            if ph.placeholder_format.idx == idx:
                return ph
        except Exception:
            pass
    return None


def _write_ph(slide, idx: int, text: str, font_size: int | None = None) -> bool:
    """Write plain text to a placeholder.  Returns True on success."""
    ph = _ph_by_idx(slide, idx)
    if ph is None:
        return False
    try:
        tf = ph.text_frame
        tf.clear()
        tf.word_wrap = True
        p = tf.paragraphs[0]
        p.text = text
        if font_size:
            p.font.size = Pt(font_size)
        return True
    except Exception as exc:
        logger.warning("Could not write to placeholder idx=%d: %s", idx, exc)
        return False


def _write_bullets(
    slide,
    idx: int,
    bullets: list[str],
    max_bullets: int,
    font_size: int | None = None,
) -> list[str]:
    """
    Write a cleaned bullet list into a placeholder.
    Returns any overflow bullets (caller handles them).

    When font_size is None (the default) the run inherits the template
    placeholder's designed size and typeface (IBM Plex Sans) — this is what
    keeps generated text at the large, on-brand sizes the IPC layout intends.
    Pass an explicit size only for free text boxes whose run-level sizing is
    destroyed by text_frame.clear().
    """
    ph = _ph_by_idx(slide, idx)
    if ph is None:
        return bullets  # nothing written — return all as overflow

    cleaned  = [_clean_bullet(b) for b in bullets if b.strip()]
    display  = cleaned[:max_bullets]
    overflow = cleaned[max_bullets:]

    try:
        tf = ph.text_frame
        tf.clear()
        tf.word_wrap = True
        for i, bullet in enumerate(display):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.text = bullet
            p.level = 0
            if font_size:
                p.font.size = Pt(font_size)
    except Exception as exc:
        logger.warning("Could not write bullets to placeholder idx=%d: %s", idx, exc)

    return overflow


def _append_notes(slide, text: str) -> None:
    """Append text to slide speaker notes."""
    try:
        nf = slide.notes_slide.notes_text_frame
        current = nf.text.strip()
        nf.text = (current + "\n" + text).strip()
    except Exception:
        pass


def _park_overflow(slide, overflow: list[str], context: str = "") -> None:
    """Write overflow bullets to speaker notes."""
    if overflow:
        label = f"[{context} overflow]" if context else "[Overflow]"
        _append_notes(slide, f"\n{label}\n" + "\n".join(f"• {b}" for b in overflow))


# ── Auto-split helper ─────────────────────────────────────────────────────────

def _chunk(lst: list, size: int) -> list[list]:
    """Split a list into chunks of at most `size`."""
    return [lst[i:i + size] for i in range(0, max(len(lst), 1), size)]


# ── Slide builders ─────────────────────────────────────────────────────────────
#
# Each builder duplicates the correct IPC template slide and writes content
# into its placeholders.  The visual design (layout, positioning, colours,
# typography) is preserved unchanged from the template.

def _write_shape_by_name(slide, shape_name: str, text: str, font_size: int | None = None) -> bool:
    """
    Write plain text to a named shape's text frame (non-placeholder shapes).
    Returns True on success, False if the shape is not found.

    Used for the S01 Cover 'Title 2' text box and the S34 process diagram
    step boxes, which are TEXT_BOX shapes rather than placeholders.
    """
    for shape in slide.shapes:
        if shape.name == shape_name and shape.has_text_frame:
            try:
                tf = shape.text_frame
                tf.clear()
                tf.word_wrap = True
                p = tf.paragraphs[0]
                p.text = text
                if font_size:
                    p.font.size = Pt(font_size)
                return True
            except Exception as exc:
                logger.warning("Could not write to shape %r: %s", shape_name, exc)
                return False
    return False


def _append_shape_paragraph(slide, shape_name: str, text: str, font_size: int) -> bool:
    """Append a paragraph to a named shape's existing text frame (e.g. a subtitle
    line beneath the cover title).  Returns True on success."""
    if not text:
        return False
    for shape in slide.shapes:
        if shape.name == shape_name and shape.has_text_frame:
            try:
                p = shape.text_frame.add_paragraph()
                p.text = text
                p.font.size = Pt(font_size)
                return True
            except Exception as exc:
                logger.warning("Could not append paragraph to shape %r: %s", shape_name, exc)
                return False
    return False


def _write_shape_two_part(
    slide,
    shape_name: str,
    heading: str,
    body: str,
    heading_size: int = FONT_BOX,
    body_size: int = FONT_BOX,
) -> bool:
    """
    Write a two-paragraph (heading + body) text to a named TEXT_BOX shape.

    The S34 process diagram boxes each hold a short step heading on the first
    paragraph and a description on the second.  This helper writes both
    paragraphs and applies font sizes independently.
    Returns True on success.
    """
    for shape in slide.shapes:
        if shape.name == shape_name and shape.has_text_frame:
            try:
                tf = shape.text_frame
                tf.clear()
                tf.word_wrap = True
                p0 = tf.paragraphs[0]
                p0.text = heading
                p0.font.size = Pt(heading_size)
                p0.font.bold = True
                if body:
                    p1 = tf.add_paragraph()
                    p1.text = body
                    p1.font.size = Pt(body_size)
                return True
            except Exception as exc:
                logger.warning("Could not write two-part to shape %r: %s", shape_name, exc)
                return False
    return False


def _add_cover(prs: Presentation, title: str, subtitle: str) -> None:
    """
    IPC S01 — Cover, cyan

    The deck title lives in TEXT_BOX shape 'Title 2' (pos 0.63, 2.28in,
    size 11.05×10.00in) — a free-form text box, NOT a placeholder.
    Writing to it places the title in the large IBM-blue title area.

    The contact/subtitle line uses the standard placeholder:
      idx=12: primary line (workspace name / subtitle)
      idx=13: secondary line (cleared — no email content)
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["cover"])
    # Title fills the large IBM title box; inherit its designed size (~68-132pt)
    # for short titles, but shrink long ones so they don't overflow the box.
    n = len(title)
    cover_size = None if n <= 70 else (40 if n <= 120 else 30)
    _write_shape_by_name(slide, "Title 2", title, font_size=cover_size)
    # Subtitle rides as a smaller second line inside the SAME title box so it
    # reads as a proper cover — never in the contact placeholders.
    if subtitle:
        _append_shape_paragraph(slide, "Title 2", subtitle, font_size=FONT_TITLE_COVER)
    # Blank the contact placeholders; the empty-placeholder removal pass then
    # deletes them so no 'Firstname Lastname' / 'Click to add text' ever shows.
    _write_ph(slide, 12, "")
    _write_ph(slide, 13, "")


def _add_section(prs: Presentation, title: str) -> None:
    """
    IPC S04 — Section divider
      idx=4294967295: section title
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["section_divider"])
    _write_ph(slide, _IDX_TITLE_UNSIGNED, title)  # inherit the large divider size


def _add_large_text_slide(prs: Presentation, title: str, notes: str = "") -> None:
    """
    IPC S05 — Large text
      idx=0: full-slide bold statement

    Use for: single governing insight, bottom-line finding.
    The 'title' field IS the statement — it fills the entire slide.
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["large_text"])
    # Big, room-readable statement. Explicit (not the template's 168pt) so a
    # full sentence fits without overflow, but far larger than body text.
    _write_ph(slide, 0, title, font_size=48)
    if notes:
        _append_notes(slide, notes)


def _add_callout_stat_slide(
    prs: Presentation,
    title: str,
    bullets: list[str],
    notes: str = "",
) -> None:
    """
    IPC S07 — Data, 3 callouts, vertical (repurposed as callout_stat)
      idx=0:  headline (5 lines max)
      idx=15/16/17: stat labels (large numbers / metrics)
      idx=12/13/14: stat body text
      idx=11: sources line

    For callout_stat usage, we put the slide title at idx=0 and distribute
    up to 3 bullets across the stat body placeholders (idx=12/13/14).
    Metric-style items (short labels) go to idx=15/16/17.
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["callout_stat"])
    _write_ph(slide, 0, title)  # inherit the large headline size

    # Distribute bullets across the 3 stat body placeholders
    body_idxs = [12, 13, 14]
    cap = CAPACITY["callout_stat"]
    clean = [_clean_bullet(b) for b in bullets if b.strip()]
    display = clean[:cap]
    overflow = clean[cap:]

    for i, b in enumerate(display):
        if i < len(body_idxs):
            _write_ph(slide, body_idxs[i], b)

    # Clear unused stat label placeholders (they contain template placeholder text)
    for idx in [15, 16, 17]:
        _write_ph(slide, idx, "")
    # Clear the template 'Source: 1. Lorem source name' line (idx=11)
    _write_ph(slide, 11, "")

    _park_overflow(slide, overflow, "callout_stat")
    if notes:
        _append_notes(slide, notes)


def _add_title_content_slide(
    prs: Presentation,
    title: str,
    bullets: list[str],
    notes: str = "",
) -> None:
    """
    IPC S15 — Text, 2 columns, dividers, small title (used as single-column bullets)
      idx=0:  slide title
      idx=18: left body column (primary content)
      idx=19: right body column (empty for title_content)

    For title_content, all bullets go into the left body column (idx=18).
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["title_content"])
    _write_ph(slide, 0, title)  # inherit the designed headline size

    cap = CAPACITY["title_content"]
    overflow = _write_bullets(slide, 18, bullets, max_bullets=cap)
    # Clear the right column (no content)
    _write_ph(slide, 19, "")
    # Clear col heads
    _write_ph(slide, 16, "")
    _write_ph(slide, 17, "")

    _park_overflow(slide, overflow, "title_content")
    if notes:
        _append_notes(slide, notes)


def _add_two_column_slide(
    prs: Presentation,
    title: str,
    bullets: list[str],
    notes: str = "",
) -> None:
    """
    IPC S15 — Text, 2 columns, dividers, small title
      idx=0:  title
      idx=16: col-A heading (cleared)
      idx=17: col-B heading (cleared)
      idx=18: col-A body bullets
      idx=19: col-B body bullets
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["two_column"])
    _write_ph(slide, 0, title)  # inherit the designed headline size
    # Clear column headings
    _write_ph(slide, 16, "")
    _write_ph(slide, 17, "")

    cap = CAPACITY["two_column"]
    mid = max(1, (len(bullets) + 1) // 2)
    ov1 = _write_bullets(slide, 18, bullets[:mid], max_bullets=cap)
    ov2 = _write_bullets(slide, 19, bullets[mid:], max_bullets=cap)
    _park_overflow(slide, ov1 + ov2, "two_column")
    if notes:
        _append_notes(slide, notes)


def _add_two_col_dividers_slide(
    prs: Presentation,
    title: str,
    col_a_head: str,
    col_b_head: str,
    col_a_bullets: list[str],
    col_b_bullets: list[str],
    notes: str = "",
) -> None:
    """
    IPC S13 — Text, 2 columns, dividers, large title
      idx=0:  large title
      idx=16: col-A heading
      idx=17: col-B heading
      idx=18: col-A body bullets
      idx=19: col-B body bullets
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["two_col_dividers"])
    _write_ph(slide, 0, title)              # inherit the large title size
    _write_ph(slide, 16, col_a_head)        # inherit the column-head size
    _write_ph(slide, 17, col_b_head)

    cap = CAPACITY["two_col_dividers"]
    ov_a = _write_bullets(slide, 18, col_a_bullets, max_bullets=cap)
    ov_b = _write_bullets(slide, 19, col_b_bullets, max_bullets=cap)
    _park_overflow(slide, ov_a + ov_b, "two_col_dividers")
    if notes:
        _append_notes(slide, notes)


def _add_four_column_headlines_slide(
    prs: Presentation,
    title: str,
    col_heads: list[str],
    columns: list[list[str]],
    notes: str = "",
) -> None:
    """
    IPC S10 — Text, 4 columns, dividers, headlines
      'Title 1' (TEXT_BOX): slide title  — carries run-level sizing that
                            text_frame.clear() destroys, so set it explicitly.
      idx=15/16/17: column headings (3 columns — S10 has 3 body cols)
      idx=12/13/14: column bodies
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["four_column_headlines"])

    # The title lives in the free-form 'Title 1' text box (was previously never
    # written, leaking '28/36/42pt headline').  All three heads are real heads.
    # Its run size is inherited (None at run level), so omit size to keep the
    # designed headline size rather than shrinking it.
    _write_shape_by_name(slide, "Title 1", title, font_size=None)

    head_idxs = [15, 16, 17]
    body_idxs = [12, 13, 14]

    # Pad/trim to 3
    heads3  = (list(col_heads) + ["", "", ""])[:3]
    cols3   = (list(columns) + [[], [], []])[:3]

    for i, hidx in enumerate(head_idxs):
        _write_ph(slide, hidx, heads3[i])

    cap = CAPACITY["four_column_headlines"]
    for i, bidx in enumerate(body_idxs):
        overflow = _write_bullets(slide, bidx, cols3[i], max_bullets=cap)
        _park_overflow(slide, overflow, f"four_column_headlines col{i+1}")

    if notes:
        _append_notes(slide, notes)


def _add_four_boxes_stacked_slide(
    prs: Presentation,
    title: str,
    boxes: list[str],
    notes: str = "",
) -> None:
    """
    IPC S18 — Boxes, 4 stacked, large title
      idx=0:  large title
      idx=16/17/18/19: 4 box bodies (stacked 2×2)
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["four_boxes_stacked"])
    _write_ph(slide, 0, title)  # inherit the large title size

    box_idxs = [16, 17, 18, 19]
    boxes4 = (list(boxes) + ["", "", "", ""])[:4]
    for i, bidx in enumerate(box_idxs):
        _write_ph(slide, bidx, boxes4[i])

    if notes:
        _append_notes(slide, notes)


def _add_four_boxes_wide_slide(
    prs: Presentation,
    title: str,
    boxes: list[str],
    notes: str = "",
) -> None:
    """
    IPC S20 — Boxes, 4 horizontal, large title
      idx=0:  large title
      idx=11/12/13/14: 4 box bodies (horizontal row)
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["four_boxes_wide"])
    _write_ph(slide, 0, title)  # inherit the large title size

    box_idxs = [11, 12, 13, 14]
    boxes4 = (list(boxes) + ["", "", "", ""])[:4]
    for i, bidx in enumerate(box_idxs):
        _write_ph(slide, bidx, boxes4[i])

    if notes:
        _append_notes(slide, notes)


def _add_six_boxes_slide(
    prs: Presentation,
    title: str,
    boxes: list[str],
    notes: str = "",
) -> None:
    """
    IPC S22 — Boxes, 6 stacked
      idx=0:  title
      idx=20: box 1 (top-left)
      idx=16: box 2 (top-middle)
      idx=17: box 3 (top-right)
      idx=12: box 4 (bottom-left)
      idx=13: box 5 (bottom-middle)
      idx=14: box 6 (bottom-right)
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["six_boxes"])
    _write_ph(slide, 0, title)  # inherit the designed title size

    # Visual order: 20, 16, 17, 12, 13, 14
    box_idxs = [20, 16, 17, 12, 13, 14]
    boxes6 = (list(boxes) + [""] * 6)[:6]
    for i, bidx in enumerate(box_idxs):
        _write_ph(slide, bidx, boxes6[i])

    if notes:
        _append_notes(slide, notes)


def _add_data_2_callouts_slide(
    prs: Presentation,
    title: str,
    stat_a_label: str,
    stat_a_body: str,
    stat_b_label: str,
    stat_b_body: str,
    notes: str = "",
) -> None:
    """
    IPC S06 — Data, 2 callouts, vertical
      idx=4294967295: stat-A large body text (top-left, prominent area)
      idx=11:         stat-B large body text (top-right)
      idx=13:         stat-A metric (bottom-left — the big number)
      idx=14:         stat-B metric (bottom-right — the big number)

    The layout places large metric numbers at the bottom (idx 13/14) and
    supporting body text at the top (idx 4294967295 / 11).
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["data_2_callouts"])
    # Stat-A: body text top, metric bottom — inherit the designed sizes
    # (the metric placeholders are the large numbers in this layout).
    _write_ph(slide, _IDX_TITLE_UNSIGNED, stat_a_body)
    _write_ph(slide, 13, stat_a_label)
    # Stat-B: body text right, metric bottom-right
    _write_ph(slide, 11, stat_b_body)
    _write_ph(slide, 14, stat_b_label)
    if notes:
        _append_notes(slide, notes)


def _add_agenda_slide(
    prs: Presentation,
    title: str,
    items_left: list[str],
    items_right: list[str],
    notes: str = "",
) -> None:
    """
    IPC S03 — Contents (Agenda)
      idx=4294967295: agenda title
      idx=10:         left column items
      idx=11:         right column items
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["agenda"])
    _write_ph(slide, _IDX_TITLE_UNSIGNED, title)  # inherit the designed title size

    cap = CAPACITY["agenda"]
    ov_l = _write_bullets(slide, 10, items_left,  max_bullets=cap)
    ov_r = _write_bullets(slide, 11, items_right, max_bullets=cap)
    _park_overflow(slide, ov_l + ov_r, "agenda")
    if notes:
        _append_notes(slide, notes)


def _add_process_diagram_slide(
    prs: Presentation,
    title: str,
    steps: list[dict],
    notes: str = "",
) -> None:
    """
    IPC S34 — Process diagram

    The visual shows 7 step boxes connected by a horizontal flow line with
    numbered circle connectors.  The top row holds 3 boxes (steps 1–3) and
    the bottom row holds 4 boxes (steps 4–7).

    Content is written to named TEXT_BOX shapes (not placeholders):
      TextBox 39  top-left   (step 1)
      TextBox 40  top-centre (step 2)
      TextBox 41  top-right  (step 3)
      TextBox 33  bottom-left         (step 4)
      TextBox 35  bottom-centre-left  (step 5)
      TextBox 36  bottom-centre-right (step 6)
      TextBox 38  bottom-right        (step 7)

    Each step is a dict:  {"heading": "Step name", "body": "Description."}
    OR a plain string (used as heading only, body empty).

    steps: up to 7 items.  Unused boxes retain their placeholder text.
    The title is written to idx=0 (TITLE placeholder).
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["process_diagram"])
    _write_ph(slide, 0, title)  # inherit the designed title size

    cap = min(len(steps), len(_PROCESS_TEXTBOX_NAMES))
    for i in range(cap):
        step = steps[i]
        box_name = _PROCESS_TEXTBOX_NAMES[i]
        if isinstance(step, dict):
            heading = (step.get("heading") or step.get("title") or "").strip()
            body    = (step.get("body")    or step.get("description") or "").strip()
        else:
            heading = str(step).strip()
            body    = ""
        if heading:
            # Free text boxes lose their run-level size on clear(); set explicitly.
            _write_shape_two_part(slide, box_name, heading, body,
                                  heading_size=16, body_size=13)

    # Blank any step boxes we didn't fill — they otherwise keep 'Lorem ipsum…'.
    for i in range(cap, len(_PROCESS_TEXTBOX_NAMES)):
        _write_shape_by_name(slide, _PROCESS_TEXTBOX_NAMES[i], "")
    # Clear the 'Source: If applicable…' line.
    _write_ph(slide, 13, "")

    if notes:
        _append_notes(slide, notes)


def _add_technical_architecture_slide(
    prs: Presentation,
    title: str,
    context: str = "",
    notes: str = "",
) -> None:
    """
    IPC S35 — Technical diagram, light

    The visual is an embedded GROUP shape containing the IBM architecture
    diagram structure — preserved exactly from the template.

    Only the title placeholder is replaced.  The diagram group is untouched.

    title:   The consulting headline for this architecture view.
    context: Optional short annotation written to the source placeholder
             (idx=13) — e.g. "Based on IBM Reference Architecture".
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["technical_architecture"])
    _write_ph(slide, 0, title)  # inherit the designed title size
    _write_ph(slide, 13, context if context else "")  # else clear the demo source line
    if notes:
        _append_notes(slide, notes)


def _add_timeline_slide(
    prs: Presentation,
    title: str,
    context: str = "",
    notes: str = "",
) -> None:
    """
    IPC S37 — Timeline

    The visual is an embedded GROUP shape containing the IBM timeline
    structure — preserved exactly from the template.

    Only the title placeholder is replaced.

    title:   The consulting headline for this timeline view.
    context: Optional annotation written to source placeholder (idx=13).
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["timeline"])
    _write_ph(slide, 0, title)  # inherit the designed title size
    _write_ph(slide, 13, context if context else "")  # else clear the demo source line
    if notes:
        _append_notes(slide, notes)


def _add_hierarchy_slide(
    prs: Presentation,
    title: str,
    bullets: list[str],
    notes: str = "",
) -> None:
    """
    IPC S38 — Hierarchy

    Layout:
      idx=0:  slide title (top-left, 5.4×1.7in)
      idx=14: left body panel (5.4×10.0in) — narrative / key points
      idx=13: source line (bottom-right, cleared)
      GROUP:  IBM hierarchy diagram on right — preserved from template

    The left panel carries consulting narrative (bullet list).
    The hierarchy diagram on the right stays intact from the template.
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["hierarchy"])
    _write_ph(slide, 0, title)  # inherit the designed title size
    cap = CAPACITY["hierarchy"]
    overflow = _write_bullets(slide, 14, bullets, max_bullets=cap)
    _write_ph(slide, 13, "")  # clear source placeholder
    _park_overflow(slide, overflow, "hierarchy")
    if notes:
        _append_notes(slide, notes)


def _add_value_tree_slide(
    prs: Presentation,
    title: str,
    bullets: list[str],
    notes: str = "",
) -> None:
    """
    IPC S41 — Value tree

    Layout:
      idx=0:  slide title (top-left, 5.4×1.7in)
      idx=14: left body panel (5.4×10.0in) — value decomposition narrative
      idx=13: source line (bottom-right, cleared)
      DIAGRAM: IBM value tree diagram on right — preserved from template

    The left panel carries the value decomposition narrative.
    The value tree diagram on the right stays intact.
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["value_tree"])
    _write_ph(slide, 0, title)  # inherit the designed title size
    cap = CAPACITY["value_tree"]
    overflow = _write_bullets(slide, 14, bullets, max_bullets=cap)
    _write_ph(slide, 13, "")
    _park_overflow(slide, overflow, "value_tree")
    if notes:
        _append_notes(slide, notes)


def _add_raci_slide(
    prs: Presentation,
    title: str,
    bullets: list[str],
    notes: str = "",
) -> None:
    """
    IPC S42 — RACI table

    Layout:
      idx=0:  slide title (top-left, 5.4×1.7in)
      idx=14: left body panel (5.4×10.0in) — governance / role narrative
      idx=13: source line (cleared)
      TABLE:  IBM RACI table on right — preserved from template
      GROUP:  Role legend group on right — preserved from template

    The left panel carries the governance context narrative.
    The RACI table and role legend stay intact from the template.
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["raci"])
    _write_ph(slide, 0, title)  # inherit the designed title size
    cap = CAPACITY["raci"]
    overflow = _write_bullets(slide, 14, bullets, max_bullets=cap)
    _write_ph(slide, 13, "")
    _park_overflow(slide, overflow, "raci")
    if notes:
        _append_notes(slide, notes)


def _add_end_slide(prs: Presentation) -> None:
    """IPC S29 — End slide (IBM-branded closer, no text placeholders)."""
    _add_slide_from_template(prs, SLIDE_REGISTRY["end_slide"])


def _add_sources_slide(prs: Presentation, sources: list[str]) -> None:
    """
    Sources & Evidence slide — uses IPC S15 (two-column small title).
    All unique sources are listed in the left body column.
    """
    slide = _add_slide_from_template(prs, SLIDE_REGISTRY["title_content"])
    _write_ph(slide, 0, "Sources & Evidence")  # inherit the designed title size
    _write_ph(slide, 16, "")
    _write_ph(slide, 17, "")
    _write_ph(slide, 19, "")

    seen: set[str] = set()
    unique: list[str] = []
    for s in sources:
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            unique.append(s)

    bullets = unique if unique else ["No source documents recorded."]
    # Sources can be numerous — keep an explicit, compact-but-legible size.
    overflow = _write_bullets(slide, 18, bullets, max_bullets=14, font_size=16)
    if overflow:
        _append_notes(slide, "\nAdditional sources:\n" + "\n".join(f"• {b}" for b in overflow))


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_pptx(deck_spec: dict[str, Any], workspace_name: str) -> bytes:
    """
    Build a .pptx from a structured LLM deck spec and return file bytes.

    This is a template-first renderer: every slide is a copy of the corresponding
    IPC template slide.  Only text placeholder content is replaced.  All visual
    design comes from the IPC template.

    deck_spec schema:
    {
      "deliverable_type": "client_101" | "client_201" | "executive_summary",
      "title": "...",
      "focus_area": "...",          # executive_summary only
      "slides": [
        {
          "slide_number": 1,
          "title": "takeaway title",
          "layout": "title_content" | "two_column" | "two_col_dividers"
                   | "four_column_headlines"
                   | "four_boxes_wide" | "four_boxes_stacked" | "six_boxes"
                   | "large_text" | "callout_stat" | "data_2_callouts"
                   | "section_divider" | "agenda",
          "bullets": [...],
          "columns": [[...],[...]],
          "col_heads": ["A","B"],
          "boxes": [...],
          "stats": [{"label":"METRIC","body":"context"}]
        }
      ],
      "metadata": {
        "source_documents": [...],
        "generation_notes": "..."
      }
    }
    """
    template_path = IPC_TEMPLATE_PATH if IPC_TEMPLATE_PATH.exists() else FALLBACK_TEMPLATE_PATH
    if not template_path.exists():
        raise FileNotFoundError(
            f"IPC template not found at:\n  {IPC_TEMPLATE_PATH}\n"
            "Place IPC_PPT_Template_2026.pptx in backend/deliverables/templates/"
        )

    # Get a blank IPC-template presentation (all slides cleared, master preserved)
    snaps = _get_snapshots()
    prs = snaps.get_blank_presentation()

    title            = deck_spec.get("title", "Client Material")
    deliverable_type = deck_spec.get("deliverable_type", "")
    slides           = deck_spec.get("slides", [])
    metadata         = deck_spec.get("metadata", {})
    all_sources      = metadata.get("source_documents", [])
    gen_notes        = metadata.get("generation_notes", "")

    # Pre-flight: validate bullets for trailing ellipsis
    validate_bullets(slides)

    # ── Cover slide ───────────────────────────────────────────────────────────
    subtitle_parts = [workspace_name]
    if deliverable_type == "executive_summary":
        focus = deck_spec.get("focus_area") or metadata.get("focus_area", "")
        if focus:
            subtitle_parts.append(f"Focus: {focus}")
    if gen_notes:
        short = gen_notes.split(".")[0].strip()
        if short and short not in subtitle_parts:
            subtitle_parts.append(short)

    _add_cover(prs, title, " · ".join(subtitle_parts))

    # ── Content slides ────────────────────────────────────────────────────────
    for slide_spec in slides:
        layout_key  = (slide_spec.get("layout") or "title_content").strip()
        slide_title = (slide_spec.get("title") or "").strip()
        notes       = slide_spec.get("notes") or ""

        raw_bullets: list[str] = [
            b for b in (slide_spec.get("bullets") or [])
            if isinstance(b, str) and b.strip()
        ]
        raw_columns: list[list[str]] = slide_spec.get("columns") or []
        col_heads: list[str]         = slide_spec.get("col_heads") or []
        raw_boxes: list[str]         = slide_spec.get("boxes") or []

        if not slide_title:
            continue  # skip blank slides

        # Skip any LLM-generated 'Sources'/'Sources & Evidence' slide — a single
        # consolidated one is always appended at the end (avoids the duplicate).
        if _is_sources_title(slide_title):
            continue

        # ── section_divider ──────────────────────────────────────────────────
        if layout_key == "section_divider":
            _add_section(prs, slide_title)

        # ── large_text ───────────────────────────────────────────────────────
        elif layout_key == "large_text":
            _add_large_text_slide(prs, slide_title, notes)

        # ── four_boxes_wide ──────────────────────────────────────────────────
        elif layout_key == "four_boxes_wide":
            boxes = raw_boxes if raw_boxes else raw_bullets
            for chunk_boxes in _chunk(boxes, 4):
                _add_four_boxes_wide_slide(prs, slide_title, chunk_boxes, notes)

        # ── six_boxes ────────────────────────────────────────────────────────
        elif layout_key == "six_boxes":
            boxes = raw_boxes if raw_boxes else raw_bullets
            for chunk_boxes in _chunk(boxes, 6):
                _add_six_boxes_slide(prs, slide_title, chunk_boxes, notes)

        # ── four_boxes_stacked ───────────────────────────────────────────────
        elif layout_key == "four_boxes_stacked":
            boxes = raw_boxes if raw_boxes else raw_bullets
            for chunk_boxes in _chunk(boxes, 4):
                _add_four_boxes_stacked_slide(prs, slide_title, chunk_boxes, notes)

        # ── data_2_callouts ──────────────────────────────────────────────────
        elif layout_key == "data_2_callouts":
            stats = slide_spec.get("stats") or []
            if len(stats) >= 2:
                _add_data_2_callouts_slide(
                    prs, slide_title,
                    stat_a_label=stats[0].get("label", ""),
                    stat_a_body=stats[0].get("body", ""),
                    stat_b_label=stats[1].get("label", ""),
                    stat_b_body=stats[1].get("body", ""),
                    notes=notes,
                )
            elif len(raw_bullets) >= 2:
                _add_data_2_callouts_slide(
                    prs, slide_title,
                    stat_a_label=raw_bullets[0],
                    stat_a_body="; ".join(raw_bullets[2:4]) if len(raw_bullets) > 2 else "",
                    stat_b_label=raw_bullets[1],
                    stat_b_body="; ".join(raw_bullets[4:6]) if len(raw_bullets) > 4 else "",
                    notes=notes,
                )
            else:
                # Final fallback: render as title_content
                cap = CAPACITY["title_content"]
                for k, chunk_bullets in enumerate(_chunk(raw_bullets or [""], cap)):
                    chunk_title = slide_title if k == 0 else f"{slide_title} (cont.)"
                    _add_title_content_slide(prs, chunk_title, chunk_bullets, notes if k == 0 else "")

        # ── callout_stat ─────────────────────────────────────────────────────
        elif layout_key == "callout_stat":
            _add_callout_stat_slide(prs, slide_title, raw_bullets, notes)

        # ── agenda ───────────────────────────────────────────────────────────
        elif layout_key == "agenda":
            items = raw_bullets or []
            if raw_columns and len(raw_columns) >= 2:
                items_left  = raw_columns[0]
                items_right = raw_columns[1]
            else:
                mid = max(1, (len(items) + 1) // 2)
                items_left  = items[:mid]
                items_right = items[mid:]
            _add_agenda_slide(prs, slide_title, items_left, items_right, notes)

        # ── end_slide ────────────────────────────────────────────────────────
        elif layout_key == "end_slide":
            continue  # deck terminates on Sources — never render a blank end slide

        # ── process_diagram ──────────────────────────────────────────────────
        elif layout_key == "process_diagram":
            # Accept "steps" list (preferred) or fall back to bullets/boxes.
            # Each step: {"heading": "...", "body": "..."} or a plain string.
            raw_steps = slide_spec.get("steps") or []
            if not raw_steps and raw_bullets:
                raw_steps = [{"heading": b, "body": ""} for b in raw_bullets]
            elif not raw_steps and raw_boxes:
                raw_steps = [{"heading": b, "body": ""} for b in raw_boxes]
            _add_process_diagram_slide(prs, slide_title, raw_steps[:7], notes)

        # ── technical_architecture ───────────────────────────────────────────
        elif layout_key == "technical_architecture":
            context = slide_spec.get("context") or (raw_bullets[0] if raw_bullets else "")
            _add_technical_architecture_slide(prs, slide_title, context, notes)

        # ── timeline ─────────────────────────────────────────────────────────
        elif layout_key == "timeline":
            context = slide_spec.get("context") or (raw_bullets[0] if raw_bullets else "")
            _add_timeline_slide(prs, slide_title, context, notes)

        # ── hierarchy ────────────────────────────────────────────────────────
        elif layout_key == "hierarchy":
            _add_hierarchy_slide(prs, slide_title, raw_bullets, notes)

        # ── value_tree ───────────────────────────────────────────────────────
        elif layout_key == "value_tree":
            _add_value_tree_slide(prs, slide_title, raw_bullets, notes)

        # ── raci ─────────────────────────────────────────────────────────────
        elif layout_key == "raci":
            _add_raci_slide(prs, slide_title, raw_bullets, notes)

        # ── four_column_headlines / three_column ─────────────────────────────
        elif layout_key in ("four_column", "four_column_headlines", "three_column"):
            # S10 has 3 body columns; map "four_column" here as well
            if raw_columns and len(raw_columns) >= 2:
                cols = (list(raw_columns) + [[], [], []])[:3]
            else:
                per = max(1, (len(raw_bullets) + 2) // 3)
                cols = _chunk(raw_bullets, per)[:3]
                while len(cols) < 3:
                    cols.append([])
            heads3 = (list(col_heads) + ["", "", ""])[:3]
            # Trim oversized columns
            cap = CAPACITY["four_column_headlines"]
            trimmed = []
            all_overflow: list[str] = []
            for col in cols:
                trimmed.append(col[:cap])
                all_overflow.extend(col[cap:])
            _add_four_column_headlines_slide(prs, slide_title, heads3, trimmed, notes)
            if all_overflow:
                _park_overflow(prs.slides[-1], all_overflow, "four_column")

        # ── two_col_dividers ─────────────────────────────────────────────────
        elif layout_key == "two_col_dividers":
            if raw_columns and len(raw_columns) >= 2:
                col_a, col_b = raw_columns[0], raw_columns[1]
            else:
                mid   = max(1, len(raw_bullets) // 2)
                col_a = raw_bullets[:mid]
                col_b = raw_bullets[mid:]
            head_a = col_heads[0] if len(col_heads) > 0 else ""
            head_b = col_heads[1] if len(col_heads) > 1 else ""
            _add_two_col_dividers_slide(prs, slide_title, head_a, head_b, col_a, col_b, notes)

        # ── two_column ───────────────────────────────────────────────────────
        elif layout_key == "two_column":
            cap = CAPACITY["two_column"]
            total_cap = cap * 2
            if len(raw_bullets) > total_cap:
                for chunk_bullets in _chunk(raw_bullets, total_cap):
                    _add_two_column_slide(prs, slide_title, chunk_bullets, notes)
            else:
                _add_two_column_slide(prs, slide_title, raw_bullets, notes)

        # ── title_content / callout_stat / default ───────────────────────────
        else:
            cap = CAPACITY["title_content"]
            if len(raw_bullets) > cap:
                chunks = _chunk(raw_bullets, cap)
                for k, chunk_bullets in enumerate(chunks):
                    chunk_title = slide_title if k == 0 else f"{slide_title} (cont.)"
                    _add_title_content_slide(prs, chunk_title, chunk_bullets, notes if k == 0 else "")
            else:
                _add_title_content_slide(prs, slide_title, raw_bullets, notes)

    # ── Sources & Evidence (the deck terminates here — no blank end slide) ────
    _add_sources_slide(prs, all_sources)

    # ── Placeholder-leak elimination + empty-placeholder removal ──────────────
    # 1. Blank any shape still holding template demo text.
    # 2. Delete unused placeholders outright — blanking leaves PowerPoint's
    #    "Click to add text" prompt + dashed border on empty placeholders, which
    #    only shape removal suppresses.
    total_scrubbed = total_removed = 0
    for slide in prs.slides:
        total_scrubbed += _scrub_template_text(slide)
        total_removed += _remove_empty_placeholders(slide)
    if total_scrubbed or total_removed:
        logger.info("[powerpoint] Scrubbed %d demo placeholder(s); removed %d empty placeholder(s)",
                    total_scrubbed, total_removed)

    # ── Post-render QA gate — abort rather than ship a defective deck ─────────
    leaks = _scan_leaks(prs)
    empties = _scan_empty_placeholders(prs)
    if leaks or empties:
        for s_idx, name, txt in leaks:
            logger.error("[powerpoint] Template text LEAKED — slide %d, shape %r: %r", s_idx, name, txt)
        for s_idx, name in empties:
            logger.error("[powerpoint] EMPTY placeholder survived — slide %d, shape %r", s_idx, name)
        first = (f"leak on slide {leaks[0][0]} ({leaks[0][1]!r})" if leaks
                 else f"empty placeholder on slide {empties[0][0]} ({empties[0][1]!r})")
        raise ValueError(
            f"Post-render QA failed: {len(leaks)} leak(s), {len(empties)} empty "
            f"placeholder(s) survived (first: {first}). Generation aborted."
        )

    buf = io.BytesIO()
    # Suppress cosmetic "Duplicate name" UserWarnings from python-pptx's zipfile
    # serialiser — these arise from template-first slide duplication and do not
    # affect the validity or openability of the generated .pptx file.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        prs.save(buf)
    return buf.getvalue()
