#!/usr/bin/env python
"""
Visual QA — geometric + text-fit analysis of rendered PPTX decks.

Phase 7 asked for rendered-image QA (rasterize slides, detect overlap/clipping/
off-slide). A true rasterizer (LibreOffice / PowerPoint / Aspose) is NOT available
in every environment, so this tool does the reliable part deterministically from
the PowerPoint object model and clearly separates EXACT checks from ESTIMATES:

  EXACT   (object geometry):
    - off-slide shapes (any shape extending beyond the slide bounds)
    - content-shape collisions (bounding-box overlap between text-bearing shapes)

  ESTIMATE (font-metric text fit — no rasterizer, so approximate):
    - text likely overflowing its own frame (the "12 short words but very long
      words" case), using resolved/explicit font size + frame dimensions

If LibreOffice `soffice` is on PATH, `--render` additionally rasterizes each slide
to PNG for human review (the true visual inspection). Absent that, geometry+fit is
the automated proxy.

Usage:
  venv/bin/python tools/visual_qa.py --pptx deliverables/benchmarks/client_201.pptx
  venv/bin/python tools/visual_qa.py --dir deliverables/benchmarks [--render out_dir]
"""
from __future__ import annotations

import argparse
import math
import shutil
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pptx import Presentation  # noqa: E402
from pptx.util import Emu  # noqa: E402

EMU_PER_PT = 12700
# Conservative default point sizes for placeholders whose size is INHERITED
# (python-pptx can't cheaply resolve the layout/master chain). Tuned to the IPC
# template's designed sizes on its 26.67"x15" canvas.
_TYPE_DEFAULT_PT = {"TITLE": 36, "CENTER_TITLE": 44, "SUBTITLE": 24, "BODY": 22}
_FALLBACK_PT = 18


def _pt(emu) -> float:
    return (emu or 0) / EMU_PER_PT


def _resolve_pt(shape, para) -> float:
    for r in para.runs:
        if r.font.size:
            return r.font.size.pt
    if para.font.size:
        return para.font.size.pt
    try:
        if shape.is_placeholder:
            return _TYPE_DEFAULT_PT.get(str(shape.placeholder_format.type).split()[0], _FALLBACK_PT)
    except Exception:
        pass
    return _FALLBACK_PT


def _bbox(shape):
    try:
        return (shape.left, shape.top, shape.left + shape.width, shape.top + shape.height)
    except Exception:
        return None


def _overlap_area(a, b) -> float:
    ox = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    oy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return ox * oy


def analyze_pptx(path: Path) -> dict:
    r = analyze_prs(Presentation(str(path)))
    r["path"] = path.name
    return r


def analyze_prs(prs, name: str = "<in-memory>") -> dict:
    W, H = prs.slide_width, prs.slide_height
    findings = []  # (slide_idx, severity, kind, detail)

    for s_idx, slide in enumerate(prs.slides, 1):
        text_shapes = []
        for sh in slide.shapes:
            bb = _bbox(sh)
            if bb is None:
                continue
            # ── EXACT: off-slide ───────────────────────────────────────────
            if bb[0] < -EMU_PER_PT or bb[1] < -EMU_PER_PT or bb[2] > W + EMU_PER_PT or bb[3] > H + EMU_PER_PT:
                findings.append((s_idx, "ERROR", "off_slide",
                                 f"{sh.name}: extends beyond slide bounds"))
            # collect content-bearing text shapes for overlap + fit
            has_text = False
            try:
                has_text = sh.has_text_frame and sh.text_frame.text.strip() != ""
            except Exception:
                pass
            if has_text:
                text_shapes.append((sh, bb))
                # ── ESTIMATE: text fit within own frame ───────────────────
                frame_w_pt = max(1.0, _pt(sh.width) - 14)   # ~7pt L/R inset
                frame_h_pt = max(1.0, _pt(sh.height) - 8)
                needed_pt = 0.0
                for para in sh.text_frame.paragraphs:
                    txt = "".join(r.text for r in para.runs) or para.text
                    if not txt:
                        continue
                    fs = _resolve_pt(sh, para)
                    cpl = max(1.0, frame_w_pt / (0.5 * fs))          # ~0.5*fs avg char width
                    lines = max(1, math.ceil(len(txt) / cpl))
                    needed_pt += lines * fs * 1.2                    # 1.2 line height
                if needed_pt > frame_h_pt * 1.15:                    # 15% tolerance
                    ratio = needed_pt / frame_h_pt
                    sev = "ERROR" if ratio > 1.5 else "WARN"
                    findings.append((s_idx, sev, "text_overflow_estimate",
                                     f"{sh.name}: ~{needed_pt:.0f}pt of text in ~{frame_h_pt:.0f}pt frame "
                                     f"(x{ratio:.2f})"))
        # ── EXACT: content-shape collisions (text over text) ──────────────
        for i in range(len(text_shapes)):
            for j in range(i + 1, len(text_shapes)):
                a, b = text_shapes[i][1], text_shapes[j][1]
                area = _overlap_area(a, b)
                if area <= 0:
                    continue
                min_area = min((a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1])) or 1
                if area / min_area > 0.15:  # >15% of the smaller shape overlaps
                    findings.append((s_idx, "WARN", "text_collision",
                                     f"{text_shapes[i][0].name} overlaps {text_shapes[j][0].name} "
                                     f"({100*area/min_area:.0f}%)"))
    errors = [f for f in findings if f[1] == "ERROR"]
    warns = [f for f in findings if f[1] == "WARN"]
    return {"path": name, "slides": len(prs.slides),
            "errors": errors, "warns": warns, "findings": findings}


def maybe_render(path: Path, out_dir: str):
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        print(f"  [render] skipped — no LibreOffice on PATH (geometry+fit QA only)")
        return
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", out_dir, str(path)], check=False)
    print(f"  [render] rasterized to PDF in {out_dir} for human visual inspection")


def main():
    ap = argparse.ArgumentParser(description="Geometric + text-fit visual QA for PPTX decks")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pptx", type=str)
    g.add_argument("--dir", type=str, help="analyze every *.pptx in the directory")
    ap.add_argument("--render", type=str, default=None, help="if set and LibreOffice exists, rasterize here")
    args = ap.parse_args()

    files = [Path(args.pptx)] if args.pptx else sorted(Path(args.dir).glob("*.pptx"))
    total_err = total_warn = 0
    for f in files:
        r = analyze_pptx(f)
        total_err += len(r["errors"]); total_warn += len(r["warns"])
        status = "PASS" if not r["errors"] else "FAIL"
        print(f"\n===== {r['path']} — {status} ({r['slides']} slides, "
              f"{len(r['errors'])} error(s), {len(r['warns'])} warning(s)) =====")
        for s_idx, sev, kind, detail in r["findings"][:30]:
            print(f"   [{sev}] slide {s_idx} {kind}: {detail}")
        if not r["findings"]:
            print("   no geometric or estimated text-fit issues")
        if args.render:
            maybe_render(f, args.render)
    print(f"\n{'='*54}\nTOTAL: {total_err} error(s), {total_warn} warning(s) across {len(files)} deck(s)")
    print("NOTE: off-slide/collision are EXACT (object geometry); text_overflow_estimate is a "
          "font-metric ESTIMATE. Pixel-accurate QA requires a rasterizer (LibreOffice) — use --render there.")
    return 1 if total_err else 0


if __name__ == "__main__":
    sys.exit(main())
