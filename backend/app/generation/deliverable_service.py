"""
Deliverable generation service — Client Material Generator.

Architecture (two-phase + render):

  Phase 1 — BLUEPRINT
    Claude receives the workspace knowledge graph intelligence and a consulting
    methodology prompt, then produces a structured narrative blueprint: storyline,
    section plan, slide purposes, key messages, and visual recommendations.
    Output: blueprint dict.

  Phase 2 — QUALITY REVIEW
    Claude reviews the blueprint as a senior consultant would — checking
    storyline coherence, executive readability, slide quality, content balance,
    narrative flow, and knowledge graph coverage.  Issues are identified and
    fixes are applied to the blueprint before rendering begins.
    Output: refined blueprint dict.

  Normalisation — _blueprint_to_deck_spec()
    A thin Python function (zero LLM calls) normalises the refined blueprint
    into the exact JSON schema expected by the PowerPoint generator.
    Handles field aliasing, slide renumbering, focus_area injection, and
    source document population.  Replaces the former Phase 3 LLM call which
    was 100% pass-through with no content value (20–40 s wasted).

  Phase 3 (PPTX) — PowerPoint generator
    Renders the normalised spec to .pptx bytes using python-pptx and the IBM
    Asset Kit master template.

Knowledge graph intelligence (not raw text):
    The context fed to Claude in Phase 1 is a structured graph digest:
      - Top concepts ranked by degree centrality (most-connected nodes)
      - Strongest relationships (edge strength ≥ 0.7, with reasoning)
      - Consulting patterns and their problem/approach summaries
      - Per-document evidence summary
    This ensures Claude reasons from graph topology, not flat document dumps.

Prompts live at:
    backend/deliverables/prompts/{deliverable_type}.md
"""
from __future__ import annotations

import json
import logging
import re
import string
from functools import lru_cache
from pathlib import Path
from collections import Counter

import networkx as nx
from sqlalchemy.orm import Session

from app.db.models import Concept, ConsultingPattern, Deliverable, Document, Relationship, Workspace
from app.retrieval.qa_service import _extract_keywords_llm
from app.graph.memory_manager import graph_memory_manager
from app.graph.traversal import find_nodes_by_keywords, get_neighbourhood
from app.llm_client import chat
from app.schemas import DeliverableOut, DeliverablePptxResponse, SourceRef

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).parent.parent.parent / "deliverables" / "prompts"

DELIVERABLE_TYPES = {
    "client_101":        "Client 101",
    "client_201":        "Client 201",
    "executive_summary": "Executive Summary",
}


# ─────────────────────────────────────────────────────────────────────────────
# Prompt loading
# ─────────────────────────────────────────────────────────────────────────────

def _load_prompt(deliverable_type: str) -> str:
    """Load the generation prompt for the given deliverable type from disk."""
    prompt_path = PROMPTS_DIR / f"{deliverable_type}.md"
    if not prompt_path.exists():
        raise ValueError(
            f"No prompt file found for deliverable type '{deliverable_type}'. "
            f"Expected: {prompt_path}"
        )
    return prompt_path.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Graph intelligence extraction
# ─────────────────────────────────────────────────────────────────────────────

def _build_graph_intelligence(
    G: nx.DiGraph,
    db: Session,
    workspace_id: int,
    relevant_node_ids: set[int],
    focus_area: str | None,
) -> tuple[str, list[SourceRef], list[int], list[int]]:
    """
    Build a structured graph intelligence digest for Claude — not a flat text dump.

    Returns
    -------
    graph_digest : str
        A formatted, multi-section intelligence brief covering:
          § Top concepts ranked by graph centrality (most connected = most important)
          § Strongest relationships with type and reasoning
          § Consulting patterns linked to relevant concepts
          § Per-document evidence provenance
    source_refs  : list[SourceRef]
    concept_ids  : list[int]
    doc_ids      : list[int]
    """
    # ── 1. Rank relevant nodes by in-degree + out-degree (total connections) ─
    node_degree: dict[int, int] = {}
    for nid in relevant_node_ids:
        if G.has_node(nid):
            node_degree[nid] = G.in_degree(nid) + G.out_degree(nid)

    # Top 60 by degree — enough depth without context explosion
    top_nodes = sorted(node_degree, key=lambda n: node_degree[n], reverse=True)[:60]

    # ── 2. Pull concept records for top nodes ─────────────────────────────────
    concepts = db.query(Concept).filter(Concept.id.in_(top_nodes)).all()
    concept_by_id: dict[int, Concept] = {c.id: c for c in concepts}

    seen_doc_ids: set[int] = set()
    source_refs: list[SourceRef] = []
    concept_ids: list[int] = []
    doc_name_by_id: dict[int, str] = {}

    for c in concepts:
        concept_ids.append(c.id)
        if c.source_document_id:
            if c.source_document_id not in doc_name_by_id:
                doc = db.get(Document, c.source_document_id)
                doc_name_by_id[c.source_document_id] = (
                    doc.title or doc.filename if doc else "Unknown"
                )
            doc_name = doc_name_by_id[c.source_document_id]
            if c.source_document_id not in seen_doc_ids:
                seen_doc_ids.add(c.source_document_id)
                source_refs.append(SourceRef(
                    document_id=c.source_document_id,
                    document_name=doc_name,
                    excerpt=c.source_excerpt or "",
                ))

    # ── 3. Build §1: Top concepts section ────────────────────────────────────
    concept_lines: list[str] = []
    for nid in top_nodes:
        c = concept_by_id.get(nid)
        if not c:
            continue
        degree = node_degree[nid]
        doc_name = doc_name_by_id.get(c.source_document_id or -1, "Unknown")
        concept_lines.append(
            f"  [{c.type or 'General'}] {c.name} (connections: {degree}, "
            f"confidence: {c.confidence:.2f})\n"
            f"    {c.description or '(no description)'}\n"
            f"    Source: {doc_name}"
        )

    # ── 4. Build §2: Strongest relationships ─────────────────────────────────
    top_node_set = set(top_nodes)
    strong_edges: list[tuple[float, str]] = []
    for src, tgt, data in G.edges(data=True):
        if src not in top_node_set or tgt not in top_node_set:
            continue
        strength = float(data.get("strength", 0.7))
        if strength < 0.6:
            continue
        src_c = concept_by_id.get(src)
        tgt_c = concept_by_id.get(tgt)
        if not (src_c and tgt_c):
            continue
        rel_type = data.get("relationship_type", "related_to")
        reasoning = data.get("reasoning", "")
        edge_str = (
            f"  {src_c.name}  —[{rel_type}, strength={strength:.2f}]→  {tgt_c.name}"
        )
        if reasoning:
            edge_str += f"\n    Reasoning: {reasoning[:120]}"
        strong_edges.append((strength, edge_str))

    strong_edges.sort(reverse=True)
    relationship_lines = [line for _, line in strong_edges[:40]]

    # ── 5. Build §3: Consulting patterns ─────────────────────────────────────
    patterns = db.query(ConsultingPattern).filter(
        ConsultingPattern.workspace_id == workspace_id
    ).all()
    pattern_lines: list[str] = []
    for p in patterns:
        related_ids = set(p.related_concept_ids or [])
        if not related_ids.intersection(set(concept_ids)):
            continue
        steps = "; ".join((p.ibm_approach or [])[:4])
        pattern_lines.append(
            f"  Pattern: {p.name}\n"
            f"    Problem: {p.problem_statement or '(none)'}\n"
            f"    Approach: {steps or '(none)'}"
        )
        for did in (p.source_document_ids or []):
            did_int = int(did)
            if did_int not in seen_doc_ids:
                doc = db.get(Document, did_int)
                if doc:
                    seen_doc_ids.add(did_int)
                    source_refs.append(SourceRef(
                        document_id=did_int,
                        document_name=doc.title or doc.filename,
                        excerpt="(Pattern context)",
                    ))

    # ── 6. Build §4: Document evidence provenance ─────────────────────────────
    doc_concept_count: Counter[int] = Counter()
    for c in concepts:
        if c.source_document_id:
            doc_concept_count[c.source_document_id] += 1

    doc_lines: list[str] = []
    for doc_id, count in doc_concept_count.most_common(15):
        name = doc_name_by_id.get(doc_id, f"Document {doc_id}")
        doc_lines.append(f"  {name}: {count} concepts extracted")

    # ── 7. Assemble full digest ───────────────────────────────────────────────
    focus_note = (
        f"\nFocus Area: {focus_area}\n"
        f"(Graph traversal rooted at topic: '{focus_area}' — {len(relevant_node_ids)} nodes retrieved)\n"
        if focus_area
        else f"\nScope: Full workspace — {len(relevant_node_ids)} graph nodes included\n"
    )

    parts = [
        f"=== GRAPH INTELLIGENCE BRIEF ==={focus_note}",
        f"GRAPH STATISTICS\n"
        f"  Total nodes in workspace graph: {G.number_of_nodes()}\n"
        f"  Total edges in workspace graph: {G.number_of_edges()}\n"
        f"  Nodes included in this brief: {len(top_nodes)}\n"
        f"  Strong relationships (≥0.6): {len(strong_edges)}\n"
        f"  Consulting patterns: {len(pattern_lines)}",
    ]

    if concept_lines:
        parts.append(
            "§1 TOP CONCEPTS (ranked by graph connectivity — most central = most important)\n"
            + "\n".join(concept_lines)
        )

    if relationship_lines:
        parts.append(
            "§2 STRONGEST RELATIONSHIPS (strength ≥ 0.6, sorted by strength)\n"
            + "\n".join(relationship_lines)
        )

    if pattern_lines:
        parts.append(
            "§3 CONSULTING PATTERNS (recurring themes across workspace documents)\n"
            + "\n".join(pattern_lines)
        )

    if doc_lines:
        parts.append(
            "§4 SOURCE DOCUMENT EVIDENCE\n"
            + "\n".join(doc_lines)
        )

    return "\n\n".join(parts), source_refs, concept_ids, list(seen_doc_ids)


# Stopwords to ignore in local keyword extraction
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for",
    "with", "by", "from", "is", "are", "was", "were", "be", "been", "being",
    "that", "this", "these", "those", "as", "it", "its", "about",
})

# LLM keyword extraction is only called for long/complex focus areas.
# For short strings (≤60 chars / ≤6 tokens) we use a local tokeniser.
_LOCAL_EXTRACTION_THRESHOLD_CHARS  = 60
_LOCAL_EXTRACTION_THRESHOLD_TOKENS = 6


@lru_cache(maxsize=256)
def _extract_keywords_local(focus_area: str) -> list[str]:
    """
    Fast local keyword extractor — zero LLM calls, lru_cache for repeated calls.

    Tokenises by splitting on whitespace and punctuation, lowercases, drops
    stopwords and short tokens, returns up to 7 keywords.

    Used for short, single-concept focus areas such as "ISO 20022",
    "Cross-Border Payments", or "Settlement Modernization".
    """
    tokens = focus_area.translate(
        str.maketrans(string.punctuation, " " * len(string.punctuation))
    ).split()
    keywords = [
        t for t in tokens
        if len(t) >= 3 and t.lower() not in _STOPWORDS
    ]
    # Preserve original capitalisation for proper nouns; also include lowercase
    result: list[str] = []
    seen: set[str] = set()
    for kw in keywords[:7]:
        lw = kw.lower()
        if lw not in seen:
            seen.add(lw)
            result.append(kw)
    return result or [focus_area]


def _get_focus_keywords(focus_area: str) -> list[str]:
    """
    Choose between local extraction and LLM extraction based on input complexity.

    - Short inputs (≤60 chars or ≤6 words): use local tokeniser — instant, cached.
    - Long/complex inputs: delegate to LLM keyword extractor for higher accuracy.
    """
    words = focus_area.split()
    if (
        len(focus_area) <= _LOCAL_EXTRACTION_THRESHOLD_CHARS
        or len(words) <= _LOCAL_EXTRACTION_THRESHOLD_TOKENS
    ):
        return _extract_keywords_local(focus_area)
    return _extract_keywords_llm(focus_area)


def _retrieve_relevant_nodes(
    G: nx.DiGraph,
    focus_area: str | None,
) -> set[int]:
    """Return the set of graph node IDs relevant to the focus area (or all nodes).

    When focus_area is None (Client 101 / Client 201 — whole workspace), return
    all nodes immediately without BFS traversal.  The BFS is O(N²) on a full
    graph and produces the same result as returning all nodes directly.
    """
    if not focus_area:
        # Short-circuit: no focus — every node is relevant
        return set(G.nodes())

    keywords = _get_focus_keywords(focus_area)
    matched  = find_nodes_by_keywords(G, keywords) or list(G.nodes())

    relevant: set[int] = set()
    for nid in matched:
        nbr_nodes, _ = get_neighbourhood(G, nid, hops=2)
        relevant.update(nbr_nodes)

    return relevant or set(G.nodes())


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — Blueprint system prompt
# ─────────────────────────────────────────────────────────────────────────────

_BLUEPRINT_SYSTEM_PROMPT = """\
You are a principal management consultant at IBM Consulting.

Your task is to analyse a workspace knowledge graph and produce a structured
PRESENTATION BLUEPRINT for a client-ready consulting deck.

The blueprint is NOT the final slide spec.  It is the consulting intelligence layer —
the storyline, narrative architecture, and content strategy that will guide the final
presentation.  A separate rendering step converts the blueprint into slides.

YOUR RESPONSIBILITIES IN THIS PHASE:
  ✓ Understand the workspace documents, concepts, relationships, and patterns
  ✓ Identify the 3–5 governing messages that define the narrative
  ✓ Determine the logical section structure and slide sequence
  ✓ Assign a clear purpose to every slide
  ✓ Write executive-quality slide titles (takeaway-based, not label-based)
  ✓ Write concise supporting bullets (complete sentences — not fragments)
  ✓ Choose structured/visual layouts over bullets wherever the content permits
  ✓ Ensure progressive disclosure — simple → deep, context → insight → action
  ✓ Ensure every claim names at least one concept, relationship, or pattern from the graph

ABSOLUTE PROHIBITIONS — violating any of these means the slide must be removed or merged:
  ✗ Invent facts, metrics, names, or relationships not in the graph intelligence
  ✗ Pad with generic consulting boilerplate not grounded in the workspace
  ✗ Create slides without a clear purpose
  ✗ Include slides whose title is a category label ("Key Challenges", "Overview",
    "Strategic Priorities", "Introduction", "Background", "Conclusion") — titles must
    be takeaway statements that carry the message without reading the bullets
  ✗ Include slides with fewer than 3 bullets (or 3 box/column items) unless the layout
    is large_text, section_divider, data_2_callouts, or callout_stat
  ✗ Include slides that do not name at least one concept, entity, or finding from
    the Graph Intelligence Brief — floating, ungrounded slides are not permitted
  ✗ Repeat the same finding on two slides — merge or drop the weaker one
  ✗ Fragment a single coherent point into two thin slides — use one well-structured slide
  ✗ Default every slide to title_content — choose the best layout for the content type
  ✗ Inflate slide count to appear thorough — fewer high-quality slides beat more weak ones

VISUAL-FIRST MANDATE:
  At least 40% of content slides must use a non-title_content layout.
  Content slides = all slides except section_divider, large_text, cover, sources, end_slide.

  Before assigning title_content to any slide, ask: can this be better expressed as:
    - a four_boxes_wide (4 parallel findings, priorities, recommendations)?
    - a four_boxes_stacked (4 risk areas, dimensions, domains in a 2×2 grid)?
    - a six_boxes (5–6 capabilities, components, initiatives)?
    - a four_column (4 independent pillars/workstreams — no column heads needed)?
    - a four_column_headlines (3 parallel pillars, each with a named heading)?
    - a two_col_dividers (current-state vs target-state, or any two-track split)?
    - a data_2_callouts (exactly 2 headline metrics with context — use "stats" field)?
    - a callout_stat (2–3 concise metrics, lighter than data_2_callouts)?
    - a large_text (the single most important insight in the whole deck)?
    - a two_column (6–10 parallel items split into two equal columns)?

  Use large_text exactly ONCE per deck — for the single governing insight.
  If the content is 4 items: prefer four_boxes_wide or four_boxes_stacked over title_content.
  If the content is 5–6 items: prefer six_boxes over title_content.
  If the content is a comparison or split analysis: prefer two_col_dividers over title_content.
  If the content is metrics: prefer data_2_callouts or callout_stat over title_content.

SLIDE COUNT PRINCIPLE — STORY-FIRST:
  The deck size is determined by the STORY, not by the data volume.
  The user message includes a calibrated TARGET and RANGE for content slides.
  Content slides = all slides except: cover, section_divider, sources, end_slide.

  The TARGET is calculated from workspace richness. Treat it as a firm recommendation.
  The RANGE is a guardrail: hard floor and hard ceiling — do not cross either bound.

  Justification rules:
    - Every slide below target: that's fine if the story is told.
    - Every slide AT target: ideal.
    - Every slide ABOVE target: must justify with a distinct, non-redundant purpose.
    - Content above the MAX: not permitted.

  Filler patterns that will be removed in Phase 2 review — avoid generating them:
    ✗ Agenda slide that lists sections already visible from section dividers
    ✗ Two slides covering the same concept with different wording
    ✗ A "Summary so far" slide mid-deck
    ✗ A section opener that restates the section divider title
    ✗ "Thank you / Questions" slide — end_slide handles this automatically
    ✗ Splitting 4 bullets into two slides

SLIDE TITLE QUALITY RULE:
  Every slide title must be a TAKEAWAY — it states the conclusion, not the topic.
  Bad:  "Key Challenges"       Good: "Legacy infrastructure blocks 60% of digital initiatives"
  Bad:  "Strategic Priorities" Good: "Three capability investments unlock the largest opportunity"
  Bad:  "Overview"             Good: "Ripple bridges the gap between legacy rails and real-time settlement"
  Bad:  "Background"           Good: "Regulatory pressure and cost structure are forcing payments modernisation"

  If you cannot write a takeaway title for a slide, that is a signal the slide has no purpose
  — merge or drop it.

BULLET QUALITY RULES — ENFORCED:
  EVERY bullet in the deck must satisfy ALL of the following:
    ✓ Is a COMPLETE grammatical sentence (subject + verb + object/complement)
    ✓ Ends with a full stop (period) — never with "..." or "…" or a dangling clause
    ✓ Is 20–100 characters long — split longer sentences into two separate bullets
    ✓ Leads with the insight or action (active voice)
    ✓ Names at least one entity, concept, finding, or metric from the graph
    ✓ Does NOT start with a vague opener: "This shows...", "It is important...",
      "There are several...", "Various factors...", "Many organizations..."

  Examples of BANNED bullets (fragment / vague / truncated):
    ✗ "Legacy system constraints"
    ✗ "Multiple stakeholders involved..."
    ✗ "Various payment modernisation initiatives underway"
    ✗ "Key challenge is infrastructure"
    ✗ "Ripple provides real-time..."

  Examples of REQUIRED bullet quality:
    ✓ "ISO 20022 adoption drives richer data exchange across all payment corridors."
    ✓ "Ripple's On-Demand Liquidity removes the pre-funding requirement for cross-border transfers."
    ✓ "SWIFT gpi reduces settlement time from days to under two hours for participating banks."

SLIDE WORTHINESS GATE — run this check on EVERY content slide before including it:
  A content slide earns its place only if it passes ALL FOUR tests:

  TEST 1 — EVIDENCE: does the slide title or at least one bullet name a specific concept,
    entity, relationship, or pattern from the Graph Intelligence Brief?
    FAIL → the slide is ungrounded; ground it or drop it.

  TEST 2 — SUBSTANCE: does the slide have at least 3 complete bullets (or ≥3 box/column items)?
    Exception: large_text, section_divider, data_2_callouts, callout_stat are exempt.
    FAIL → merge with an adjacent slide or add grounded bullets to reach 3.

  TEST 3 — TAKEAWAY TITLE: is the slide title a takeaway statement (not a label)?
    FAIL → rewrite the title as a conclusion before including the slide.

  TEST 4 — UNIQUENESS: does this slide communicate something not already covered by another
    slide in the deck?
    FAIL → merge the unique bullets into the slide that covers the nearest topic, then drop this one.

  Any slide that fails ANY test is either fixed or removed before the output is returned.
  DO NOT include failing slides in the output — they will be detected in Phase 2 and removed.

KNOWLEDGE GRAPH UTILISATION — mandatory:
  Do not produce generic consulting content that could apply to any client.
  The Graph Intelligence Brief contains ranked concepts, strong relationships, and patterns.
  EVERY section of the deck must use the graph's specific findings:
    - Name specific concepts (by their exact name in the Graph Intelligence Brief)
    - Reference specific relationships (e.g., "X depends on Y", "A enables B")
    - Surface specific patterns by name
    - Cite specific evidence from the document evidence section
  Slides that could have been written without looking at the Graph Intelligence Brief
  are not acceptable.

LAYOUT REFERENCE — exact field requirements per layout type:
  "title_content"         → "title" + "bullets": [3–7 complete sentence strings]
  "callout_stat"          → "title" + "bullets": [2–3 metric/stat lines]
  "two_column"            → "title" + "bullets": flat list (split evenly left/right)
  "two_col_dividers"      → "title" + "col_heads":["Left","Right"] + "columns":[left_bullets, right_bullets]
  "four_column"           → "title" + "columns":[[col1],[col2],[col3],[col4]]
  "four_column_headlines" → "title" + "col_heads":["H1","H2","H3"] + "columns":[[c1],[c2],[c3]]
  "four_boxes_wide"       → "title" + "boxes":["box1","box2","box3","box4"]
  "four_boxes_stacked"    → "title" + "boxes":["box1","box2","box3","box4"]
  "six_boxes"             → "title" + "boxes":["b1","b2","b3","b4","b5","b6"]
  "data_2_callouts"       → "title" + "stats":[{"label":"METRIC","body":"context"},{"label":"METRIC","body":"context"}]
  "large_text"            → "title" (the full statement — this IS the slide content)
  "section_divider"       → "title" (section label — short, 2–5 words)
  "agenda"                → "title" + "bullets": [section items as flat list]

BLUEPRINT OUTPUT FORMAT:
Return a JSON object with this structure:

{
  "deliverable_type": "client_101" | "client_201" | "executive_summary",
  "title": "deck title — specific to the workspace subject, not generic",
  "governing_messages": ["message 1 — complete insight sentence", "message 2", "message 3"],
  "storyline_summary": "2-3 sentence description of the narrative arc",
  "slides": [
    {
      "slide_number": 1,
      "section": "section name this slide belongs to",
      "purpose": "what this slide communicates — one complete sentence",
      "title": "TAKEAWAY title — states the conclusion, never a category label",
      "layout": "title_content | two_column | two_col_dividers | four_column | four_column_headlines | four_boxes_wide | four_boxes_stacked | six_boxes | large_text | callout_stat | data_2_callouts | section_divider | agenda",
      "bullets": ["Complete grammatical sentence.", "Complete grammatical sentence."],
      "columns": [[...], [...], [...], [...]],
      "col_heads": ["head1", "head2"],
      "boxes": ["Concise box content.", "Concise box content."],
      "stats": [{"label": "METRIC", "body": "supporting context sentence"}],
      "notes": "speaker notes — expand on bullets, add context the presenter needs",
      "visual_recommendation": "description of a diagram or visual that would strengthen this slide"
    }
  ],
  "metadata": {
    "total_slides": <actual count>,
    "source_documents": ["doc name 1", ...],
    "open_items": ["gap or assumption that needs verification"],
    "generation_notes": "bottom-line summary: governing insight + narrative arc"
  }
}

Return ONLY the JSON object.  No preamble.  No markdown fences.  Start with { and end with }.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Quality review system prompt
# ─────────────────────────────────────────────────────────────────────────────

_REVIEW_SYSTEM_PROMPT = """\
You are a senior IBM Consulting partner doing a pre-production quality gate on a
presentation blueprint before it goes to PowerPoint rendering.

You are NOT a passive reviewer — you are an EDITOR.  Every issue you identify must be
FIXED in the refined_blueprint before you return it.  Do not describe problems and leave
them unfixed.  Do not merely flag issues in issues_found while leaving the blueprint unchanged.

YOUR MANDATE: return a refined_blueprint that is immediately renderable as a high-quality,
client-ready consulting presentation.  The refined_blueprint must be strictly better than
the input.  If the input was already high quality, your job is smaller but still real.

═══════════════════════════════════════════════════════════════════════
PASS 1 — TITLE AUDIT  (run on EVERY slide)
═══════════════════════════════════════════════════════════════════════

For every slide in the blueprint, check: is the title a TAKEAWAY STATEMENT?

A takeaway title:
  ✓ States a finding, conclusion, or recommendation
  ✓ Can be read on its own and understood without the bullets
  ✓ Is specific to this workspace — references a named concept, theme, or entity

A label title:
  ✗ Names a category: "Overview", "Key Challenges", "Strategic Priorities",
    "Introduction", "Background", "Current State", "Conclusion", "Summary",
    "Next Steps", "Recommendations", "Opportunities", "Risks"
  ✗ Is a question without an answer: "What are the challenges?"
  ✗ Is generic enough to appear in any deck for any client

FIX RULE: If the title is a label, rewrite it as a takeaway.  Apply the rewrite directly
to the slide's title field in refined_blueprint.  Record each rewrite in fixes_applied.

═══════════════════════════════════════════════════════════════════════
PASS 2 — BULLET QUALITY AUDIT  (run on EVERY bullet in EVERY slide)
═══════════════════════════════════════════════════════════════════════

Each bullet must satisfy ALL of the following.  For each violation, FIX it in place:

  ✓ COMPLETE SENTENCE: has a subject, a verb, and completes a thought.
    FAIL → complete the sentence; do not delete the bullet.
    Example fix: "Legacy system constraints" → "Legacy core banking systems lack the APIs
    needed to support real-time payment instruction enrichment."

  ✓ ENDS WITH A PERIOD: never ends with "..." or "…" or a hanging clause.
    FAIL → complete the sentence and add a period.

  ✓ LENGTH 20–100 chars: longer bullets must be split into two separate bullets.
    FAIL → split at the natural clause boundary.

  ✓ SPECIFIC: names at least one entity, concept, metric, or finding from the workspace.
    FAIL → add the specific reference, or merge the bullet into a slide that has context.

  ✓ NOT VAGUE: does not open with "This shows...", "It is important...", "There are...",
    "Various...", "Multiple...", "Many organizations...", "Key aspects include..."
    FAIL → rewrite to lead with the specific insight.

═══════════════════════════════════════════════════════════════════════
PASS 3 — SLIDE WORTHINESS GATE  (run on EVERY content slide)
═══════════════════════════════════════════════════════════════════════

Content slides = layout NOT IN (section_divider, agenda, end_slide).

For each content slide, apply the four-test gate.  FAILING slides are REMOVED or MERGED:

  TEST 1 — EVIDENCE: does the slide title or any bullet name a specific concept, entity,
    relationship, or pattern from the workspace?
    FAIL → merge its bullets into the thematically nearest slide, then DELETE this slide.

  TEST 2 — SUBSTANCE: does the slide have ≥3 bullets (or ≥3 box/column items)?
    Exempt: large_text, data_2_callouts, callout_stat.
    FAIL → add grounded bullets to reach 3, OR merge into an adjacent slide, then DELETE.

  TEST 3 — TAKEAWAY TITLE: is the title a takeaway (already fixed in Pass 1)?
    If after Pass 1 the title is still a label (you could not write a takeaway), the slide
    has no purpose → DELETE it and redistribute any useful bullets.

  TEST 4 — UNIQUENESS: does another slide in the deck already cover this topic?
    FAIL → merge the unique points into the existing slide, then DELETE this duplicate.

  For each deletion, record: "DELETED slide [N] '[title]' — [reason]" in fixes_applied.
  For each merge, record: "MERGED slide [N] '[title]' into slide [M] '[title]'" in fixes_applied.

═══════════════════════════════════════════════════════════════════════
PASS 4 — VISUAL LAYOUT REBALANCING
═══════════════════════════════════════════════════════════════════════

Count how many content slides use title_content.  If more than 60% of content slides use
title_content, convert some slides to better layouts:

  - If a slide has exactly 4 bullets/items → convert to four_boxes_wide or four_boxes_stacked.
    Set "boxes" field from the bullets.  Remove the "bullets" field.
  - If a slide has 5–6 bullets/items → convert to six_boxes.
    Set "boxes" field.  Remove the "bullets" field.
  - If a slide presents a comparison, current/target, as-is/to-be, or two-track analysis →
    convert to two_col_dividers.  Set "col_heads" and "columns" fields.
  - If a slide presents 4 equal parallel pillars/workstreams → convert to four_column.
    Set "columns" field from bullets split into 4 groups.
  - If the most important insight slide is not already large_text → convert it.
    Set the title to the full insight statement.

  Target: no more than 60% of content slides should use title_content.
  Record each layout conversion in fixes_applied: "CONVERTED slide [N] from title_content to [layout]".

═══════════════════════════════════════════════════════════════════════
PASS 5 — DECK SIZE CHECK
═══════════════════════════════════════════════════════════════════════

Content slides = layout NOT IN (section_divider, agenda, end_slide).
Count them after Passes 1–4.

Type defaults (apply if the blueprint does not carry its own calibrated range):
  executive_summary: 4–10 content slides
  client_101:        8–18 content slides
  client_201:        12–22 content slides

CHECK A — Too long (count > max_for_type):
  Remove the weakest remaining slides — those with the lowest narrative value:
    Priority removal order: duplicate topics, label-only titles (already fixed but thin),
    section openers that add nothing beyond the section divider, agenda slides.
  Record each removal in fixes_applied.

CHECK B — Too short (count < min_for_type):
  Add at most ONE new slide for the most important topic not yet covered.
  The new slide must be grounded in the graph intelligence.  Do not pad.

CHECK C — Bloated sections:
  Remove or merge slides where 3+ consecutive slides make the same point.
  Remove section_divider slides that have no content slides following them.

═══════════════════════════════════════════════════════════════════════
PASS 6 — NARRATIVE COHERENCE CHECK
═══════════════════════════════════════════════════════════════════════

After all removals and merges, verify:
  - The deck still tells a coherent beginning → middle → end story.
  - No section is left without at least one content slide.
  - The most important governing message appears in the first third of the deck.
  - The deck ends with a clear implication, recommendation, or next steps slide.
  If any of these are broken by earlier passes, add minimal bridging content.

OUTPUT FORMAT:
Return a JSON object with this structure:

{
  "quality_score": <integer 1-10, score the REFINED output — not the input>,
  "content_slide_count": <integer — count after all fixes>,
  "deck_size_verdict": "appropriate" | "too_long" | "too_short",
  "visual_layout_pct_non_title_content": <percentage of content slides NOT using title_content>,
  "strengths": ["specific strength of the refined deck"],
  "issues_found": ["specific issue that was identified and fixed"],
  "fixes_applied": [
    "FIXED TITLE slide 3: 'Key Challenges' → 'Three infrastructure gaps block real-time settlement'",
    "COMPLETED BULLET slide 5 bullet 2: 'Ripple provides real-time...' → 'Ripple provides real-time liquidity...'",
    "DELETED slide 7 'Background' — duplicate of slide 2; bullets merged into slide 2",
    "CONVERTED slide 9 from title_content to four_boxes_wide — 4 strategic priorities"
  ],
  "refined_blueprint": { <the complete refined blueprint — same schema as input> }
}

ABSOLUTE RULES:
  - refined_blueprint must use the SAME JSON schema as the input blueprint.
  - Do NOT change the deliverable_type or overall topic.
  - Do NOT invent new facts — only improve presentation quality using existing graph content.
  - Completing a truncated bullet is allowed.  Inventing a new bullet from nothing is not.
  - Every fix in fixes_applied must be reflected in the actual refined_blueprint — no phantom fixes.
  - The refined_blueprint must be complete: all slides, all fields, no truncation.
  - Return ONLY the JSON object.  No preamble.  No markdown fences.  Start with { end with }.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Normaliser — replaces Phase 3 LLM call with a pure-Python transform
# ─────────────────────────────────────────────────────────────────────────────

def _blueprint_to_deck_spec(
    blueprint: dict,
    focus_area: str | None,
    source_refs: list,
) -> dict:
    """
    Convert a reviewed blueprint into the PowerPoint generator's deck_spec schema.

    This is a zero-LLM-call normaliser — it only:
      • Copies all slides as-is (blueprint and deck_spec share the same schema)
      • Injects focus_area at the top level
      • Renumbers slide_number sequentially from 1
      • Ensures metadata.source_documents is populated from source_refs if absent
      • Strips internal-only fields (purpose, section, visual_recommendation)
        that the PowerPoint generator does not use

    No content is changed here.  All storytelling decisions were made in Phase 1+2.
    """
    slides_in  = blueprint.get("slides") or []
    metadata   = dict(blueprint.get("metadata") or {})

    # Populate source_documents from source_refs if the LLM left it empty
    if not metadata.get("source_documents") and source_refs:
        metadata["source_documents"] = [
            getattr(sr, "document_name", str(sr)) for sr in source_refs
        ]

    # Normalise slides: renumber, strip blueprint-only keys
    _BLUEPRINT_ONLY_KEYS = {"purpose", "section", "visual_recommendation"}
    slides_out: list[dict] = []
    for i, slide in enumerate(slides_in, start=1):
        s = {k: v for k, v in slide.items() if k not in _BLUEPRINT_ONLY_KEYS}
        s["slide_number"] = i
        slides_out.append(s)

    metadata["total_slides"] = len(slides_out)

    return {
        "deliverable_type": blueprint.get("deliverable_type", ""),
        "title":            blueprint.get("title", ""),
        "focus_area":       focus_area or "",
        "slides":           slides_out,
        "metadata":         metadata,
    }


# ─────────────────────────────────────────────────────────────────────────────
# JSON parsing
# ─────────────────────────────────────────────────────────────────────────────

def _parse_json(raw: str, phase: str) -> dict:
    """
    Robustly extract and parse JSON from an LLM response.
    Handles clean JSON, fenced JSON, and JSON embedded in prose.
    """
    text = raw.strip()

    # Strip markdown code fences
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Extract first top-level JSON object from prose
    brace_start = text.find("{")
    if brace_start != -1:
        depth = 0
        for i, ch in enumerate(text[brace_start:], start=brace_start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[brace_start: i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break

    logger.error("[%s] LLM returned non-JSON. First 400 chars: %.400s", phase, raw)
    raise RuntimeError(
        f"Phase '{phase}': the AI returned a narrative response instead of JSON. "
        "Please try again."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Build per-phase user messages
# ─────────────────────────────────────────────────────────────────────────────

# ── Slide count calibration ───────────────────────────────────────────────────

# Base target slides per deliverable type — content-slide count (excludes cover,
# section dividers, sources, end slide which are structural).
_BASE_TARGETS: dict[str, tuple[int, int, int]] = {
    #                              min  target  max
    "executive_summary":          (  4,      7,  10),
    "client_101":                 (  8,     12,  18),
    "client_201":                 ( 12,     16,  22),
}

def _compute_target_range(
    deliverable_type: str,
    concept_count: int,
    doc_count: int,
    pattern_count: int,
    relationship_count: int,
) -> tuple[int, int, int, str]:
    """
    Compute a calibrated (min, target, max) content-slide range based on workspace
    richness and deliverable type.

    Returns (min_slides, target_slides, max_slides, rationale_string).

    The target is a firm recommendation; min and max are guardrail bounds.
    Content slides = all slides except cover, section dividers, sources, end slide.
    """
    base_min, base_target, base_max = _BASE_TARGETS.get(
        deliverable_type, (8, 12, 20)
    )

    # ── Richness score: 0.0 → sparse, 1.0 → rich ─────────────────────────────
    # Each signal contributes independently; capped to avoid runaway inflation.
    doc_score       = min(doc_count       / 20,  1.0)   # saturates at 20 docs
    concept_score   = min(concept_count   / 80,  1.0)   # saturates at 80 concepts
    pattern_score   = min(pattern_count   / 8,   1.0)   # saturates at 8 patterns
    rel_score       = min(relationship_count / 100, 1.0) # saturates at 100 rels

    richness = (doc_score * 0.30 + concept_score * 0.35 +
                pattern_score * 0.20 + rel_score * 0.15)

    # ── Scale the range proportionally with richness ──────────────────────────
    spread       = base_max - base_min
    target       = round(base_min + spread * richness)
    target       = max(base_min, min(target, base_max))
    adj_min      = max(base_min, target - 2)
    adj_max      = min(base_max, target + 3)

    # ── Human-readable rationale for the LLM prompt ──────────────────────────
    if richness < 0.25:
        tier = "sparse workspace (few documents, concepts, and patterns)"
    elif richness < 0.55:
        tier = "moderate workspace"
    elif richness < 0.80:
        tier = "rich workspace"
    else:
        tier = "very rich workspace with extensive knowledge"

    rationale = (
        f"{tier}: {doc_count} doc(s), {concept_count} concepts, "
        f"{relationship_count} relationships, {pattern_count} pattern(s)"
    )
    return adj_min, target, adj_max, rationale


def _build_blueprint_message(
    workspace_name: str,
    deliverable_type: str,
    focus_area: str | None,
    generation_prompt: str,
    graph_digest: str,
    concept_count: int,
    doc_count: int,
    pattern_count: int,
    relationship_count: int = 0,
) -> str:
    """Construct the user message for Phase 1 (blueprint generation)."""
    min_s, target_s, max_s, rationale = _compute_target_range(
        deliverable_type, concept_count, doc_count, pattern_count, relationship_count
    )

    slide_guidance = (
        f"SLIDE COUNT GUIDANCE (content slides only — excludes cover, section dividers, sources, end):\n"
        f"  Target: {target_s} content slides (calibrated for this workspace)\n"
        f"  Acceptable range: {min_s}–{max_s} content slides\n"
        f"  Basis: {rationale}\n"
        f"  Hard ceiling: {max_s} content slides — do NOT exceed this.\n"
        f"  Hard floor: {min_s} content slides — do NOT go below this.\n"
        f"  IMPORTANT: aim for the TARGET ({target_s}) unless the story genuinely demands\n"
        f"  more or less. Every slide above the target needs a clear justification."
    )

    focus_line = (
        f"Focus Area: {focus_area}" if focus_area
        else "Scope: Entire workspace — cover all available knowledge"
    )
    return (
        f"Workspace: {workspace_name}\n"
        f"{focus_line}\n\n"
        f"{slide_guidance}\n\n"
        f"=== DELIVERABLE METHODOLOGY ===\n{generation_prompt}\n\n"
        f"{graph_digest}\n\n"
        f"=== TASK ===\n"
        f"Using the methodology above and the graph intelligence brief, produce the "
        f"presentation blueprint JSON now. Ensure:\n"
        f"  - Governing messages are tight and evidence-grounded\n"
        f"  - Every slide title is a takeaway statement\n"
        f"  - Every slide has a stated 'purpose' field — no purpose, no slide\n"
        f"  - Content slide count is {min_s}–{max_s} (target {target_s})\n"
        f"  - At least 30% of content slides use a non-title_content layout\n"
        f"  - large_text is used for the single most important insight\n"
        f"  - section_divider opens every major section\n"
        f"  - All bullets are complete sentences (never end with ...)\n"
        f"Output ONLY the JSON object. Start with {{ and end with }}."
    )


def _build_review_message(blueprint: dict) -> str:
    """Construct the user message for Phase 2 (quality review)."""
    return (
        "Review the following presentation blueprint and return the refined version.\n\n"
        "BLUEPRINT TO REVIEW:\n"
        + json.dumps(blueprint, indent=2)
        + "\n\nReturn the review result JSON (quality_score, strengths, issues_found, "
        "fixes_applied, refined_blueprint). Start with { and end with }."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main generation entry point
# ─────────────────────────────────────────────────────────────────────────────

def generate_client_material(
    db: Session,
    workspace_id: int,
    deliverable_type: str,
    focus_area: str | None = None,
) -> DeliverablePptxResponse:
    """
    Generate a client material PowerPoint via two Claude phases + Python normaliser.

    Phase 1: Blueprint     — Claude builds consulting storyline from graph intelligence
    Phase 2: Review        — Claude reviews and refines the blueprint
    Normalise: deck_spec   — Pure Python _blueprint_to_deck_spec() (no LLM call)
    Phase 3: PPTX          — PowerPoint generator renders the spec to .pptx bytes

    Args:
        db:               SQLAlchemy session.
        workspace_id:     Workspace to draw knowledge from.
        deliverable_type: One of 'client_101', 'client_201', 'executive_summary'.
        focus_area:       Optional topic string (only used for executive_summary).

    Returns:
        DeliverablePptxResponse with metadata, sources, and pptx_bytes.
    """
    import sys as _sys
    _root = str(Path(__file__).parent.parent.parent)
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    from deliverables.generators.powerpoint_generator import generate_pptx

    if deliverable_type not in DELIVERABLE_TYPES:
        raise ValueError(
            f"Unsupported deliverable type '{deliverable_type}'. "
            f"Must be one of: {list(DELIVERABLE_TYPES)}"
        )

    # Only executive_summary supports a focus area
    effective_focus = focus_area if deliverable_type == "executive_summary" else None

    ws = db.get(Workspace, workspace_id)
    workspace_name = ws.name if ws else f"Workspace {workspace_id}"

    # ── Load deliverable methodology prompt ───────────────────────────────────
    generation_prompt = _load_prompt(deliverable_type)

    # ── Build graph intelligence digest ──────────────────────────────────────
    G = graph_memory_manager.get_workspace_graph(workspace_id, db)
    relevant_node_ids = _retrieve_relevant_nodes(G, effective_focus)

    if not relevant_node_ids:
        raise ValueError(
            "No knowledge found in workspace. "
            "Upload and process documents first."
        )

    graph_digest, source_refs, concept_ids, doc_ids = _build_graph_intelligence(
        G, db, workspace_id, relevant_node_ids, effective_focus
    )

    concept_count      = len(concept_ids)
    doc_count          = len(doc_ids)
    pattern_count      = db.query(ConsultingPattern).filter(
        ConsultingPattern.workspace_id == workspace_id
    ).count()
    relationship_count = db.query(Relationship).filter(
        Relationship.workspace_id == workspace_id
    ).count()

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 1 — BLUEPRINT
    # Claude analyses graph intelligence → produces consulting narrative blueprint
    # ═══════════════════════════════════════════════════════════════════════
    logger.info(
        "[deliverable ws=%d type=%s] Phase 1: generating blueprint "
        "(%d concepts, %d docs, %d patterns, %d relationships)",
        workspace_id, deliverable_type, concept_count, doc_count,
        pattern_count, relationship_count,
    )

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

    try:
        raw_blueprint = chat(
            system=_BLUEPRINT_SYSTEM_PROMPT,
            user=blueprint_message,
            max_tokens=16000,
            operation="deliverable_blueprint",
        )
    except Exception as exc:
        logger.error("[deliverable ws=%d] Phase 1 failed: %s", workspace_id, exc)
        raise RuntimeError(f"Blueprint generation failed: {exc}") from exc

    blueprint = _parse_json(raw_blueprint, "blueprint")
    logger.info(
        "[deliverable ws=%d] Phase 1 complete: %d slides in blueprint",
        workspace_id, len(blueprint.get("slides", [])),
    )

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 2 — QUALITY REVIEW
    # Claude reviews the blueprint and returns a refined version
    # ═══════════════════════════════════════════════════════════════════════
    logger.info(
        "[deliverable ws=%d type=%s] Phase 2: quality review",
        workspace_id, deliverable_type,
    )

    review_message = _build_review_message(blueprint)

    try:
        raw_review = chat(
            system=_REVIEW_SYSTEM_PROMPT,
            user=review_message,
            max_tokens=16000,
            operation="deliverable_review",
        )
    except Exception as exc:
        logger.error(
            "[deliverable ws=%d] Phase 2 failed: %s — using unreviewed blueprint",
            workspace_id, exc,
        )
        # Graceful degradation: proceed with original blueprint
        refined_blueprint = blueprint
        quality_score = None
    else:
        review_result = _parse_json(raw_review, "review")
        quality_score = review_result.get("quality_score")
        issues        = review_result.get("issues_found", [])
        fixes         = review_result.get("fixes_applied", [])
        refined_blueprint = review_result.get("refined_blueprint") or blueprint

        logger.info(
            "[deliverable ws=%d] Phase 2 complete: score=%s, %d issues found, %d fixes applied",
            workspace_id, quality_score, len(issues), len(fixes),
        )
        if issues:
            logger.info("[deliverable ws=%d] Review issues: %s", workspace_id, "; ".join(issues[:5]))
        if fixes:
            logger.info("[deliverable ws=%d] Fixes applied: %s", workspace_id, "; ".join(fixes[:5]))

    # ═══════════════════════════════════════════════════════════════════════
    # NORMALISE — blueprint → deck_spec  (zero LLM calls)
    # Pure Python transform; replaces the former Phase 3 LLM call
    # ═══════════════════════════════════════════════════════════════════════
    deck_spec = _blueprint_to_deck_spec(refined_blueprint, effective_focus, source_refs)
    logger.info(
        "[deliverable ws=%d] Normalised deck spec: %d slides",
        workspace_id, len(deck_spec.get("slides", [])),
    )

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 3 — PPTX GENERATION
    # PowerPoint generator renders the spec to .pptx bytes
    # ═══════════════════════════════════════════════════════════════════════
    logger.info(
        "[deliverable ws=%d type=%s] Phase 3: rendering PPTX",
        workspace_id, deliverable_type,
    )

    try:
        pptx_bytes = generate_pptx(deck_spec, workspace_name)
    except Exception as exc:
        logger.error("[deliverable ws=%d] Phase 3 (PPTX) failed: %s", workspace_id, exc)
        raise RuntimeError(f"PowerPoint assembly failed: {exc}") from exc

    logger.info(
        "[deliverable ws=%d] Phase 3 (PPTX) complete: %.1f KB PPTX generated",
        workspace_id, len(pptx_bytes) / 1024,
    )

    # ── Derive title ──────────────────────────────────────────────────────────
    type_label = DELIVERABLE_TYPES[deliverable_type]
    title = deck_spec.get("title") or refined_blueprint.get("title") or (
        f"{type_label}: {effective_focus}" if effective_focus
        else f"{type_label}: {workspace_name}"
    )

    # ── Persist metadata record ───────────────────────────────────────────────
    gen_notes = deck_spec.get("metadata", {}).get("generation_notes", "")
    if quality_score is not None:
        gen_notes = f"[Review score: {quality_score}/10] {gen_notes}".strip()

    deliverable = Deliverable(
        workspace_id=workspace_id,
        type=deliverable_type,
        title=title,
        content_markdown=None,
        source_concept_ids=concept_ids,
        source_document_ids=doc_ids,
    )
    db.add(deliverable)
    db.commit()
    db.refresh(deliverable)

    return DeliverablePptxResponse(
        deliverable=DeliverableOut.model_validate(deliverable),
        sources=source_refs,
        pptx_bytes=pptx_bytes,
        filename=f"{deliverable_type}_{workspace_id}_{deliverable.id}.pptx",
    )
