"""
PowerPoint Generator for Client Materials — IBM Asset Kit edition.

Uses the IBM Asset Kit and Assistant Pack Kit Guidance_2025.pptx as the master
template. The original file is NEVER modified; generation always works on an
in-memory copy.

IBM Template layout reference (verified against actual .pptx file — 47 layouts total, 14 wired):
  ┌──────────────────────────┬──────────────────────────────────────────────┬────────────────────────────────────────────────┐
  │ JSON key                 │ IBM layout name                              │ Placeholder map (idx)                          │
  ├──────────────────────────┼──────────────────────────────────────────────┼────────────────────────────────────────────────┤
  │ title_content            │ Callout, headline                            │ 0=title(left⅓)  1=bullets(right⅔)             │
  │ callout_stat             │ Callout, headline  (alias)                   │ same — use for 2–3 large stat lines            │
  │ two_column               │ Text, 2 wide columns                         │ 0=title  1=left col  21=right col              │
  │ two_col_dividers         │ Text, 2 columns, dividers, large title       │ 0=title  1=col-A head  21=col-B head           │
  │                          │                                              │ 22=col-A body  23=col-B body                   │
  │ four_column              │ Text, 4 columns                              │ 0=title  1,21,22,23=col bodies                 │
  │ four_column_headlines    │ Text, 4 columns, dividers, headlines         │ 0=title  1,21,22,23=col bodies  23,24,25=heads │
  │ four_boxes_wide          │ Boxes, 4 horizontal, large title             │ 0=title  1,21,22,23=box bodies                 │
  │ four_boxes_stacked       │ Boxes, 4 stacked, large title                │ 0=title  1,21=top boxes  22,23=bottom boxes    │
  │                          │                                              │ 24=accent strip (short text)                   │
  │ six_boxes                │ Boxes, 6 stacked                             │ 0=title  1,21=top  22,23=mid  24,25=bottom     │
  │ data_2_callouts          │ Data, 2 callouts, horizontal                 │ 0=stat-A label  1=stat-A body                  │
  │                          │                                              │ 21=stat-B label  22=stat-B body                │
  │ large_text               │ Large text                                   │ 0=full-slide bold statement                    │
  │ agenda                   │ Contents                                     │ 0=title  1=left col  21=right col              │
  │ section_divider          │ Section divider                              │ 0=section title only                           │
  │ end_slide                │ End slide                                    │ (no text placeholders — branded closer)        │
  └──────────────────────────┴──────────────────────────────────────────────┴────────────────────────────────────────────────┘

Layouts intentionally NOT wired (require images, deprecated, or non-consulting use):
  Cover, imagery / Cover, imagery half/half-BU — need picture placeholder
  All "Video or imagery" variants — need picture placeholder
  Contacts, profiles, contributors — needs headshots
  Text, 4 columns, dividers, pictograms / Text, 2 columns, dividers, pictograms — need icons
  Boxes, 4 stacked wide pictograms / Boxes, 6 stacked, icons — need icons
  Table — python-pptx table API is separate; not wired
  Legal disclaimer — boilerplate only
  v1_standard / v1_client pain points — legacy layouts

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

import io
import logging
import re
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.util import Pt

logger = logging.getLogger(__name__)

TEMPLATE_PATH = (
    Path(__file__).parent.parent
    / "templates"
    / "IBM Asset Kit and Assistant Pack Kit Guidance_2025.pptx"
)

# ── IBM template layout names (exact strings from the .pptx file) ─────────────
LAYOUT_COVER               = "Cover, cyan"
LAYOUT_SECTION             = "Section divider"
LAYOUT_CALLOUT             = "Callout, headline"
LAYOUT_TWO_COL             = "Text, 2 wide columns"
LAYOUT_TWO_COL_DIVIDERS    = "Text, 2 columns, dividers, large title"
LAYOUT_FOUR_COL            = "Text, 4 columns "          # note trailing space in template
LAYOUT_FOUR_COL_HEADLINES  = "Text, 4 columns, dividers, headlines"
LAYOUT_FOUR_BOXES_WIDE     = "Boxes, 4 horizontal, large title"
LAYOUT_FOUR_BOXES_STACKED  = "Boxes, 4 stacked, large title"
LAYOUT_SIX_BOXES           = "Boxes, 6 stacked"
LAYOUT_DATA_2_CALLOUTS     = "Data, 2 callouts, horizontal"
LAYOUT_LARGE_TEXT          = "Large text"
LAYOUT_AGENDA              = "Contents"
LAYOUT_END                 = "End slide"

# ── JSON key → IBM layout name ────────────────────────────────────────────────
LAYOUT_MAP: dict[str, str] = {
    "cover":                 LAYOUT_COVER,
    "section_divider":       LAYOUT_SECTION,
    "title_content":         LAYOUT_CALLOUT,
    "callout_stat":          LAYOUT_CALLOUT,            # alias — stat lines in bullets
    "two_column":            LAYOUT_TWO_COL,
    "two_col_dividers":      LAYOUT_TWO_COL_DIVIDERS,
    "four_column":           LAYOUT_FOUR_COL,
    "four_column_headlines": LAYOUT_FOUR_COL_HEADLINES,
    "four_boxes_wide":       LAYOUT_FOUR_BOXES_WIDE,
    "four_boxes_stacked":    LAYOUT_FOUR_BOXES_STACKED,
    "six_boxes":             LAYOUT_SIX_BOXES,
    "data_2_callouts":       LAYOUT_DATA_2_CALLOUTS,
    "large_text":            LAYOUT_LARGE_TEXT,
    "agenda":                LAYOUT_AGENDA,
    "end_slide":             LAYOUT_END,
}

# ── Per-layout bullet/item capacity (overflow → speaker notes or auto-split) ──
CAPACITY: dict[str, int] = {
    "title_content":         7,   # right-side body area
    "callout_stat":          5,   # stat lines — keep sparse
    "two_column":            5,   # per column (total 10)
    "two_col_dividers":      5,   # per column body (total 10)
    "four_column":           4,   # per column body (total 16)
    "four_column_headlines": 4,   # per column body (total 16)
    "four_boxes_wide":       3,   # per box — keep concise
    "four_boxes_stacked":    3,   # per box
    "six_boxes":             2,   # per box — very concise
    "data_2_callouts":       4,   # per stat body area
    "large_text":            1,   # single statement
    "agenda":                8,   # per column
    "section_divider":       0,   # no bullets
    "end_slide":             0,   # no text
}

# ── Font sizes ────────────────────────────────────────────────────────────────
FONT_TITLE_COVER   = 32
FONT_TITLE_SECTION = 28
FONT_TITLE_CONTENT = 20
FONT_BODY          = 14
FONT_SMALL         = 13
FONT_BOX           = 12
FONT_SOURCES       = 11

# ── Strip [Source: ...] inline citations from bullet text ─────────────────────
_SOURCE_TAG_RE = re.compile(r"\s*\[Source:[^\]]*\]", re.IGNORECASE)


# ── Quality validators ────────────────────────────────────────────────────────

def validate_bullets(slides: list[dict]) -> None:
    """
    Log a warning for every bullet that ends with '...' or '…'.
    Called before PPTX assembly so regressions are visible in server logs.
    """
    for i, slide in enumerate(slides, 1):
        for j, bullet in enumerate(slide.get("bullets", []), 1):
            if isinstance(bullet, str) and (bullet.rstrip().endswith("...") or bullet.rstrip().endswith("…")):
                logger.warning(
                    "Slide %d bullet %d ends with ellipsis (truncated sentence): %r",
                    i, j, bullet[:80],
                )


def _clean_bullet(text: str) -> str:
    """
    Strip [Source: ...] inline citations.  Keep [Inferred] markers.
    Never truncate or append ellipsis — bullets are complete sentences
    enforced at the LLM prompt level.
    """
    return _SOURCE_TAG_RE.sub("", text).strip()


# ── Layout helpers ─────────────────────────────────────────────────────────────

def _get_layout(prs: Presentation, layout_name: str):
    """Return a slide layout by exact name; fall back to Callout, headline."""
    for layout in prs.slide_master.slide_layouts:
        if layout.name == layout_name:
            return layout
    # Second pass: strip trailing whitespace (template names can have a trailing space)
    for layout in prs.slide_master.slide_layouts:
        if layout.name.strip() == layout_name.strip():
            return layout
    logger.warning("Layout %r not found — falling back to 'Callout, headline'.", layout_name)
    return _get_layout(prs, LAYOUT_CALLOUT)


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
    font_size: int = FONT_BODY,
) -> list[str]:
    """
    Write a cleaned bullet list into a placeholder.
    Returns any overflow bullets that did not fit (caller handles them).
    """
    ph = _ph_by_idx(slide, idx)
    if ph is None:
        return bullets  # nothing written — return all as overflow

    cleaned = [_clean_bullet(b) for b in bullets if b.strip()]
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


# ── Slide management ──────────────────────────────────────────────────────────

def _clear_slides(prs: Presentation) -> None:
    """
    Remove all existing slides, preserving slide master, layouts, and theme.

    Order matters: clear <p:sldIdLst> XML first (avoids touching the lazy
    prs.slides property), then pop relationship entries so iter_parts() does
    not visit old slide objects during serialisation (avoids Duplicate-name
    ZIP warnings).
    """
    prs_elem = prs.part._element  # noqa: SLF001
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
        if rid in prs.part._rels:  # noqa: SLF001
            prs.part._rels.pop(rid)  # noqa: SLF001


# ── Slide builders ─────────────────────────────────────────────────────────────
#
# Each builder receives pre-cleaned, validated bullet lists.
# The caller (generate_pptx) is responsible for auto-splitting oversized bullet
# lists before calling these builders.

def _add_cover(prs: Presentation, title: str, subtitle: str) -> None:
    """
    Cover, cyan:  idx=0 title · idx=1 subtitle-left · idx=21 subtitle-right
    """
    layout = _get_layout(prs, LAYOUT_COVER)
    slide  = prs.slides.add_slide(layout)
    _write_ph(slide, 0, title, font_size=FONT_TITLE_COVER)
    if " · " in subtitle:
        left, right = subtitle.split(" · ", 1)
        _write_ph(slide, 1,  left)
        _write_ph(slide, 21, right)
    else:
        _write_ph(slide, 1, subtitle)


def _add_section(prs: Presentation, title: str) -> None:
    """
    Section divider:  idx=0 section title
    """
    layout = _get_layout(prs, LAYOUT_SECTION)
    slide  = prs.slides.add_slide(layout)
    _write_ph(slide, 0, title, font_size=FONT_TITLE_SECTION)


def _add_callout_slide(
    prs: Presentation,
    title: str,
    bullets: list[str],
    notes: str = "",
) -> None:
    """
    Callout, headline:  idx=0 headline (left ⅓ — 4.06"×1.17") · idx=1 content (right ⅔)
    Primary IBM consulting layout — title states the takeaway, bullets support it.

    The left panel is only 4.06" wide × 1.17" tall.  We use FONT_SMALL (13pt) so that
    a typical takeaway title (8–12 words) fits without truncation.  Longer titles wrap
    naturally within the box.
    """
    layout = _get_layout(prs, LAYOUT_CALLOUT)
    slide  = prs.slides.add_slide(layout)
    _write_ph(slide, 0, title, font_size=FONT_SMALL)   # 13pt fits in 4.06"×1.17" box
    overflow = _write_bullets(slide, 1, bullets, max_bullets=CAPACITY["title_content"])
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
    Text, 2 wide columns:  idx=0 title · idx=1 left col · idx=21 right col
    Use for comparisons, parallel themes, or 6–10 distinct points.
    """
    layout = _get_layout(prs, LAYOUT_TWO_COL)
    slide  = prs.slides.add_slide(layout)
    _write_ph(slide, 0, title, font_size=FONT_TITLE_CONTENT)

    cap = CAPACITY["two_column"]
    mid = max(1, (len(bullets) + 1) // 2)
    ov1 = _write_bullets(slide, 1,  bullets[:mid], max_bullets=cap)
    ov2 = _write_bullets(slide, 21, bullets[mid:], max_bullets=cap)
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
    Text, 2 columns, dividers, large title (verified geometry):
      idx=0  large title (left half  — 4.53"×1.88" at x=0.24")
      idx=1  col-A column heading   (right half — 2.03"×0.63" at x=5.23")
      idx=21 col-B column heading   (right half — 2.03"×0.63" at x=7.73")
      idx=22 col-A body bullets     (right half — 2.03"×3.52" at x=5.23")
      idx=23 col-B body bullets     (right half — 2.03"×3.52" at x=7.74")
    The title box is tall (1.88") so we cap at FONT_SMALL (13pt) to keep it tidy.
    """
    layout = _get_layout(prs, LAYOUT_TWO_COL_DIVIDERS)
    slide  = prs.slides.add_slide(layout)
    _write_ph(slide, 0,  title,      font_size=FONT_SMALL)   # 13pt in 4.53"×1.88" box
    _write_ph(slide, 1,  col_a_head, font_size=FONT_SMALL)
    _write_ph(slide, 21, col_b_head, font_size=FONT_SMALL)
    cap = CAPACITY["two_col_dividers"]
    ov_a = _write_bullets(slide, 22, col_a_bullets, max_bullets=cap)
    ov_b = _write_bullets(slide, 23, col_b_bullets, max_bullets=cap)
    _park_overflow(slide, ov_a + ov_b, "two_col_dividers")
    if notes:
        _append_notes(slide, notes)


def _add_four_column_slide(
    prs: Presentation,
    title: str,
    columns: list[list[str]],
    col_heads: list[str] | None = None,
    notes: str = "",
) -> None:
    """
    Text, 4 columns (verified geometry):
      Without heads (four_column):
        idx=0  slide title (2.03"×0.63" at x=0.24")
        idx=1  col-A body (2.03"×3.52" at x=0.23")
        idx=21 col-B body (2.03"×3.52" at x=2.73")
        idx=22 col-C body (2.03"×3.52" at x=5.23")
        idx=23 col-D body (2.03"×3.52" at x=7.73")
      With heads (four_column_headlines):
        idx=0  slide title (2.03"×0.63" at x=0.24")  ← no 1st-col head in this layout
        idx=23 col-B head  (2.03"×0.63" at x=2.73")
        idx=24 col-C head  (2.03"×0.63" at x=5.23")
        idx=25 col-D head  (2.03"×0.63" at x=7.73")
        idx=1  col-A body  (2.03"×3.52" at x=2.73")
        idx=21 col-B body  (2.03"×3.52" at x=5.23")
        idx=22 col-C body  (2.03"×3.52" at x=7.73")
        Note: headlines layout has only 3 body columns (idx 1,21,22); the 4th column
        placeholder idx=23 is the col-B head in this layout.

    columns must have the right count for the layout (4 for no-heads, 3 for heads).
    col_heads must have exactly 3 entries for four_column_headlines.
    """
    use_headlines = bool(col_heads and len(col_heads) >= 3)
    layout_name   = LAYOUT_FOUR_COL_HEADLINES if use_headlines else LAYOUT_FOUR_COL
    layout        = _get_layout(prs, layout_name)
    slide         = prs.slides.add_slide(layout)
    _write_ph(slide, 0, title, font_size=FONT_SMALL)

    cap = CAPACITY["four_column"]

    if use_headlines:
        # four_column_headlines: 3 body cols (idx 1,21,22) + 3 col heads (idx 23,24,25)
        # Pad columns to 3
        cols3 = (columns + [[], [], []])[:3]
        body_idxs = [1, 21, 22]
        head_idxs = [23, 24, 25]
        for i, hidx in enumerate(head_idxs):
            head_text = col_heads[i] if i < len(col_heads) else ""
            _write_ph(slide, hidx, head_text, font_size=FONT_SMALL)
        for i, col_bullets in enumerate(cols3):
            overflow = _write_bullets(slide, body_idxs[i], col_bullets, max_bullets=cap, font_size=FONT_BOX)
            _park_overflow(slide, overflow, f"four_column_headlines col{i+1}")
    else:
        # four_column: 4 body cols (idx 1,21,22,23)
        columns4 = (columns + [[], [], [], []])[:4]
        body_idxs = [1, 21, 22, 23]
        for i, col_bullets in enumerate(columns4):
            overflow = _write_bullets(slide, body_idxs[i], col_bullets, max_bullets=cap, font_size=FONT_BOX)
            _park_overflow(slide, overflow, f"four_column col{i+1}")

    if notes:
        _append_notes(slide, notes)


def _add_four_boxes_wide_slide(
    prs: Presentation,
    title: str,
    boxes: list[str],
    notes: str = "",
) -> None:
    """
    Boxes, 4 horizontal, large title:
      idx=0 large title (top half)
      idx=1, 21, 22, 23 → box bodies (bottom row, left to right)

    boxes: list of 4 strings — one concise paragraph per box.
    Use for: 4 strategic priorities, 4 workstreams, 4 recommendations.
    """
    layout = _get_layout(prs, LAYOUT_FOUR_BOXES_WIDE)
    slide  = prs.slides.add_slide(layout)
    _write_ph(slide, 0, title, font_size=FONT_TITLE_CONTENT)
    box_idxs = [1, 21, 22, 23]
    boxes = (boxes + ["", "", "", ""])[:4]
    for i, box_text in enumerate(boxes):
        _write_ph(slide, box_idxs[i], box_text, font_size=FONT_BOX)
    if notes:
        _append_notes(slide, notes)


def _add_six_boxes_slide(
    prs: Presentation,
    title: str,
    boxes: list[str],
    notes: str = "",
) -> None:
    """
    Boxes, 6 stacked:
      idx=0  title (left column, top)
      idx=1  top-right box A
      idx=21 top-right box B
      idx=22 mid-right box C
      idx=23 mid-right box D
      idx=24 mid-centre box E
      idx=25 mid-centre box F

    boxes: list of 6 short strings.
    Use for: 6 capabilities, 6 initiatives, 6 components.
    """
    layout = _get_layout(prs, LAYOUT_SIX_BOXES)
    slide  = prs.slides.add_slide(layout)
    _write_ph(slide, 0, title, font_size=FONT_TITLE_CONTENT)
    box_idxs = [1, 21, 22, 23, 24, 25]
    boxes = (boxes + [""] * 6)[:6]
    for i, box_text in enumerate(boxes):
        _write_ph(slide, box_idxs[i], box_text, font_size=FONT_BOX)
    if notes:
        _append_notes(slide, notes)


def _add_large_text_slide(
    prs: Presentation,
    title: str,
    statement: str,
    notes: str = "",
) -> None:
    """
    Large text:  idx=0 large bold statement (fills most of slide).
    Use for: single key insight, bottom-line finding, memorable quote.
    The 'title' field feeds the statement; 'statement' goes to notes.
    """
    layout = _get_layout(prs, LAYOUT_LARGE_TEXT)
    slide  = prs.slides.add_slide(layout)
    _write_ph(slide, 0, title, font_size=28)
    if statement:
        _append_notes(slide, statement)
    if notes:
        _append_notes(slide, notes)


def _add_four_boxes_stacked_slide(
    prs: Presentation,
    title: str,
    boxes: list[str],
    accent: str = "",
    notes: str = "",
) -> None:
    """
    Boxes, 4 stacked, large title:
      idx=0  large title (left side, mid-height)
      idx=1  top-right box A
      idx=21 top-right box B
      idx=22 bottom-right box C
      idx=23 bottom-right box D
      idx=24 accent strip (very short label, top of left column)

    boxes: list of 4 strings.
    Use for: 4 workstreams stacked, 4 risk areas, 4 capability domains.
    Distinct from four_boxes_wide — boxes are stacked in a 2×2 grid on the right.
    """
    layout = _get_layout(prs, LAYOUT_FOUR_BOXES_STACKED)
    slide  = prs.slides.add_slide(layout)
    _write_ph(slide, 0, title, font_size=FONT_TITLE_CONTENT)
    box_idxs = [1, 21, 22, 23]
    boxes = (boxes + ["", "", "", ""])[:4]
    for i, box_text in enumerate(boxes):
        _write_ph(slide, box_idxs[i], box_text, font_size=FONT_BOX)
    if accent:
        _write_ph(slide, 24, accent, font_size=FONT_BOX)
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
    Data, 2 callouts, horizontal:
      idx=0  stat-A large label  (top-left, 2.03×2.19in — the big number/metric)
      idx=1  stat-A body         (top-right, 4.53×2.19in — supporting bullets)
      idx=21 stat-B large label  (bottom-left)
      idx=22 stat-B body         (bottom-right)

    Use for: 2 paired key metrics with supporting context.
    Examples: revenue + growth context / cost reduction + timeline context.
    Keep labels short and punchy (e.g. "CAD 18.2B", "94%", "+8% YoY").
    """
    layout = _get_layout(prs, LAYOUT_DATA_2_CALLOUTS)
    slide  = prs.slides.add_slide(layout)
    _write_ph(slide, 0,  stat_a_label, font_size=24)
    _write_ph(slide, 21, stat_b_label, font_size=24)
    cap = CAPACITY["data_2_callouts"]
    _write_bullets(slide, 1,  [stat_a_body] if isinstance(stat_a_body, str) else stat_a_body, max_bullets=cap)
    _write_bullets(slide, 22, [stat_b_body] if isinstance(stat_b_body, str) else stat_b_body, max_bullets=cap)
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
    Contents layout (used as Agenda):
      idx=0  title (large, left column, top area)
      idx=1  left agenda column
      idx=21 right agenda column

    Use for: deck agenda / table of contents / section overview.
    Keep items concise — one line per agenda point.
    """
    layout = _get_layout(prs, LAYOUT_AGENDA)
    slide  = prs.slides.add_slide(layout)
    _write_ph(slide, 0, title, font_size=FONT_TITLE_CONTENT)
    cap = CAPACITY["agenda"]
    ov_l = _write_bullets(slide, 1,  items_left,  max_bullets=cap, font_size=FONT_BODY)
    ov_r = _write_bullets(slide, 21, items_right, max_bullets=cap, font_size=FONT_BODY)
    _park_overflow(slide, ov_l + ov_r, "agenda")
    if notes:
        _append_notes(slide, notes)


def _add_end_slide(prs: Presentation) -> None:
    """
    End slide — IBM-branded closing slide.
    No text placeholders; the branding/design is entirely in the layout.
    Use as the final slide of every deck (after Sources & Evidence).
    """
    layout = _get_layout(prs, LAYOUT_END)
    prs.slides.add_slide(layout)


def _add_sources_slide(prs: Presentation, sources: list[str]) -> None:
    """
    Single consolidated Sources & Evidence slide (Callout, headline).
    This is the ONLY place source citations appear in the presentation.
    """
    layout = _get_layout(prs, LAYOUT_CALLOUT)
    slide  = prs.slides.add_slide(layout)
    _write_ph(slide, 0, "Sources & Evidence", font_size=FONT_TITLE_CONTENT)

    seen: set[str] = set()
    unique: list[str] = []
    for s in sources:
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            unique.append(s)

    bullets = unique if unique else ["No source documents recorded."]
    overflow = _write_bullets(slide, 1, bullets, max_bullets=14, font_size=FONT_SOURCES)
    if overflow:
        _append_notes(slide, "\nAdditional sources:\n" + "\n".join(f"• {b}" for b in overflow))


# ── Auto-split helper ─────────────────────────────────────────────────────────

def _chunk(lst: list, size: int) -> list[list]:
    """Split a list into chunks of at most `size`."""
    return [lst[i:i + size] for i in range(0, max(len(lst), 1), size)]


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_pptx(deck_spec: dict[str, Any], workspace_name: str) -> bytes:
    """
    Build a .pptx from a structured LLM deck spec and return file bytes.

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
                   | "four_column" | "four_column_headlines"
                   | "four_boxes_wide" | "six_boxes"
                   | "large_text" | "section_divider",
          "bullets": [...],         # flat list for most layouts
          "columns": [[...],[...]], # for two_col_dividers / four_column (optional override)
          "col_heads": ["A","B"],   # column headings for two_col_dividers / four_column_headlines
          "boxes": [...],           # for four_boxes_wide / six_boxes (optional override)
          "notes": "Speaker notes",
          "sources": [...]
        }
      ],
      "metadata": {
        "source_documents": [...],
        "generation_notes": "..."
      }
    }
    """
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(
            f"IBM template not found at:\n  {TEMPLATE_PATH}\n"
            "Place the IBM Asset Kit .pptx file at that path."
        )

    prs = Presentation(io.BytesIO(TEMPLATE_PATH.read_bytes()))
    _clear_slides(prs)

    title            = deck_spec.get("title", "Client Material")
    deliverable_type = deck_spec.get("deliverable_type", "")
    slides           = deck_spec.get("slides", [])
    metadata         = deck_spec.get("metadata", {})
    all_sources      = metadata.get("source_documents", [])
    gen_notes        = metadata.get("generation_notes", "")

    # ── Pre-flight: validate bullets for trailing ellipsis ────────────────────
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

        # Accept both "bullets" (flat list) and dedicated structured fields.
        raw_bullets: list[str] = [
            b for b in (slide_spec.get("bullets") or [])
            if isinstance(b, str) and b.strip()
        ]
        raw_columns: list[list[str]] = slide_spec.get("columns") or []
        col_heads: list[str]         = slide_spec.get("col_heads") or []
        raw_boxes: list[str]         = slide_spec.get("boxes") or []

        if not slide_title:
            continue  # skip blank slides

        # ── section_divider ──────────────────────────────────────────────────
        if layout_key == "section_divider":
            _add_section(prs, slide_title)

        # ── large_text ───────────────────────────────────────────────────────
        elif layout_key == "large_text":
            statement = raw_bullets[0] if raw_bullets else ""
            _add_large_text_slide(prs, slide_title, statement, notes)

        # ── four_boxes_wide ──────────────────────────────────────────────────
        elif layout_key == "four_boxes_wide":
            boxes = raw_boxes if raw_boxes else raw_bullets
            cap = CAPACITY["four_boxes_wide"]
            if len(boxes) > 4:
                # Extra boxes → split into continuation slide(s)
                for chunk_boxes in _chunk(boxes, 4):
                    _add_four_boxes_wide_slide(prs, slide_title, chunk_boxes, notes)
            else:
                _add_four_boxes_wide_slide(prs, slide_title, boxes, notes)

        # ── six_boxes ────────────────────────────────────────────────────────
        elif layout_key == "six_boxes":
            boxes = raw_boxes if raw_boxes else raw_bullets
            if len(boxes) > 6:
                for chunk_boxes in _chunk(boxes, 6):
                    _add_six_boxes_slide(prs, slide_title, chunk_boxes, notes)
            else:
                _add_six_boxes_slide(prs, slide_title, boxes, notes)

        # ── four_boxes_stacked ───────────────────────────────────────────────
        elif layout_key == "four_boxes_stacked":
            boxes = raw_boxes if raw_boxes else raw_bullets
            accent = slide_spec.get("accent") or ""
            if len(boxes) > 4:
                for chunk_boxes in _chunk(boxes, 4):
                    _add_four_boxes_stacked_slide(prs, slide_title, chunk_boxes, accent, notes)
            else:
                _add_four_boxes_stacked_slide(prs, slide_title, boxes, accent, notes)

        # ── data_2_callouts ──────────────────────────────────────────────────
        elif layout_key == "data_2_callouts":
            # Expects: "stats": [{"label": "CAD 18B", "body": "revenue context"}, ...]
            # Falls back to first two bullets as labels, rest as body
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
                # Fallback: treat first bullet as label-A, second as label-B
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
                _add_callout_slide(prs, slide_title, raw_bullets, notes)

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
            _add_end_slide(prs)

        # ── four_column / four_column_headlines ──────────────────────────────
        elif layout_key in ("four_column", "four_column_headlines"):
            # four_column_headlines has only 3 body columns (IBM template verified).
            # four_column has 4 body columns.
            if layout_key == "four_column_headlines":
                n_cols = 3
                use_heads = col_heads[:3] if len(col_heads) >= 3 else None
                if raw_columns and len(raw_columns) >= 3:
                    columns = raw_columns[:3]
                else:
                    per = max(1, (len(raw_bullets) + 2) // 3)
                    columns = _chunk(raw_bullets, per)[:3]
                    while len(columns) < 3:
                        columns.append([])
            else:
                n_cols = 4
                use_heads = None
                if raw_columns and len(raw_columns) >= 4:
                    columns = raw_columns[:4]
                else:
                    per = max(1, (len(raw_bullets) + 3) // 4)
                    columns = _chunk(raw_bullets, per)[:4]
                    while len(columns) < 4:
                        columns.append([])
            # auto-truncate oversized columns to capacity; overflow → notes
            cap = CAPACITY["four_column"]
            max_col = max((len(c) for c in columns), default=0)
            if max_col > cap:
                trimmed_cols = [c[:cap] for c in columns]
                overflow_all = [b for c in columns for b in c[cap:]]
                _add_four_column_slide(prs, slide_title, trimmed_cols, use_heads, notes)
                if overflow_all:
                    _park_overflow(prs.slides[-1], overflow_all, "four_column")
            else:
                _add_four_column_slide(prs, slide_title, columns, use_heads, notes)

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
                # Auto-split into pages
                for chunk_bullets in _chunk(raw_bullets, total_cap):
                    _add_two_column_slide(prs, slide_title, chunk_bullets, notes)
            else:
                _add_two_column_slide(prs, slide_title, raw_bullets, notes)

        # ── title_content / callout_stat (default) ───────────────────────────
        else:
            cap = CAPACITY["title_content"]
            if len(raw_bullets) > cap:
                # Auto-split: create continuation slides
                chunks = _chunk(raw_bullets, cap)
                for k, chunk_bullets in enumerate(chunks):
                    chunk_title = slide_title if k == 0 else f"{slide_title} (cont.)"
                    _add_callout_slide(prs, chunk_title, chunk_bullets, notes if k == 0 else "")
            else:
                _add_callout_slide(prs, slide_title, raw_bullets, notes)

    # ── Sources & Evidence ────────────────────────────────────────────────────
    _add_sources_slide(prs, all_sources)

    # ── End slide (IBM-branded closer, always appended) ───────────────────────
    _add_end_slide(prs)

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()
