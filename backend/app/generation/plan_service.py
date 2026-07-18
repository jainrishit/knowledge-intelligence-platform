"""
Presentation Plan service — implements the plan → review → approve → generate workflow.

This module exposes three public functions:

  generate_plan()
      Phase 1 (Blueprint) — single Claude call to generate the deck structure from the
      graph intelligence brief.  Phase 2 (Quality Review) has been removed: it was a
      second full LLM call (~60–120s) that doubled generation time with no user-visible
      benefit — the user reviews and revises the plan interactively.
      Returns a persisted PresentationPlan record whose blueprint_json holds the blueprint.

  revise_plan()
      Accepts a user instruction and the current blueprint, asks Claude to apply the
      revision, and updates the PresentationPlan record.  Can be called multiple times.

  approve_and_generate()
      Takes an approved PresentationPlan, runs the Python normaliser + PPTX generator,
      persists a Deliverable, and returns the PPTX bytes + metadata.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
import sys

from sqlalchemy.orm import Session

from app.db.models import (
    Concept, ConsultingPattern, Deliverable, Document, PresentationPlan,
    Relationship, Workspace,
)
from app.generation.deliverable_service import (
    DELIVERABLE_TYPES,
    _BLUEPRINT_SYSTEM_PROMPT,
    _build_blueprint_message,
    _build_graph_intelligence,
    _blueprint_to_deck_spec,
    _load_prompt,
    _parse_json,
    _retrieve_relevant_nodes,
    _validate_deck_spec,
)
from app.graph.memory_manager import graph_memory_manager
from app.llm_client import chat
from app.schemas import (
    DeliverablePptxResponse, DeliverableOut, PlanSlide, PlanSlidesUpdate,
    PresentationPlanOut, SourceRef,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Plan revision system prompt
# ─────────────────────────────────────────────────────────────────────────────

_REVISION_SYSTEM_PROMPT = """\
You are a principal management consultant at IBM Consulting acting as a presentation architect.

You have been given:
  1. A presentation blueprint (structured deck plan with slides, titles, and content)
  2. A user revision instruction

Your task: apply the instruction precisely and return a REVISED blueprint.

REVISION RULES:
  - Apply the instruction literally: add, remove, reorder, expand, or reduce as directed.
  - When adding slides: ground every new slide in the existing blueprint's concepts, relationships,
    or evidence.  Do not invent facts.
  - When removing slides: renumber slide_number fields sequentially from 1.
  - When expanding a section: add new slides after the existing slides in that section.
  - When reducing a section: merge the thinner slides; retain the most evidenced ones.
  - Preserve the blueprint's JSON schema exactly — same field names, same types.
  - Preserve governing_messages, storyline_summary, deliverable_type, and title unless the
    instruction explicitly asks you to change them.
  - After applying the revision, run the same quality checks as the original review:
      • Every slide title must be a takeaway statement (not a label).
      • Every bullet must be a complete grammatical sentence ending with a period.
      • No duplicate slides.
      • At least 40% of content slides should use a non-title_content layout.
  - Do NOT add filler slides that lack evidence support.
  - For every content slide (not section_divider, cover, end_slide), populate ALL five
   annotation fields so the user can see exactly what graph intelligence supports it:
     • "key_insights"       — 1–3 consulting insights this slide communicates (complete sentences);
                              these are the "so what" takeaways the user reviews before approving
     • "graph_concepts"     — concept names from the graph that this slide uses
     • "relationships_used" — relationships in "A -> B (type)" format
     • "patterns_used"      — consulting pattern names (empty list if none apply)
     • "evidence"           — source document names that back the claims on this slide
  - Newly added slides must have non-empty key_insights, graph_concepts and evidence lists.
    If you cannot populate graph_concepts and evidence for a new slide, that slide
    is ungrounded — do not add it.

OUTPUT FORMAT:
Return a JSON object with this structure:
{
  "revised_blueprint": { <complete revised blueprint — same schema as input> },
  "changes_summary": ["brief description of each change made"]
}

Return ONLY the JSON object.  No preamble.  No markdown fences.  Start with { end with }.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Defensive JSON generation — extract → fence-strip → repair → corrective retry
#                             → graceful fallback. Malformed LLM output can never
#                             become an HTTP 500.
# ─────────────────────────────────────────────────────────────────────────────

_JSON_CORRECTION_INSTRUCTION = (
    "\n\n---\n"
    "IMPORTANT: your previous reply could NOT be parsed as JSON. It contained prose, "
    "an explanation, or markdown fences. Reply again with ONLY the JSON object "
    "specified above — no preamble, no commentary, no markdown fences. The very first "
    "character of your reply must be '{' and the very last must be '}'."
)


def _generate_json_phase(
    system: str,
    user: str,
    *,
    phase: str,
    operation: str,
    max_tokens: int,
    temperature: float = 0.2,
) -> dict | None:
    """
    Call Claude expecting a JSON object, defensively. Returns the parsed dict, or
    None if valid JSON could not be obtained after one corrective retry.

    Recovery ladder (each step logged for diagnostics):
      1. chat() → _parse_json — which already strips markdown fences, extracts an
         embedded object from prose, and repairs JSON truncated by max_tokens.
      2. On failure: log the FULL raw reply, then retry ONCE with an explicit
         "JSON only" correction appended (temperature 0; a larger token budget if
         the first reply looked truncated — never smaller).
      3. Still unparseable → return None so the caller can degrade gracefully.

    This function NEVER raises on malformed output, so a narrative/garbled reply
    can never surface as an HTTP 500. An HTTP 503 from chat() (LLM unavailable) is
    allowed to propagate — that is a clean "try again", not a malformed-output crash.
    """
    def _attempt(u: str, mt: float, temp: float, op: str) -> tuple[dict | None, str]:
        raw = chat(system=system, user=u, max_tokens=int(mt), temperature=temp, operation=op)
        try:
            return _parse_json(raw, phase), raw
        except RuntimeError:
            return None, raw

    result, raw = _attempt(user, max_tokens, temperature, operation)
    if result is not None:
        return result

    logger.error(
        "[%s] LLM reply was not valid JSON (%d chars). Retrying once with correction. "
        "Raw reply follows:\n%s", phase, len(raw), raw[:6000],
    )
    looked_truncated = raw.count("{") > raw.count("}") or raw.count("[") > raw.count("]")
    retry_tokens = min(int(max_tokens * 1.5), 32000) if looked_truncated else max_tokens

    result, raw2 = _attempt(
        user + _JSON_CORRECTION_INSTRUCTION, retry_tokens, 0.0, operation + "_retry"
    )
    if result is not None:
        logger.info("[%s] Corrective retry produced valid JSON.", phase)
        return result

    logger.error(
        "[%s] Corrective retry still not valid JSON (%d chars) — caller will degrade "
        "gracefully (no 500). Raw reply follows:\n%s", phase, len(raw2), raw2[:6000],
    )
    return None


def _fallback_blueprint(
    db: Session,
    workspace_id: int,
    deliverable_type: str,
    concept_ids: list[int],
    source_refs: list,
) -> dict:
    """
    Build a valid, renderable blueprint DETERMINISTICALLY from the knowledge graph.

    Used only when the LLM cannot produce parseable JSON even after a corrective
    retry. The result is plainer than an AI-authored deck, but it is grounded in
    real workspace concepts and evidence, matches the blueprint schema, and lets
    the plan → approve workflow complete without a 500. It is transparently flagged
    in metadata.open_items so the plan is never silently degraded.
    """
    ws = db.get(Workspace, workspace_id)
    ws_name = ws.name if ws else f"Workspace {workspace_id}"
    type_label = DELIVERABLE_TYPES.get(deliverable_type, deliverable_type)

    concepts: list[Concept] = []
    if concept_ids:
        rows = {c.id: c for c in db.query(Concept).filter(Concept.id.in_(concept_ids)).all()}
        concepts = [rows[i] for i in concept_ids if i in rows]
    if not concepts:
        concepts = (
            db.query(Concept).filter(Concept.workspace_id == workspace_id).limit(40).all()
        )

    # Distinct source-document names for the evidence annotation.
    doc_names: list[str] = []
    seen_docs: set[int] = set()
    for c in concepts:
        if c.source_document_id and c.source_document_id not in seen_docs:
            seen_docs.add(c.source_document_id)
            doc = db.get(Document, c.source_document_id)
            if doc:
                doc_names.append(doc.title or doc.filename)
    if not doc_names:
        doc_names = [getattr(sr, "document_name", str(sr)) for sr in source_refs]

    def _one_sentence(text: str, fallback: str, limit: int = 150) -> str:
        t = " ".join((text or "").split()).strip() or fallback
        t = t.split(". ")[0].strip().rstrip(".")
        if len(t) > limit:  # cut at a word boundary — never emit a "..." fragment
            t = t[:limit].rsplit(" ", 1)[0].rstrip()
        return t + "."

    governing = [
        f"{c.name} is a central element of the {ws_name} knowledge base."
        for c in concepts[:3]
    ] or [f"{ws_name} knowledge base summary."]

    slides: list[dict] = [{
        "slide_number": 1,
        "layout": "large_text",
        "title": f"{type_label}: {ws_name}",
        "bullets": [],
        "key_insights": ["Frame the workspace and its most significant concepts."],
        "graph_concepts": [c.name for c in concepts[:3]],
        "evidence": doc_names[:3],
    }]

    group: list[Concept] = []
    def _flush(title: str):
        if not group:
            return
        slides.append({
            "slide_number": len(slides) + 1,
            "layout": "title_content",
            "title": title,
            "bullets": [_one_sentence(f"{c.name}: {c.description}", c.name) for c in group],
            "key_insights": ["Summarise workspace concepts and their supporting evidence."],
            "graph_concepts": [c.name for c in group],
            "evidence": doc_names[:3],
        })
        group.clear()

    for c in concepts:
        group.append(c)
        if len(group) >= 4:
            _flush("Key concepts grounded in the workspace evidence.")
    _flush("Additional concepts grounded in the workspace evidence.")

    return {
        "deliverable_type": deliverable_type,
        "title": f"{type_label}: {ws_name}",
        "governing_messages": governing,
        "storyline_summary": (
            "Deterministic fallback plan grounded directly in the workspace "
            "knowledge graph concepts and evidence."
        ),
        "slides": slides,
        "metadata": {
            "total_slides": len(slides),
            "source_documents": doc_names,
            "open_items": [
                "This plan was generated deterministically from the knowledge graph "
                "because the AI response could not be parsed. Regenerate for a fuller "
                "consulting narrative."
            ],
            "generation_notes": (
                "Fallback plan (AI JSON unavailable) — grounded in workspace "
                "concepts and evidence."
            ),
        },
    }


def _blueprint_from_plan(plan: PresentationPlan) -> dict:
    """Deserialise the blueprint JSON stored in the plan record."""
    try:
        return json.loads(plan.blueprint_json)
    except (json.JSONDecodeError, TypeError):
        return {}


def _plan_to_out(plan: PresentationPlan) -> PresentationPlanOut:
    """Convert a PresentationPlan ORM row to the Pydantic output schema."""
    blueprint = _blueprint_from_plan(plan)

    raw_slides = blueprint.get("slides") or []
    slides: list[PlanSlide] = []
    for s in raw_slides:
        try:
            slides.append(PlanSlide(**{k: v for k, v in s.items()}))
        except Exception:
            # Partial slide — include with defaults
            slides.append(PlanSlide(
                slide_number=s.get("slide_number", 0),
                title=s.get("title", "(untitled)"),
                layout=s.get("layout", "title_content"),
            ))

    return PresentationPlanOut(
        id=plan.id,
        workspace_id=plan.workspace_id,
        deliverable_type=plan.deliverable_type,
        focus_area=plan.focus_area,
        slides=slides,
        governing_messages=blueprint.get("governing_messages") or [],
        storyline_summary=blueprint.get("storyline_summary"),
        deck_title=blueprint.get("title"),
        revision_history=plan.revision_history or [],
        status=plan.status,
        deliverable_id=plan.deliverable_id,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public: generate_plan
# ─────────────────────────────────────────────────────────────────────────────

def generate_plan(
    db: Session,
    workspace_id: int,
    deliverable_type: str,
    focus_area: str | None = None,
) -> PresentationPlanOut:
    """
    Generate a presentation plan from the workspace knowledge graph.

    Single Claude call (Phase 1: Blueprint only).  Phase 2 (Quality Review) has been
    removed because:
      - It was a second full LLM call that doubled wall-clock time (60–120s extra).
      - The user reviews and revises the plan interactively — they are the quality gate.
      - The blueprint system prompt already enforces all quality rules in Phase 1.

    Does NOT generate a PPTX.  Returns the draft plan for user review.
    """
    _t_start = time.perf_counter()

    if deliverable_type not in DELIVERABLE_TYPES:
        raise ValueError(
            f"Unsupported deliverable type '{deliverable_type}'. "
            f"Must be one of: {list(DELIVERABLE_TYPES)}"
        )

    effective_focus = focus_area if deliverable_type == "executive_summary" else None

    # ── Step 1: Workspace + prompt load ──────────────────────────────────────
    _t = time.perf_counter()
    ws = db.get(Workspace, workspace_id)
    workspace_name = ws.name if ws else f"Workspace {workspace_id}"
    generation_prompt = _load_prompt(deliverable_type)
    logger.info("[plan ws=%d] step=workspace_load  elapsed=%.0fms",
                workspace_id, (time.perf_counter() - _t) * 1000)

    # ── Step 2: Graph load ────────────────────────────────────────────────────
    _t = time.perf_counter()
    G = graph_memory_manager.get_workspace_graph(workspace_id, db)
    logger.info("[plan ws=%d] step=graph_load  nodes=%d edges=%d  elapsed=%.0fms",
                workspace_id, G.number_of_nodes(), G.number_of_edges(),
                (time.perf_counter() - _t) * 1000)

    # ── Step 3: Relevant node retrieval ──────────────────────────────────────
    _t = time.perf_counter()
    relevant_node_ids = _retrieve_relevant_nodes(G, effective_focus)
    logger.info("[plan ws=%d] step=retrieve_nodes  count=%d  elapsed=%.0fms",
                workspace_id, len(relevant_node_ids), (time.perf_counter() - _t) * 1000)

    if not relevant_node_ids:
        raise ValueError(
            "No knowledge found in workspace. "
            "Upload and process documents first."
        )

    # ── Step 4: Graph intelligence brief ─────────────────────────────────────
    _t = time.perf_counter()
    graph_digest, source_refs, concept_ids, doc_ids = _build_graph_intelligence(
        G, db, workspace_id, relevant_node_ids, effective_focus
    )
    logger.info("[plan ws=%d] step=graph_intelligence  brief_chars=%d concepts=%d docs=%d  elapsed=%.0fms",
                workspace_id, len(graph_digest), len(concept_ids), len(doc_ids),
                (time.perf_counter() - _t) * 1000)

    # ── Step 5: DB metadata counts ───────────────────────────────────────────
    _t = time.perf_counter()
    concept_count = len(concept_ids)
    doc_count = len(doc_ids)
    pattern_count = db.query(ConsultingPattern).filter(
        ConsultingPattern.workspace_id == workspace_id
    ).count()
    relationship_count = db.query(Relationship).filter(
        Relationship.workspace_id == workspace_id
    ).count()
    logger.info("[plan ws=%d] step=db_counts  concepts=%d rels=%d patterns=%d  elapsed=%.0fms",
                workspace_id, concept_count, relationship_count, pattern_count,
                (time.perf_counter() - _t) * 1000)

    # ── Step 6: Build blueprint prompt ───────────────────────────────────────
    _t = time.perf_counter()
    blueprint_message = _build_blueprint_message(
        workspace_name=workspace_name,
        deliverable_type=deliverable_type,
        focus_area=effective_focus,
        generation_prompt=generation_prompt,
        graph_digest=graph_digest,
        concept_count=concept_count,
        doc_count=doc_count,
        pattern_count=pattern_count,
        relationship_count=relationship_count,
    )
    total_prompt_chars = len(blueprint_message) + len(_BLUEPRINT_SYSTEM_PROMPT)
    logger.info("[plan ws=%d] step=build_prompt  user_chars=%d system_chars=%d total_chars=%d (~%d tokens)  elapsed=%.0fms",
                workspace_id, len(blueprint_message), len(_BLUEPRINT_SYSTEM_PROMPT),
                total_prompt_chars, total_prompt_chars // 4,
                (time.perf_counter() - _t) * 1000)

    # ── Step 7: Claude call (Phase 1 — Blueprint) ─────────────────────────────
    # max_tokens=16000: live measurements show Client 101/201 blueprints with
    # full annotation fields reach ~38KB (~10,000 output tokens).  8192 caused
    # consistent truncation mid-JSON for those types.  16000 provides ~6000
    # token headroom.  Executive Summary (7-10 slides) stays well under 8192
    # but benefits from the same headroom.
    # ── Steps 7–8: Claude blueprint call + DEFENSIVE JSON parse ───────────────
    # Un-crashable path: extraction, fence-stripping, and truncation repair live
    # inside _parse_json; _generate_json_phase adds ONE corrective "JSON only"
    # retry on top. If the model STILL returns non-JSON, we fall back to a
    # deterministic graph-grounded blueprint. A malformed/narrative reply can
    # therefore never surface as an HTTP 500. (An HTTP 503 from chat() — LLM
    # unavailable — still propagates as a clean "try again", not a 500.)
    logger.info("[plan ws=%d type=%s] step=claude_call  phase=blueprint  max_tokens=16000",
                workspace_id, deliverable_type)
    _t = time.perf_counter()
    blueprint = _generate_json_phase(
        _BLUEPRINT_SYSTEM_PROMPT, blueprint_message,
        phase="plan_blueprint", operation="plan_blueprint", max_tokens=16000,
    )
    _claude_elapsed = time.perf_counter() - _t
    if blueprint is None:
        logger.error(
            "[plan ws=%d] blueprint JSON unparseable after corrective retry — "
            "using deterministic graph-grounded fallback (no 500).", workspace_id)
        blueprint = _fallback_blueprint(
            db, workspace_id, deliverable_type, concept_ids, source_refs
        )
    slide_count = len(blueprint.get("slides", []))
    logger.info("[plan ws=%d] step=claude_call+parse DONE  slides=%d  elapsed=%.1fs",
                workspace_id, slide_count, _claude_elapsed)

    # ── Step 9: Persist plan ──────────────────────────────────────────────────
    _t = time.perf_counter()
    plan = PresentationPlan(
        workspace_id=workspace_id,
        deliverable_type=deliverable_type,
        focus_area=effective_focus,
        blueprint_json=json.dumps(blueprint),
        revision_history=[],
        status="draft",
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    logger.info("[plan ws=%d] step=persist_plan  plan_id=%d  elapsed=%.0fms",
                workspace_id, plan.id, (time.perf_counter() - _t) * 1000)

    _total_elapsed = time.perf_counter() - _t_start
    logger.info(
        "[plan ws=%d] COMPLETE  plan_id=%d type=%s slides=%d  "
        "total_elapsed=%.1fs  claude_elapsed=%.1fs  overhead=%.0fms",
        workspace_id, plan.id, deliverable_type, slide_count,
        _total_elapsed, _claude_elapsed,
        (_total_elapsed - _claude_elapsed) * 1000,
    )
    return _plan_to_out(plan)


# ─────────────────────────────────────────────────────────────────────────────
# Public: update_plan_slides
# ─────────────────────────────────────────────────────────────────────────────

def update_plan_slides(
    db: Session,
    plan_id: int,
    body: PlanSlidesUpdate,
) -> PresentationPlanOut:
    """
    Persist the user's local slide edits (reorder / remove) to the database.

    This replaces the blueprint's slide array with the caller-supplied list,
    renumbering slide_number fields sequentially from 1.  All other blueprint
    fields (title, governing_messages, storyline_summary, metadata) are
    preserved unchanged.

    Must be called before revise_plan() or approve_and_generate() so that
    any local edits made in the frontend are not discarded when the server
    reads the blueprint.
    """
    plan = db.get(PresentationPlan, plan_id)
    if not plan:
        raise ValueError(f"Plan {plan_id} not found.")
    if plan.status == "approved":
        raise ValueError("Plan is already approved; create a new plan to start over.")

    blueprint = _blueprint_from_plan(plan)

    # Replace slide array with the caller-supplied list, renumbered from 1.
    new_slides = []
    for i, slide in enumerate(body.slides, start=1):
        s = slide.model_dump(exclude_none=False)
        s["slide_number"] = i
        new_slides.append(s)

    blueprint["slides"] = new_slides
    if "metadata" in blueprint:
        blueprint["metadata"]["total_slides"] = len(new_slides)

    plan.blueprint_json = json.dumps(blueprint)
    plan.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(plan)

    logger.info(
        "[plan %d] Slides updated: %d slides persisted by user edit",
        plan_id, len(new_slides),
    )
    return _plan_to_out(plan)


# ─────────────────────────────────────────────────────────────────────────────
# Public: revise_plan
# ─────────────────────────────────────────────────────────────────────────────

def revise_plan(
    db: Session,
    plan_id: int,
    instruction: str,
) -> PresentationPlanOut:
    """
    Apply a user revision instruction to an existing draft plan.

    Claude receives the current blueprint + instruction and returns a revised blueprint.
    The plan record is updated in place.  Revision history is appended.
    """
    plan = db.get(PresentationPlan, plan_id)
    if not plan:
        raise ValueError(f"Plan {plan_id} not found.")
    if plan.status == "approved":
        raise ValueError("Plan is already approved; create a new plan to start over.")

    current_blueprint = _blueprint_from_plan(plan)

    user_message = (
        f"CURRENT BLUEPRINT:\n{json.dumps(current_blueprint, indent=2)}\n\n"
        f"REVISION INSTRUCTION:\n{instruction}\n\n"
        "Return the revised blueprint JSON. Start with { and end with }."
    )

    logger.info("[plan %d] Revision requested: %.120s", plan_id, instruction)

    # Defensive JSON parse with a corrective retry (extraction + fence-strip +
    # repair are inside _parse_json). If the model still returns non-JSON, degrade
    # gracefully by keeping the current blueprint unchanged — a revision can safely
    # no-op, so malformed output never produces a 500.
    revision_result = _generate_json_phase(
        _REVISION_SYSTEM_PROMPT, user_message,
        phase="plan_revision", operation="plan_revision", max_tokens=8192,
    )
    if revision_result is None:
        logger.error(
            "[plan %d] revision JSON unparseable after corrective retry — keeping "
            "current blueprint unchanged (no 500).", plan_id)
        return _plan_to_out(plan)
    revised_blueprint = revision_result.get("revised_blueprint") or current_blueprint
    changes = revision_result.get("changes_summary", [])
    logger.info("[plan %d] Revision complete: %d changes", plan_id, len(changes))

    # Update plan record
    plan.blueprint_json = json.dumps(revised_blueprint)
    history = list(plan.revision_history or [])
    history.append({
        "instruction": instruction,
        "changes": changes,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    plan.revision_history = history
    plan.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(plan)

    return _plan_to_out(plan)


# ─────────────────────────────────────────────────────────────────────────────
# Public: approve_and_generate
# ─────────────────────────────────────────────────────────────────────────────

def approve_and_generate(
    db: Session,
    plan_id: int,
) -> DeliverablePptxResponse:
    """
    Approve a plan and generate the PPTX.

    Normalises the blueprint → deck_spec (zero LLM calls), then renders PPTX.
    Persists a Deliverable record and marks the plan as approved.
    """
    _root = str(Path(__file__).parent.parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    try:
        from deliverables.generators.powerpoint_generator import generate_pptx
    except (ImportError, ModuleNotFoundError) as _imp_err:
        raise RuntimeError(
            f"PowerPoint generator could not be loaded: {_imp_err}. "
            "Ensure the deliverables package is installed and the backend is started "
            "from the project root directory."
        ) from _imp_err

    plan = db.get(PresentationPlan, plan_id)
    if not plan:
        raise ValueError(f"Plan {plan_id} not found.")

    ws = db.get(Workspace, plan.workspace_id)
    workspace_name = ws.name if ws else f"Workspace {plan.workspace_id}"

    refined_blueprint = _blueprint_from_plan(plan)
    if not refined_blueprint:
        raise ValueError("Plan blueprint is empty — cannot generate PPTX.")

    # Rebuild source refs from graph (needed for deck spec metadata)
    G = graph_memory_manager.get_workspace_graph(plan.workspace_id, db)
    relevant_node_ids = _retrieve_relevant_nodes(G, plan.focus_area)
    if not relevant_node_ids:
        raise ValueError(
            "No knowledge found in workspace graph. "
            "The workspace may have been cleared since this plan was created. "
            "Please re-upload documents and generate a new plan."
        )
    _, source_refs, concept_ids, doc_ids = _build_graph_intelligence(
        G, db, plan.workspace_id, relevant_node_ids, plan.focus_area
    )

    # ── Normalise blueprint → deck_spec ──────────────────────────────────────
    deck_spec = _blueprint_to_deck_spec(refined_blueprint, plan.focus_area, source_refs)
    logger.info(
        "[plan %d] Normalised deck spec: %d slides", plan_id, len(deck_spec.get("slides", []))
    )

    # NOTE: Phase 4 (_review_deck_spec) is intentionally skipped here.
    # The user has already reviewed, revised, and explicitly approved this plan.
    # Running an LLM review pass after approval would silently alter or remove
    # slides that the user accepted — violating the plan → review → approve contract.
    # Validation (deterministic, no LLM) still runs to catch rendering issues.

    # ── Validation Layer (deterministic pre-render gate) ──────────────────────
    # Removes any remaining empty/placeholder slides, fixes bullet termination,
    # guards against overflow — no LLM calls.
    deck_spec = _validate_deck_spec(
        deck_spec,
        context=f"plan={plan_id} type={plan.deliverable_type}",
    )
    logger.info(
        "[plan %d] Validation complete: %d slides before render", plan_id, len(deck_spec.get("slides", []))
    )

    # ── Render PPTX ──────────────────────────────────────────────────────────
    try:
        pptx_bytes = generate_pptx(deck_spec, workspace_name)
    except Exception as exc:
        logger.error("[plan %d] PPTX generation failed: %s", plan_id, exc)
        raise RuntimeError(f"PowerPoint assembly failed: {exc}") from exc

    # ── Derive title ──────────────────────────────────────────────────────────
    type_label = DELIVERABLE_TYPES[plan.deliverable_type]
    title = deck_spec.get("title") or refined_blueprint.get("title") or (
        f"{type_label}: {plan.focus_area}" if plan.focus_area
        else f"{type_label}: {workspace_name}"
    )

    # ── Persist Deliverable ───────────────────────────────────────────────────
    deliverable = Deliverable(
        workspace_id=plan.workspace_id,
        type=plan.deliverable_type,
        title=title,
        content_markdown=None,
        source_concept_ids=concept_ids,
        source_document_ids=doc_ids,
    )
    db.add(deliverable)
    db.flush()  # get deliverable.id before commit

    # ── Mark plan approved ────────────────────────────────────────────────────
    plan.status = "approved"
    plan.deliverable_id = deliverable.id
    plan.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(deliverable)

    logger.info(
        "[plan %d] Approved → deliverable %d, %.1f KB PPTX",
        plan_id, deliverable.id, len(pptx_bytes) / 1024,
    )

    safe_title = title.replace(" ", "_")[:60]
    filename = f"{safe_title}.pptx"

    return DeliverablePptxResponse(
        deliverable=DeliverableOut.model_validate(deliverable),
        sources=source_refs,
        pptx_bytes=pptx_bytes,
        filename=filename,
    )
