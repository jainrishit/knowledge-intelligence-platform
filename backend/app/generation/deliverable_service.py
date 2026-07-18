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

    # Top 30 by degree — reduced from 60 to keep the intelligence brief under ~8k chars.
    # The top 30 nodes by degree are the backbone of the workspace graph; nodes 31-60
    # add marginal signal but double the §1 section size and push the total prompt over
    # 20k input tokens, which destabilises the IBM Gateway under load.
    top_nodes = sorted(node_degree, key=lambda n: node_degree[n], reverse=True)[:30]

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
    # Description is truncated to 160 chars to avoid bloating the brief with
    # very long descriptions while still conveying the core concept meaning.
    concept_lines: list[str] = []
    for nid in top_nodes:
        c = concept_by_id.get(nid)
        if not c:
            continue
        degree = node_degree[nid]
        doc_name = doc_name_by_id.get(c.source_document_id or -1, "Unknown")
        desc = (c.description or "(no description)")[:160]
        if c.description and len(c.description) > 160:
            desc += "…"
        concept_lines.append(
            f"  [{c.type or 'General'}] {c.name} (connections: {degree}, "
            f"confidence: {c.confidence:.2f})\n"
            f"    {desc}\n"
            f"    Source: {doc_name}"
        )

    # ── 4. Build §2: Strongest relationships ─────────────────────────────────
    top_node_set = set(top_nodes)
    strong_edges: list[tuple[float, str]] = []
    for src, tgt, data in G.edges(data=True):
        if src not in top_node_set or tgt not in top_node_set:
            continue
        strength = float(data.get("strength", 0.7))
        if strength < 0.5:  # lowered from 0.6 — include implied relationships
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
    # Only the first 2 approach steps are included (truncated to 120 chars each).
    # Full 4-step approaches average ~800 chars per pattern; this reduces §3 from
    # ~15k chars to ~4k chars without losing the pattern's essential signal.
    patterns = db.query(ConsultingPattern).filter(
        ConsultingPattern.workspace_id == workspace_id
    ).all()
    pattern_lines: list[str] = []
    for p in patterns:
        related_ids = set(p.related_concept_ids or [])
        if not related_ids.intersection(set(concept_ids)):
            continue
        approach_steps = (p.ibm_approach or [])[:2]
        steps = "; ".join(s[:120] for s in approach_steps)
        problem = (p.problem_statement or "(none)")[:200]
        pattern_lines.append(
            f"  Pattern: {p.name}\n"
            f"    Problem: {problem}\n"
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

    When focus_area is provided, BFS uses a 0.4 strength threshold so that only
    meaningfully supported relationships are followed during topic expansion.
    This prevents a focused executive summary from drifting into loosely-connected
    concepts that dilute the final intelligence brief.
    """
    if not focus_area:
        # Short-circuit: no focus — every node is relevant
        return set(G.nodes())

    keywords = _get_focus_keywords(focus_area)
    matched  = find_nodes_by_keywords(G, keywords) or list(G.nodes())

    relevant: set[int] = set()
    for nid in matched:
        # Use a 0.4 strength floor so BFS follows only credible connections.
        nbr_nodes, _ = get_neighbourhood(G, nid, hops=2, strength_threshold=0.4)
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
      "visual_recommendation": "description of a diagram or visual that would strengthen this slide",
      "key_insights": ["The single most important consulting insight this slide communicates.", "Second insight if the slide covers two distinct points."],
      "graph_concepts": ["ConceptName1", "ConceptName2"],
      "relationships_used": ["ConceptA -> ConceptB (relationship_type)", "ConceptC -> ConceptD (relationship_type)"],
      "patterns_used": ["Pattern name if applicable"],
      "evidence": ["Document name that supports this slide's claims"]
    }
  ],
  "metadata": {
    "total_slides": <actual count>,
    "source_documents": ["doc name 1", ...],
    "open_items": ["gap or assumption that needs verification"],
    "generation_notes": "bottom-line summary: governing insight + narrative arc"
  }
}

ANNOTATION FIELDS — mandatory for every content slide (not required for section_divider, cover, end_slide):
  "key_insights"       — 1–3 consulting insights this slide communicates, stated as complete sentences; these are the
                         "so what" takeaways the user sees during plan review before approving generation
  "graph_concepts"     — list of specific concept names from the Graph Intelligence Brief that this slide draws on
  "relationships_used" — list of relationships this slide's narrative depends on, formatted "A -> B (type)"
  "patterns_used"      — list of consulting pattern names from §3 that apply to this slide (empty list if none)
  "evidence"           — list of source document names from §4 that support the claims on this slide

These annotation fields serve two purposes:
  1. They force you to verify that every slide is grounded in the graph — no grounding means no slide.
  2. They are shown to the user during plan review so they can see exactly what intelligence each slide uses,
     including the key insights the slide is designed to communicate.

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

    # Normalise slides: renumber, strip blueprint-only and plan-annotation keys.
    # - "purpose", "section", "visual_recommendation" are planning metadata.
    # - "key_insights", "graph_concepts", "relationships_used", "patterns_used",
    #   "evidence" are plan-review annotation fields shown to the user before
    #   approval; the PowerPoint renderer has no use for them and they must not
    #   appear in the rendered output.
    _BLUEPRINT_ONLY_KEYS = {
        "purpose", "section", "visual_recommendation",
        "key_insights", "graph_concepts", "relationships_used",
        "patterns_used", "evidence",
    }
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
# Phase 4 — Presentation Review (post-normalise, pre-render)
# ─────────────────────────────────────────────────────────────────────────────

_PRESENTATION_REVIEW_SYSTEM_PROMPT = """\
You are a senior IBM Consulting partner reviewing a finalised presentation spec
immediately before it is rendered into PowerPoint.

You are NOT rewriting the deck from scratch.
You are NOT generating new content from outside the spec.
You ARE a consulting manager doing a final quality pass to ensure every slide
earns its place, every layout is optimal, and the deck reads like a
consultant-prepared document — not an auto-generated one.

═══════════════════════════════════════════════════════════════════
YOUR MANDATE
═══════════════════════════════════════════════════════════════════

Return a corrected deck_spec that is immediately renderable.
Every correction must be reflected in the corrected_deck_spec.
Do not report issues you did not fix.

═══════════════════════════════════════════════════════════════════
PASS 1 — SLIDE REMOVAL
═══════════════════════════════════════════════════════════════════

Remove any slide that meets any of these conditions:

  • No content: title only, no bullets / boxes / columns / stats.
  • Pure placeholder: title is a category label with no supporting content
    ("Overview", "Introduction", "Background", "Conclusion", "Summary",
    "Next Steps", "Key Challenges", "Strategic Priorities").
  • Redundant: the same finding or topic already covered by another slide —
    merge the unique points into the better slide and delete this one.
  • Section orphan: a section_divider with zero content slides following it.
  • Duplicate section_divider: two consecutive section_dividers — keep one.
  • Unsupported: no bullets, no boxes, no columns, no stats.
    Exception: section_divider, large_text, end_slide are exempt.

After each removal, renumber slide_number sequentially from 1.
Record: "REMOVED slide [N] '[title]' — [reason]"

═══════════════════════════════════════════════════════════════════
PASS 2 — TITLE REWRITES
═══════════════════════════════════════════════════════════════════

For every content slide (layout NOT IN section_divider, end_slide):
  Is the title a TAKEAWAY STATEMENT — does it state a finding, conclusion,
  or recommendation specific to this deck?

  Labels to fix (rewrite to a takeaway):
    "Overview", "Introduction", "Background", "Current State", "Summary",
    "Conclusion", "Key Challenges", "Strategic Priorities", "Opportunities",
    "Recommendations", "Risks", "Next Steps", "Architecture", "Approach"

  A takeaway title:
    ✓ States the conclusion the slide proves
    ✓ Can be read alone and understood without the bullets
    ✓ Is specific — references a named concept, technology, or finding

  If you cannot write a meaningful takeaway from the existing bullets,
  the slide has no purpose — remove it (record the removal).
  Record each rewrite: "RETITLED slide [N]: '[old]' → '[new]'"

═══════════════════════════════════════════════════════════════════
PASS 3 — LAYOUT OPTIMISATION  (visual-first enforcement)
═══════════════════════════════════════════════════════════════════

COUNT how many content slides use title_content.
Content slides = layout NOT IN (section_divider, end_slide).

If more than 55% of content slides use title_content, convert slides to
better layouts.  Apply these conversion rules:

  RULE A — Exactly 4 bullets → convert to four_boxes_wide or four_boxes_stacked.
    Move bullets to "boxes" field.  Remove "bullets" field.

  RULE B — 5 or 6 bullets → convert to six_boxes.
    Move bullets to "boxes" field.  Remove "bullets" field.

  RULE C — Slide compares two things (current/target, as-is/to-be, before/after,
    two parallel tracks) → convert to two_col_dividers.
    Split bullets evenly between columns.  Set "col_heads" to the two themes.
    Set "columns" field.  Remove "bullets" field.

  RULE D — Slide has 4 equal parallel pillars, workstreams, or phases →
    convert to four_column.  Split bullets into 4 equal groups.
    Set "columns" field.  Remove "bullets" field.

  RULE E — Slide has 3 parallel themes with headings → four_column_headlines.
    Set "col_heads" (3 items) and "columns" (3 lists).

  RULE F — Slide has exactly 2 headline metrics with supporting context →
    convert to data_2_callouts.  Extract the two metrics as stats entries:
    {"label": "<metric>", "body": "<supporting sentence>"}.
    Remove "bullets" field.

  RULE G — If no large_text slide exists in the deck and there is a governing
    insight slide: convert the most important insight slide to large_text.
    Set the title to the full insight statement.  Remove bullets.

  Priority order: A > B > C > D > E > F > G.
  Stop converting once title_content share drops to ≤55% of content slides.
  Record: "CONVERTED slide [N] from title_content to [layout]: [reason]"

═══════════════════════════════════════════════════════════════════
PASS 4 — BULLET QUALITY
═══════════════════════════════════════════════════════════════════

For every bullet in every slide in the corrected spec:

  ✓ Must be a complete grammatical sentence (subject + verb + object).
    INCOMPLETE: "Legacy system constraints" → complete it.

  ✓ Must end with a period (never "..." or "…" or a dangling clause).
    FIX: complete the sentence, add period.

  ✓ Must be 20–100 characters.
    LONG: split at natural clause boundary into two separate bullets.

  ✓ Must NOT open with vague filler:
    "This shows...", "It is important...", "There are...",
    "Various...", "Multiple...", "Many organizations...", "Key aspects..."
    FIX: rewrite to lead with the specific insight.

  Fixing a truncated bullet is allowed.
  Inventing a new bullet from nothing is not allowed.

═══════════════════════════════════════════════════════════════════
PASS 5 — STRUCTURAL INTEGRITY
═══════════════════════════════════════════════════════════════════

After Passes 1–4:

  A — Every section_divider must have ≥1 content slide following it before
      the next section_divider or end of deck.
      FAIL → remove the orphaned section_divider.

  B — The deck must end with an end_slide.
      FAIL → append one.

  C — Renumber all slide_number fields sequentially from 1.

  D — Update metadata.total_slides to the final slide count.

═══════════════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════════

{
  "presentation_review_score": <integer 1–10 — score the CORRECTED output>,
  "slides_removed": <integer>,
  "slides_retitled": <integer>,
  "slides_converted": <integer>,
  "bullets_fixed": <integer>,
  "corrections": [
    "REMOVED slide 4 'Key Challenges' — label-only title with no supporting content",
    "RETITLED slide 6: 'Overview' → 'ISO 20022 adoption reshapes cross-border settlement'",
    "CONVERTED slide 9 from title_content to four_boxes_wide — 4 strategic priorities"
  ],
  "corrected_deck_spec": { <complete corrected deck_spec — same schema as input> }
}

ABSOLUTE RULES:
  - corrected_deck_spec must be a COMPLETE, valid deck_spec — all slides, all fields.
  - Do NOT invent new content.  Fix and improve only what is present.
  - Do NOT change deliverable_type, title, focus_area, or metadata.source_documents
    unless they are empty strings.
  - Every entry in "corrections" must be reflected in corrected_deck_spec — no phantom fixes.
  - Return ONLY the JSON object.  No preamble.  No markdown fences.  Start with { end with }.
"""


def _build_presentation_review_message(deck_spec: dict) -> str:
    """Construct the user message for Phase 4 (presentation review)."""
    slide_count    = len(deck_spec.get("slides", []))
    content_slides = [
        s for s in deck_spec.get("slides", [])
        if s.get("layout") not in ("section_divider", "end_slide", "cover")
    ]
    title_content_count = sum(
        1 for s in content_slides
        if s.get("layout", "title_content") in ("title_content", "callout_stat")
    )
    pct = round(100 * title_content_count / len(content_slides)) if content_slides else 0

    return (
        f"DECK SPEC TO REVIEW:\n"
        f"Total slides: {slide_count} | Content slides: {len(content_slides)} | "
        f"title_content share: {pct}% (target ≤55%)\n\n"
        + json.dumps(deck_spec, indent=2)
        + "\n\nApply all passes and return the corrected deck_spec JSON. Start with {{ and end with }}."
    )


def _review_deck_spec(deck_spec: dict, context: str = "") -> dict:
    """
    Phase 4 — Presentation Review.

    Claude acts as a consulting manager reviewing the finalised deck spec for:
      - slide removal (empty, redundant, orphaned)
      - title rewrites (labels → takeaways)
      - layout conversion (visual-first enforcement)
      - bullet quality fixes
      - structural integrity

    Returns the corrected deck_spec.  Falls back to the original on any error.

    Args:
        deck_spec: The normalised deck spec from _blueprint_to_deck_spec().
        context:   Log prefix (e.g. "ws=5 type=client_101").
    """
    review_message = _build_presentation_review_message(deck_spec)

    try:
        raw = chat(
            system=_PRESENTATION_REVIEW_SYSTEM_PROMPT,
            user=review_message,
            max_tokens=8192,
            operation="presentation_review",
        )
    except Exception as exc:
        logger.error("[presentation_review %s] LLM call failed: %s — using original spec", context, exc)
        return deck_spec

    try:
        result = _parse_json(raw, "presentation_review")
    except RuntimeError:
        logger.error("[presentation_review %s] JSON parse failed — using original spec", context)
        return deck_spec

    corrected = result.get("corrected_deck_spec")
    if not corrected or not isinstance(corrected, dict) or not corrected.get("slides"):
        logger.warning(
            "[presentation_review %s] No valid corrected_deck_spec returned — using original spec",
            context,
        )
        return deck_spec

    score          = result.get("presentation_review_score")
    corrections    = result.get("corrections", [])
    n_removed      = result.get("slides_removed", 0)
    n_retitled     = result.get("slides_retitled", 0)
    n_converted    = result.get("slides_converted", 0)
    n_bullets_fixed = result.get("bullets_fixed", 0)

    logger.info(
        "[presentation_review %s] score=%s | removed=%d retitled=%d converted=%d bullets_fixed=%d",
        context, score, n_removed, n_retitled, n_converted, n_bullets_fixed,
    )
    if corrections:
        logger.info(
            "[presentation_review %s] corrections: %s",
            context, "; ".join(corrections[:6]),
        )

    # Re-renumber slide_number fields sequentially to guarantee monotonic order
    slides = corrected.get("slides") or []
    for i, s in enumerate(slides, start=1):
        s["slide_number"] = i
    if "metadata" in corrected:
        corrected["metadata"]["total_slides"] = len(slides)

    return corrected


# ─────────────────────────────────────────────────────────────────────────────
# Validation Layer — deterministic pre-render gate (no LLM calls)
# ─────────────────────────────────────────────────────────────────────────────

_STRUCTURAL_LAYOUTS = frozenset({"section_divider", "cover", "end_slide", "sources"})
_EXEMPT_LAYOUTS = frozenset({"section_divider", "cover", "end_slide", "large_text",
                              "data_2_callouts", "callout_stat", "sources"})

_LABEL_TITLES = frozenset({
    "overview", "introduction", "background", "current state", "summary",
    "conclusion", "key challenges", "strategic priorities", "opportunities",
    "recommendations", "risks", "next steps", "architecture", "approach",
    "agenda", "thank you", "questions",
})

# Maximum safe bullet lengths to prevent overflow in the rendered slide.
# Bullets longer than this are split at the nearest sentence boundary (". ")
# or truncated with "…" to keep within the layout frame.
_MAX_BULLET_CHARS = 200


def _split_long_bullet(bullet: str, max_chars: int = _MAX_BULLET_CHARS) -> list[str]:
    """
    Split a single overly long bullet into ≤2 shorter ones at a sentence
    boundary.  If no sentence break exists within the limit, truncate at a
    word boundary and append "…".
    """
    if len(bullet) <= max_chars:
        return [bullet]

    # Try to split at a sentence boundary before the limit
    boundary = bullet[:max_chars].rfind(". ")
    if boundary > 20:
        part1 = bullet[:boundary + 1].strip()
        part2 = bullet[boundary + 1:].strip()
        if part2 and not part1.endswith((".", "!", "?")):
            part1 += "."
        return [p for p in [part1, part2] if p]

    # Fall back to word boundary truncation
    truncated = bullet[:max_chars].rsplit(" ", 1)[0]
    if truncated and not truncated.endswith((".", "!", "?")):
        truncated += "…"
    return [truncated]


def _validate_deck_spec(deck_spec: dict, context: str = "") -> dict:
    """
    Deterministic validation gate — runs immediately before PPTX render.
    No LLM calls.  Fixes or removes slides that fail quality criteria.

    Checks performed (spec: Validation Layer):
      1. No empty slides (title-only with no content fields populated)
      2. No title-only placeholder slides (label titles without content)
      3. Template compliance — layout must have required fields; demote to
         title_content and copy content when the required field is absent
         (overlap risk prevention):
           - four_boxes_wide / four_boxes_stacked → needs ≥2 non-empty boxes
           - six_boxes → needs ≥4 non-empty boxes
           - two_col_dividers → needs col_heads (list) + columns (2 lists)
           - four_column / four_column_headlines → needs columns (list of lists)
           - data_2_callouts → needs exactly 2 stats entries
      4. Sentence completeness: bullets must end with a period
      5. Overflow risk: long bullets are split; bullet lists are capped per
         layout safe limits
      6. Renumber and update metadata after any removals

    Returns the validated (and possibly modified) deck_spec.
    """
    slides_in = deck_spec.get("slides") or []
    slides_out: list[dict] = []
    removed: list[str] = []
    fixed: list[str] = []

    for slide in slides_in:
        layout = slide.get("layout", "title_content")
        title  = (slide.get("title") or "").strip()
        title_lower = title.lower()

        # ── Skip all structural / exempt layouts without content checks ────────
        if layout in _STRUCTURAL_LAYOUTS:
            slides_out.append(slide)
            continue

        # ── Check 1: empty slide — no content at all ──────────────────────────
        has_content = bool(
            slide.get("bullets")
            or slide.get("boxes")
            or slide.get("columns")
            or slide.get("stats")
            or layout in _EXEMPT_LAYOUTS   # large_text, data_2_callouts etc.
        )
        if not has_content:
            removed.append(f"REMOVED slide '{title}' — no content (empty slide)")
            continue

        # ── Check 2: pure label title with skeleton content ───────────────────
        if title_lower in _LABEL_TITLES:
            # Only remove if it also has minimal content (≤1 bullet)
            bullet_count = len(slide.get("bullets") or [])
            if bullet_count <= 1:
                removed.append(
                    f"REMOVED slide '{title}' — generic label title with insufficient content"
                )
                continue

        # ── Check 3: template compliance — layout field requirements ──────────
        slide = dict(slide)  # shallow copy so we can mutate safely

        if layout in ("four_boxes_wide", "four_boxes_stacked"):
            boxes = [b for b in (slide.get("boxes") or []) if b and b.strip()]
            if len(boxes) < 2:
                # Demote: convert bullets → boxes if there are enough, else fall
                # back to title_content so at least the content renders.
                bullets = slide.get("bullets") or []
                if len(bullets) >= 2:
                    slide["boxes"] = bullets[:6]
                    slide.pop("bullets", None)
                    fixed.append(
                        f"FIXED slide '{title}' ({layout}): promoted bullets to boxes"
                    )
                else:
                    slide["layout"] = "title_content"
                    fixed.append(
                        f"DEMOTED slide '{title}' from {layout} to title_content — "
                        f"insufficient boxes ({len(boxes)})"
                    )

        elif layout == "six_boxes":
            boxes = [b for b in (slide.get("boxes") or []) if b and b.strip()]
            if len(boxes) < 4:
                bullets = slide.get("bullets") or []
                if len(bullets) >= 4:
                    slide["boxes"] = bullets[:6]
                    slide.pop("bullets", None)
                    fixed.append(
                        f"FIXED slide '{title}' (six_boxes): promoted bullets to boxes"
                    )
                else:
                    slide["layout"] = "title_content"
                    fixed.append(
                        f"DEMOTED slide '{title}' from six_boxes to title_content — "
                        f"insufficient boxes ({len(boxes)})"
                    )

        elif layout == "two_col_dividers":
            col_heads = slide.get("col_heads") or []
            columns   = slide.get("columns") or []
            if len(col_heads) < 2 or len(columns) < 2:
                # Salvage: if there are bullets, split them evenly across 2 columns
                bullets = slide.get("bullets") or []
                if len(bullets) >= 2:
                    mid = len(bullets) // 2
                    slide["columns"] = [bullets[:mid], bullets[mid:]]
                    if len(col_heads) < 2:
                        slide["col_heads"] = ["", ""]
                    slide.pop("bullets", None)
                    fixed.append(
                        f"FIXED slide '{title}' (two_col_dividers): split bullets into columns"
                    )
                else:
                    slide["layout"] = "title_content"
                    fixed.append(
                        f"DEMOTED slide '{title}' from two_col_dividers to title_content — "
                        f"missing col_heads or columns"
                    )

        elif layout in ("four_column", "four_column_headlines"):
            columns = slide.get("columns") or []
            # columns must be a list of lists with at least 2 non-empty sub-lists
            valid_cols = [c for c in columns if isinstance(c, list) and c]
            if len(valid_cols) < 2:
                bullets = slide.get("bullets") or []
                if len(bullets) >= 2:
                    # Split bullets evenly across 4 groups (or fewer if < 4 bullets)
                    n = min(4, len(bullets))
                    groups: list[list[str]] = [[] for _ in range(n)]
                    for idx, b in enumerate(bullets):
                        groups[idx % n].append(b)
                    slide["columns"] = groups
                    slide.pop("bullets", None)
                    fixed.append(
                        f"FIXED slide '{title}' ({layout}): split bullets into columns"
                    )
                else:
                    slide["layout"] = "title_content"
                    fixed.append(
                        f"DEMOTED slide '{title}' from {layout} to title_content — "
                        f"missing or invalid columns field"
                    )

        elif layout == "data_2_callouts":
            stats = slide.get("stats") or []
            valid_stats = [s for s in stats if isinstance(s, dict) and s.get("label")]
            if len(valid_stats) < 2:
                # Try to salvage from bullets
                bullets = slide.get("bullets") or []
                if len(bullets) >= 2:
                    slide["stats"] = [
                        {"label": b[:40].strip(), "body": b}
                        for b in bullets[:2]
                    ]
                    slide.pop("bullets", None)
                    fixed.append(
                        f"FIXED slide '{title}' (data_2_callouts): built stats from bullets"
                    )
                else:
                    slide["layout"] = "title_content"
                    fixed.append(
                        f"DEMOTED slide '{title}' from data_2_callouts to title_content — "
                        f"fewer than 2 valid stats entries"
                    )
            elif len(valid_stats) > 2:
                # Cap at 2 to prevent overflow
                slide["stats"] = valid_stats[:2]
                fixed.append(
                    f"CAPPED slide '{title}' (data_2_callouts) stats to 2 entries (overflow guard)"
                )

        # ── Check 4: fix bullets — ensure period termination ─────────────────
        if slide.get("bullets"):
            fixed_bullets = []
            for b in slide["bullets"]:
                b = b.strip()
                if b and not b.endswith((".", "!", "?", "…")):
                    b = b + "."
                    fixed.append(f"Fixed bullet termination on slide '{title}'")
                fixed_bullets.append(b)
            slide["bullets"] = fixed_bullets

        # ── Check 5a: overflow risk — split long bullets ──────────────────────
        if slide.get("bullets"):
            expanded: list[str] = []
            for b in slide["bullets"]:
                parts = _split_long_bullet(b)
                if len(parts) > 1:
                    fixed.append(f"Split long bullet on slide '{title}' ({len(b)} chars)")
                expanded.extend(parts)
            slide["bullets"] = expanded

        # ── Check 5b: overflow guard — cap bullets per layout ────────────────
        # title_content: ≤7; two_column: ≤12; agenda: ≤10
        max_bullets: dict[str, int] = {
            "title_content": 7,
            "two_column": 12,
            "agenda": 10,
            "callout_stat": 4,
        }
        if slide.get("layout", layout) in max_bullets and slide.get("bullets"):
            cap = max_bullets[slide["layout"]]
            if len(slide["bullets"]) > cap:
                slide["bullets"] = slide["bullets"][:cap]
                fixed.append(f"Truncated bullets on slide '{title}' to {cap} (overflow guard)")

        # ── Check 5c: overflow guard — cap boxes per layout ──────────────────
        max_boxes: dict[str, int] = {
            "four_boxes_wide": 4,
            "four_boxes_stacked": 4,
            "six_boxes": 6,
        }
        if slide.get("layout", layout) in max_boxes and slide.get("boxes"):
            cap_b = max_boxes[slide["layout"]]
            if len(slide["boxes"]) > cap_b:
                slide["boxes"] = slide["boxes"][:cap_b]
                fixed.append(
                    f"Capped boxes on slide '{title}' to {cap_b} (overflow guard)"
                )

        slides_out.append(slide)

    # ── Renumber sequentially ─────────────────────────────────────────────────
    for i, s in enumerate(slides_out, start=1):
        s["slide_number"] = i

    if removed or fixed:
        logger.info(
            "[validation %s] %d removed, %d fixes; final slide count: %d",
            context, len(removed), len(fixed), len(slides_out),
        )
        for msg in removed[:10]:
            logger.info("[validation %s] %s", context, msg)

    validated = {**deck_spec, "slides": slides_out}
    if "metadata" in validated:
        validated["metadata"] = {
            **validated["metadata"],
            "total_slides": len(slides_out),
        }
    return validated


# ─────────────────────────────────────────────────────────────────────────────
# JSON parsing
# ─────────────────────────────────────────────────────────────────────────────

def _strip_fences(text: str) -> str:
    """Strip markdown code fences from an LLM response."""
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    return text.strip()


def _is_truncated(text: str) -> bool:
    """
    Return True when the LLM response appears to be truncated mid-JSON.

    A complete JSON object must end with '}'.  When max_tokens is hit the
    response can end mid-string, mid-array, or mid-object.  We detect this
    by checking whether the stripped text ends with the closing brace of the
    outermost object.
    """
    t = text.strip()
    return bool(t) and not t.endswith("}")


def _repair_truncated_json(text: str) -> str:
    """
    Attempt to close a truncated JSON object by appending the right number of
    closing brackets/braces.

    Strategy:
      1. Walk the entire string tracking brace/bracket/string depth.
      2. After the walk, append the necessary closing tokens in reverse order.
      3. Return the repaired string (may still not be valid — caller tries
         json.loads and falls back gracefully).
    """
    stack: list[str] = []
    in_string = False
    escape = False

    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ("{", "["):
            stack.append("}" if ch == "{" else "]")
        elif ch in ("}", "]"):
            if stack and stack[-1] == ch:
                stack.pop()

    # Close any unclosed string first
    suffix = '"' if in_string else ""
    # Close all open containers in reverse
    suffix += "".join(reversed(stack))
    return text + suffix


def _continue_truncated_json(partial: str, phase: str) -> str:
    """
    Ask Claude to complete a truncated JSON response.

    Sends the last 2000 chars of the partial response as context so Claude can
    see where it was cut off and produce only the continuation.  Returns the
    full completed string (partial + continuation).
    """
    context_tail = partial[-2000:]
    continuation_prompt = (
        "The previous JSON response was cut off due to token limits.\n"
        "Here is the END of the truncated response:\n\n"
        f"...{context_tail}\n\n"
        "Continue the JSON EXACTLY from where it was cut off.\n"
        "Output ONLY the continuation — do NOT repeat what was already written.\n"
        "Close all open arrays, objects, and strings.\n"
        "The final character you output must be the closing } of the root object.\n"
        "No preamble. No markdown fences. Start immediately with the continuation."
    )
    system_prompt = (
        "You are a JSON completion assistant. "
        "You receive the tail of a truncated JSON document and output only the continuation "
        "needed to make it valid. Never repeat already-output content."
    )
    logger.warning(
        "[%s] JSON truncated (%d chars) — requesting continuation from Claude",
        phase, len(partial),
    )
    try:
        continuation = chat(
            system=system_prompt,
            user=continuation_prompt,
            max_tokens=8192,
            operation=f"{phase}_continuation",
        )
        # Strip any fences Claude might add
        continuation = _strip_fences(continuation)
        logger.info(
            "[%s] Continuation received: %d chars — assembling full response",
            phase, len(continuation),
        )
        return partial + continuation
    except Exception as exc:
        logger.error("[%s] Continuation request failed: %s", phase, exc)
        return partial  # caller will try repair heuristic


def _parse_json(raw: str, phase: str) -> dict:
    """
    Robustly extract and parse JSON from an LLM response.

    Handles (in order):
      1. Clean JSON — direct json.loads
      2. Fenced JSON (```json ... ```) — strip fences then json.loads
      3. JSON embedded in prose — extract first {...} block
      4. Truncated JSON (hit max_tokens) — request continuation from Claude,
         then re-try steps 1-3 on the completed response
      5. Structural repair heuristic — close unclosed braces/brackets and retry
    """
    def _attempt(text: str) -> dict | None:
        """Try to parse text as JSON via direct parse and brace-extraction."""
        text = _strip_fences(text)

        # Direct parse
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Extract first top-level {...} block from mixed prose
        brace_start = text.find("{")
        if brace_start != -1:
            depth = 0
            in_string = False
            escape = False
            for i, ch in enumerate(text[brace_start:], start=brace_start):
                if escape:
                    escape = False
                    continue
                if ch == "\\" and in_string:
                    escape = True
                    continue
                if ch == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
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
        return None

    # ── Attempt 1: parse as-is ────────────────────────────────────────────
    result = _attempt(raw)
    if result is not None:
        return result

    # ── Attempt 2: truncation continuation ────────────────────────────────
    text_stripped = _strip_fences(raw)
    if _is_truncated(text_stripped):
        logger.warning(
            "[%s] Response appears truncated (%d chars, last char: %r) — "
            "requesting Claude continuation",
            phase, len(text_stripped), text_stripped[-1] if text_stripped else "",
        )
        completed = _continue_truncated_json(text_stripped, phase)
        result = _attempt(completed)
        if result is not None:
            logger.info("[%s] Continuation strategy succeeded — JSON parsed", phase)
            return result

        # ── Attempt 3: structural repair on the completed text ─────────────
        repaired = _repair_truncated_json(completed)
        result = _attempt(repaired)
        if result is not None:
            logger.info("[%s] Repair heuristic succeeded after continuation — JSON parsed", phase)
            return result
    else:
        # Not obviously truncated but still failed — try repair on original
        repaired = _repair_truncated_json(text_stripped)
        result = _attempt(repaired)
        if result is not None:
            logger.info("[%s] Repair heuristic succeeded — JSON parsed", phase)
            return result

    logger.error("[%s] All JSON recovery strategies failed. First 400 chars: %.400s", phase, raw)
    raise RuntimeError(
        f"Phase '{phase}': the AI returned a response that could not be parsed as JSON "
        "after truncation recovery and structural repair. Please try again."
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
    # The generation_prompt file contains the full deliverable methodology (25-27k chars).
    # It is NOT sent in the user message because:
    #   1. The system prompt (_BLUEPRINT_SYSTEM_PROMPT) already contains all quality rules,
    #      layout mandates, bullet quality rules, annotation field requirements, etc.
    #   2. Embedding a 26k-char file in the user message pushes total input to ~20k tokens,
    #      which causes consistent 502 Bad Gateway errors on the IBM Gateway under load.
    #   3. The generation prompt files overlap ~80% with the system prompt content.
    #
    # Instead we send a compact deliverable-type summary (~200 chars) that tells Claude
    # WHAT type of deliverable to produce. The HOW is fully specified in the system prompt.
    _DELIVERABLE_SUMMARIES = {
        "client_101": (
            "DELIVERABLE TYPE: Client 101 — a standardised briefing for a consultant new to "
            "this client/subject. Cover: overview, business context, key domains, operating "
            "model, strategic priorities, key challenges, relevant capabilities, ecosystem "
            "relationships, and implications for IBM. Story arc: orient → understand → "
            "so-what."
        ),
        "client_201": (
            "DELIVERABLE TYPE: Client 201 — a deep consulting analysis for an experienced "
            "engagement team. Cover: strategic situation, market forces, competitive dynamics, "
            "capability assessment, architecture and technology dependencies, risk landscape, "
            "opportunity sizing, and IBM's recommended approach. Story arc: situation → "
            "complication → resolution → recommendations."
        ),
        "executive_summary": (
            "DELIVERABLE TYPE: Executive Summary — a focused leadership briefing on the "
            f"{'focus area: ' + focus_area if focus_area else 'most significant workspace theme'}. "
            "Cover: the single governing insight, key findings, strategic implications, risks, "
            "and the recommended next action. Concise: no more than 8–10 content slides."
        ),
    }
    deliverable_summary = _DELIVERABLE_SUMMARIES.get(
        deliverable_type,
        f"DELIVERABLE TYPE: {deliverable_type}",
    )

    return (
        f"Workspace: {workspace_name}\n"
        f"{focus_line}\n\n"
        f"{deliverable_summary}\n\n"
        f"{slide_guidance}\n\n"
        f"{graph_digest}\n\n"
        f"=== TASK ===\n"
        f"Produce the presentation blueprint JSON for the deliverable type above. Ensure:\n"
        f"  - Content slide count is {min_s}–{max_s} (target {target_s})\n"
        f"  - Every content slide populates: key_insights, graph_concepts, relationships_used,\n"
        f"    patterns_used, evidence (annotation mandate from system prompt)\n"
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
    try:
        from deliverables.generators.powerpoint_generator import generate_pptx
    except (ImportError, ModuleNotFoundError) as _imp_err:
        raise RuntimeError(
            f"PowerPoint generator could not be loaded: {_imp_err}. "
            "Ensure the deliverables package is installed and the backend is started "
            "from the project root directory."
        ) from _imp_err

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
            max_tokens=8192,
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
            max_tokens=8192,
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
    # PHASE 4 — PRESENTATION REVIEW
    # Claude acts as consulting manager: removes weak slides, rewrites label
    # titles, converts bullet-heavy layouts to visual layouts, fixes bullets.
    # Falls back to original deck_spec on any error.
    # ═══════════════════════════════════════════════════════════════════════
    logger.info(
        "[deliverable ws=%d type=%s] Phase 4: presentation review",
        workspace_id, deliverable_type,
    )
    deck_spec = _review_deck_spec(
        deck_spec,
        context=f"ws={workspace_id} type={deliverable_type}",
    )
    logger.info(
        "[deliverable ws=%d] Phase 4 complete: %d slides after review",
        workspace_id, len(deck_spec.get("slides", [])),
    )

    # ═══════════════════════════════════════════════════════════════════════
    # VALIDATION — deterministic pre-render gate
    # No LLM calls. Removes empty/placeholder slides, fixes bullet termination,
    # guards against overflow.  Runs after Phase 4 to catch anything missed.
    # ═══════════════════════════════════════════════════════════════════════
    deck_spec = _validate_deck_spec(
        deck_spec,
        context=f"ws={workspace_id} type={deliverable_type}",
    )
    logger.info(
        "[deliverable ws=%d] Validation complete: %d slides before render",
        workspace_id, len(deck_spec.get("slides", [])),
    )

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 5 — PPTX GENERATION
    # PowerPoint generator renders the reviewed spec to .pptx bytes
    # ═══════════════════════════════════════════════════════════════════════
    logger.info(
        "[deliverable ws=%d type=%s] Phase 5: rendering PPTX",
        workspace_id, deliverable_type,
    )

    try:
        pptx_bytes = generate_pptx(deck_spec, workspace_name)
    except Exception as exc:
        logger.error("[deliverable ws=%d] Phase 5 (PPTX) failed: %s", workspace_id, exc)
        raise RuntimeError(f"PowerPoint assembly failed: {exc}") from exc

    logger.info(
        "[deliverable ws=%d] Phase 5 (PPTX) complete: %.1f KB PPTX generated",
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
