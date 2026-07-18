"""
Live end-to-end validation script — runs the full plan workflow against the real API.
No mocks. No fake responses. Calls the actual running backend.

Usage:
    cd backend && venv/bin/python _live_test.py

Outputs per deliverable type:
  - Prompt size (chars + estimated tokens)
  - Raw Claude response (first 2000 chars + last 500 chars)
  - JSON parse result
  - Plan slide count
  - PPTX generation result
  - Timing per stage
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

BASE = "http://localhost:8000"
WS_ID = 1   # Payment Modernization workspace

# ── Helpers ──────────────────────────────────────────────────────────────────

def req(method: str, path: str, body: dict | None = None, binary: bool = False):
    url = f"{BASE}{path}"
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    rq = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(rq, timeout=360) as r:
            if binary:
                return r.read(), r.status, dict(r.headers)
            return json.loads(r.read()), r.status, {}
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            detail = json.loads(body_bytes)
        except Exception:
            detail = body_bytes.decode(errors="replace")
        return {"error": detail}, e.code, {}


def bar(label: str, n: int = 60) -> None:
    print(f"\n{'═' * n}")
    print(f"  {label}")
    print(f"{'═' * n}")


def check(label: str, ok: bool, detail: str = "") -> None:
    sym = "✅" if ok else "❌"
    msg = f"  {sym}  {label}"
    if detail:
        msg += f"  —  {detail}"
    print(msg)


# ── Capture plan generation at the service layer so we can log raw response ──

import os, importlib
sys.path.insert(0, str(Path(__file__).parent))

os.environ.setdefault("DATABASE_URL", "sqlite:///./knowledge_platform.db")

# Monkey-patch chat() to capture raw responses
from app import llm_client as _llm_mod
_original_chat = _llm_mod.chat
_captured: dict[str, str] = {}

def _capturing_chat(system: str, user: str, max_tokens: int = 8192,
                    operation: str = "", **kw):
    t0 = time.perf_counter()
    result = _original_chat(system=system, user=user, max_tokens=max_tokens,
                             operation=operation, **kw)
    elapsed = time.perf_counter() - t0
    key = operation or "unknown"
    _captured[key] = result
    _captured[f"{key}__elapsed"] = f"{elapsed:.1f}s"
    _captured[f"{key}__prompt_chars"] = str(len(system) + len(user))
    _captured[f"{key}__response_chars"] = str(len(result))
    print(f"\n  [LLM] op={operation}  elapsed={elapsed:.1f}s  "
          f"prompt={len(system)+len(user):,} chars (~{(len(system)+len(user))//4:,} tokens)  "
          f"response={len(result):,} chars (~{len(result)//4:,} tokens)")
    return result

_llm_mod.chat = _capturing_chat

# Also patch in plan_service and deliverable_service
from app.generation import plan_service as _ps
from app.generation import deliverable_service as _ds
_ps.chat = _capturing_chat
_ds.chat = _capturing_chat


# ── Database session ─────────────────────────────────────────────────────────

from app.db.session import SessionLocal


def run_type(dtype: str, focus: str | None = None) -> dict:
    """Run the full plan → generate PPTX workflow for one deliverable type."""
    result = {
        "type": dtype,
        "focus": focus,
        "stage_timings": {},
        "success": False,
        "plan_id": None,
        "slide_count": None,
        "pptx_kb": None,
        "errors": [],
        "raw_response_head": "",
        "raw_response_tail": "",
        "json_valid": False,
        "truncated": False,
    }

    bar(f"TYPE: {dtype.upper()}" + (f" — focus: {focus}" if focus else ""))

    # ── Stage 1: Generate plan ──────────────────────────────────────────────
    print("\n  Stage 1: Generate plan…")
    t0 = time.perf_counter()

    db = SessionLocal()
    try:
        from app.generation.plan_service import generate_plan
        try:
            plan_out = generate_plan(
                db=db,
                workspace_id=WS_ID,
                deliverable_type=dtype,
                focus_area=focus,
            )
            elapsed_plan = time.perf_counter() - t0
            result["stage_timings"]["plan_generate"] = f"{elapsed_plan:.1f}s"
            result["plan_id"] = plan_out.id
            result["slide_count"] = len(plan_out.slides)

            # Capture raw response
            raw = _captured.get("plan_blueprint", "")
            result["raw_response_head"] = raw[:2000]
            result["raw_response_tail"] = raw[-500:] if len(raw) > 2000 else ""

            # Detect truncation — strip fences first so ```json...``` is not a false positive
            from app.generation.deliverable_service import _strip_fences, _is_truncated
            raw_stripped_check = _strip_fences(raw)
            result["truncated"] = _is_truncated(raw_stripped_check)
            result["json_valid"] = True  # If generate_plan didn't raise, JSON was parsed

            check("Plan generated",       True,  f"id={plan_out.id}  slides={result['slide_count']}  elapsed={elapsed_plan:.1f}s")
            check("JSON parsed",          True,  f"response={len(raw):,} chars")
            check("Truncation check",     not result["truncated"],
                  "Response complete" if not result["truncated"] else "⚠ Truncated — continuation recovery fired")
            check("Slides populated",     result["slide_count"] > 0,  f"{result['slide_count']} slides")
            check("Governing messages",   len(plan_out.governing_messages) > 0,
                  f"{len(plan_out.governing_messages)} messages")

            # Check annotation fields on content slides
            content_slides = [s for s in plan_out.slides
                              if s.layout not in ("section_divider","cover","end_slide","sources","agenda")]
            grounded = [s for s in content_slides if s.graph_concepts or s.evidence]
            check("Annotation fields",    len(grounded) > 0,
                  f"{len(grounded)}/{len(content_slides)} content slides have grounding")

        except Exception as exc:
            elapsed_plan = time.perf_counter() - t0
            result["stage_timings"]["plan_generate"] = f"{elapsed_plan:.1f}s"
            result["errors"].append(f"Plan generation: {exc}")

            raw = _captured.get("plan_blueprint", "")
            result["raw_response_head"] = raw[:3000]
            result["raw_response_tail"] = raw[-1000:] if len(raw) > 3000 else ""
            result["truncated"] = bool(raw.strip() and not raw.strip().endswith("}"))

            check("Plan generated",  False, str(exc)[:200])
            print(f"\n  ── RAW CLAUDE RESPONSE (first 2000 chars) ──")
            print(raw[:2000])
            if len(raw) > 2000:
                print(f"\n  … [{len(raw)-2500} chars omitted] …\n")
                print(raw[-500:])
            print(f"  ── END RAW RESPONSE (len={len(raw)}) ──")
            db.close()
            return result
    finally:
        pass  # keep db open for approve step

    # ── Stage 2: Approve → generate PPTX ───────────────────────────────────
    print(f"\n  Stage 2: Approve plan {plan_out.id} → generate PPTX…")
    t1 = time.perf_counter()
    try:
        from app.generation.plan_service import approve_and_generate
        pptx_result = approve_and_generate(db=db, plan_id=plan_out.id)
        elapsed_pptx = time.perf_counter() - t1
        result["stage_timings"]["pptx_generate"] = f"{elapsed_pptx:.1f}s"
        result["pptx_kb"] = len(pptx_result.pptx_bytes) / 1024
        result["success"] = True

        check("PPTX generated",   True,  f"{result['pptx_kb']:.1f} KB  elapsed={elapsed_pptx:.1f}s")
        check("PPTX non-empty",   len(pptx_result.pptx_bytes) > 10_000,
              f"{len(pptx_result.pptx_bytes):,} bytes")
        check("Title set",        bool(pptx_result.deliverable.title),
              pptx_result.deliverable.title[:80])
        check("Sources set",      len(pptx_result.sources) > 0,
              f"{len(pptx_result.sources)} source(s)")

        # Write PPTX to /tmp so it can be verified
        out_path = f"/tmp/live_{dtype}.pptx"
        with open(out_path, "wb") as f:
            f.write(pptx_result.pptx_bytes)
        check("PPTX written to disk", True, out_path)

    except Exception as exc:
        elapsed_pptx = time.perf_counter() - t1
        result["stage_timings"]["pptx_generate"] = f"{elapsed_pptx:.1f}s"
        result["errors"].append(f"PPTX generation: {exc}")
        check("PPTX generated", False, str(exc)[:200])
    finally:
        db.close()

    return result


# ── Run all three types ───────────────────────────────────────────────────────

def main():
    print("\n" + "═"*60)
    print("  LIVE END-TO-END VALIDATION")
    print(f"  Workspace: {WS_ID} (Payment Modernization)")
    print(f"  Backend:   {BASE}")
    print("═"*60)

    # Verify workspace has knowledge
    data, status, _ = req("GET", f"/workspaces/{WS_ID}")
    if status != 200 or "error" in data:
        print(f"\n❌ Cannot reach workspace {WS_ID}: {data}")
        sys.exit(1)
    print(f"\n  Workspace:  {data['name']}")
    print(f"  Concepts:   {data['concept_count']}")
    print(f"  Relations:  {data['relationship_count']}")
    print(f"  Patterns:   {data['pattern_count']}")
    print(f"  Docs:       {data['document_count']}")

    if data["concept_count"] == 0:
        print("\n❌ No knowledge in workspace — cannot run live tests")
        sys.exit(1)

    results = []

    # Run each type
    for dtype, focus in [
        ("client_101", None),
        ("client_201", None),
        ("executive_summary", "Digital Assets and Payment Modernization"),
    ]:
        r = run_type(dtype, focus)
        results.append(r)
        # Clear captured responses between runs
        _captured.clear()

    # ── Summary ──────────────────────────────────────────────────────────────
    bar("SUMMARY", n=60)
    print()
    all_ok = True
    for r in results:
        ok = r["success"]
        all_ok = all_ok and ok
        sym = "✅" if ok else "❌"
        pptx_str = f"{r['pptx_kb']:.0f}KB" if r['pptx_kb'] else "—"
        plan_t   = r['stage_timings'].get('plan_generate', '—')
        pptx_t   = r['stage_timings'].get('pptx_generate', '—')
        print(f"  {sym}  {r['type']:20s}  "
              f"slides={str(r['slide_count'] or '-'):>3}  "
              f"pptx={pptx_str:>7}  "
              f"plan={plan_t:>7}  "
              f"render={pptx_t:>6}")
        for e in r["errors"]:
            print(f"       ⚠  {e[:120]}")

    print()
    if all_ok:
        print("  ✅  ALL THREE DELIVERABLE TYPES SUCCEEDED")
    else:
        print("  ❌  ONE OR MORE TYPES FAILED — see details above")
        sys.exit(1)


if __name__ == "__main__":
    main()
