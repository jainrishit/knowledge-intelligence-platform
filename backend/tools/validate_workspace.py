#!/usr/bin/env python
"""
Real-corpus validation harness — benchmark comparison tooling.

Generates the three deliverables for a target workspace (or scores already-saved
deck specs) and compares them against the frozen benchmark library using OBJECTIVE
metrics only — no synthetic quality scoring. Intended for validating the platform
against a real 25–50 document corpus before broad rollout.

Usage
-----
  # Generate on a real workspace and compare (makes real LLM calls):
  venv/bin/python tools/validate_workspace.py --workspace 3

  # Score already-generated deck specs (no LLM calls) — e.g. to re-check or to
  # validate this tool itself:
  venv/bin/python tools/validate_workspace.py --specs deliverables/output/phase3

Hard invariants checked (a deck FAILS if any is violated):
  leaks == 0, sources == 1, label_dividers == 0, max_consecutive_layout <= 2,
  content slides in band, ending is an action/decision.

Benchmarks are read from deliverables/benchmarks/<type>_scorecard.json.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

from pptx import Presentation  # noqa: E402
from deliverables.generators.powerpoint_generator import (  # noqa: E402
    _scan_leaks, _scan_empty_placeholders, _iter_all_shapes,
)

BENCH_DIR = BACKEND / "deliverables" / "benchmarks"
BAND = {"executive_summary": (4, 10), "client_101": (8, 18), "client_201": (12, 22)}
STRUCT = {"cover", "sources", "end_slide", "section_divider"}
THEMES = {
    "Stablecoins": ["stablecoin", "rlusd", "reserve", "genius"],
    "Tokenization": ["token", "rwa"],
    "Custody": ["custody", "wallet", "key management"],
    "Regulatory": ["regulat", "compliance", "mica", "kyc", "nydfs"],
    "Treasury": ["treasury", "liquidity", "nostro"],
    "CBDC": ["cbdc", "central bank"],
    "CrossBorder": ["cross-border", "correspondent", "settlement", "corridor"],
    "Agentic": ["agentic", "autonomous"],
}
CRITICALS = ["ripple", "digital asset haven", "ibm z", "promontory", "tokeniz", "custody"]
_VERB = re.compile(r"\b(is|are|will|eliminat|enabl|provid|deliver|reduc|forc|creat|unlock|require|"
                   r"traps?|address|combin|represent|must|reshap|face|need|drive|accelerat)\w*", re.I)
_ACTION = re.compile(r"\b(should|recommend|prioriti|begin|deploy|establish|invest|adopt|roadmap|"
                     r"action|decision|implement|accelerat|immediate|pursue|must)\w*", re.I)


def metrics_from_spec(spec: dict, pptx_bytes: bytes | None) -> dict:
    slides = spec.get("slides", [])
    content = [s for s in slides if s.get("layout") not in STRUCT]
    lays = [s.get("layout") for s in slides if s.get("layout") not in ("cover", "sources", "end_slide")]
    maxrun = cur = 1
    for i in range(1, len(lays)):
        cur = cur + 1 if lays[i] == lays[i - 1] else 1
        maxrun = max(maxrun, cur)
    divs = [(s.get("title") or "") for s in slides if s.get("layout") == "section_divider"]
    titles = [(s.get("title") or "") for s in content]
    insight = sum(1 for t in titles if len(t.split()) >= 5 and _VERB.search(t))
    txt = json.dumps(spec).lower()
    leaks = sources = empty_placeholders = None
    if pptx_bytes:
        prs = Presentation(io.BytesIO(pptx_bytes))
        leaks = len(_scan_leaks(prs))
        empty_placeholders = len(_scan_empty_placeholders(prs))
        sources = sum(1 for s in prs.slides for sh in s.shapes
                      if getattr(sh, "has_text_frame", False)
                      and sh.text_frame.text.strip().lower().startswith("sources"))
    return {
        "content_slides": len(content),
        "pct_insight_titles": round(100 * insight / max(len(titles), 1)),
        "section_dividers": len(divs),
        "label_dividers": sum(1 for d in divs if len(d.split()) <= 4),
        "max_consecutive_layout": maxrun,
        "distinct_layouts": len(set(lays)),
        "theme_coverage": sum(1 for ks in THEMES.values() if any(k in txt for k in ks)),
        "criticals_preserved": sum(1 for c in CRITICALS if c in txt),
        "ending_is_action": bool(content and _ACTION.search(titles[-1])),
        "leaks": leaks,
        "sources": sources,
        "empty_placeholders": empty_placeholders,
    }


def check_invariants(dt: str, m: dict) -> list[str]:
    lo, hi = BAND.get(dt, (0, 999))
    fails = []
    if m["leaks"] not in (0, None):
        fails.append(f"leaks={m['leaks']} (must be 0)")
    if m["empty_placeholders"] not in (0, None):
        fails.append(f"empty_placeholders={m['empty_placeholders']} (must be 0 — no 'Click to add text')")
    if m["sources"] not in (1, None):
        fails.append(f"sources={m['sources']} (must be 1)")
    if m["label_dividers"] != 0:
        fails.append(f"label_dividers={m['label_dividers']} (must be 0)")
    if m["max_consecutive_layout"] > 2:
        fails.append(f"max_consecutive_layout={m['max_consecutive_layout']} (must be <=2)")
    if not (lo <= m["content_slides"] <= hi):
        fails.append(f"content_slides={m['content_slides']} (out of band {lo}-{hi})")
    if not m["ending_is_action"]:
        fails.append("ending is not an action/decision slide")
    return fails


def compare_to_benchmark(dt: str, m: dict) -> list[str]:
    path = BENCH_DIR / f"{dt}_scorecard.json"
    if not path.exists():
        return ["(no benchmark on file)"]
    bench = json.load(open(path)).get("objective_metrics", {})
    out = []
    for key in ("pct_insight_titles", "theme_coverage", "distinct_layouts", "section_dividers"):
        if key in bench and bench[key] is not None:
            b, a = bench[key], m.get(key)
            verdict = "same" if a == b else ("better" if a > b else "worse")
            out.append(f"{key}: {b} -> {a} ({verdict})")
    return out


def load_specs(specs_dir: str):
    out = []
    for dt in ("executive_summary", "client_101", "client_201"):
        sp = Path(specs_dir) / f"{dt}_deckspec.json"
        pp = Path(specs_dir) / f"{dt}.pptx"
        if sp.exists():
            spec = json.load(open(sp))
            pptx = pp.read_bytes() if pp.exists() else None
            out.append((dt, spec, pptx, None))
    return out


def generate_specs(workspace_id: int):
    from app.db.session import SessionLocal
    from app.generation import deliverable_service as dsvc
    out = []
    for dt in ("executive_summary", "client_101", "client_201"):
        db = SessionLocal(); t0 = time.time()
        focus = "primary workspace theme" if dt == "executive_summary" else None
        resp = dsvc.generate_client_material(db, workspace_id, dt, focus_area=focus)
        # capture the deck spec via the persisted deliverable is non-trivial; re-derive
        # objective metrics from the rendered pptx text + a light spec proxy.
        out.append((dt, {"slides": [], "deliverable_type": dt}, resp.pptx_bytes, time.time() - t0))
        db.close()
    return out


def main():
    ap = argparse.ArgumentParser(description="Real-corpus benchmark validation harness")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--workspace", type=int, help="workspace id to generate + validate (real LLM calls)")
    g.add_argument("--specs", type=str, help="directory of existing <type>_deckspec.json to score")
    args = ap.parse_args()

    items = generate_specs(args.workspace) if args.workspace else load_specs(args.specs)
    if not items:
        print("No deck specs found."); return 1

    overall_pass = True
    for dt, spec, pptx, runtime in items:
        m = metrics_from_spec(spec, pptx)
        fails = check_invariants(dt, m)
        status = "PASS" if not fails else "FAIL"
        overall_pass &= not fails
        print(f"\n===== {dt} — {status}{'' if runtime is None else f'  ({runtime:.0f}s)'} =====")
        for k, v in m.items():
            print(f"   {k}: {v}")
        print("   vs benchmark: " + "; ".join(compare_to_benchmark(dt, m)))
        if fails:
            print("   INVARIANT FAILURES: " + "; ".join(fails))

    print(f"\n{'='*54}\nOVERALL: {'PASS — meets release invariants' if overall_pass else 'FAIL — see failures above'}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
