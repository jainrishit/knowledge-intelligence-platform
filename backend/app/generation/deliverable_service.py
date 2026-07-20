"""
Deliverable generation service — Client Material Generator.

Architecture (five-phase pipeline):

  Phase 1 — BLUEPRINT
    Claude receives the workspace knowledge graph intelligence brief and
    produces a structured narrative blueprint: storyline, section plan, slide
    purposes, key messages, and layout recommendations.
    System prompt: _BLUEPRINT_SYSTEM_PROMPT (annotation fields EXCLUDED for
    token efficiency on the direct-generation path).
    For the plan-workflow path, plan_service uses _PLAN_BLUEPRINT_SYSTEM_PROMPT
    instead (annotation fields REQUIRED so the plan review UI can show grounding).
    Output: blueprint dict.

  Phase 2 — QUALITY REVIEW  (direct generation path only)
    Claude reviews the blueprint as a senior consultant — checking storyline
    coherence, executive readability, slide quality, content balance, narrative
    flow, and knowledge graph coverage.  Issues are identified and fixes applied.
    System prompt: _REVIEW_SYSTEM_PROMPT.
    Output: refined blueprint dict.

  Normalise — _blueprint_to_deck_spec()
    Zero-LLM-call Python normaliser.  Copies slides, strips annotation-only
    fields (purpose, key_insights, graph_concepts, relationships_used,
    patterns_used, evidence), renumbers slide_number sequentially, injects
    focus_area, and populates metadata.source_documents from source_refs.
    Output: deck_spec dict.

  Phase 3 — VALIDATION  (deterministic, no LLM)
    _validate_deck_spec() runs before every render.  Removes empty/placeholder
    slides, reroutes stray layouts via _RESTRICTED_LAYOUTS, enforces layout
    diversity (no more than 2 consecutive identical layouts via _SIBLING_LAYOUT),
    rewrites or drops label section dividers, applies ending-backstop swap,
    enforces process_diagram minimum 4 steps, removes stray Sources slides, and
    fixes bullet termination.  All deterministic — no provider round-trips.

  Phase 4 — CONDITIONAL PRESENTATION REVIEW
    _review_deck_spec() runs ONLY when _residual_ending_issues() detects a
    residual content gap the deterministic validator cannot synthesise
    (e.g. the deck still lacks a client-directed recommendation close).
    System prompt: _PRESENTATION_REVIEW_SYSTEM_PROMPT.
    On clean decks this phase is skipped entirely.

  Phase 5 — PPTX RENDER
    generate_pptx() renders the validated deck_spec to .pptx bytes using
    python-pptx and the IPC_PPT_Template_2026.pptx IBM IPC template.

For the plan-workflow path (plan_service.py):
    generate_plan()        → Phase 1 only (_PLAN_BLUEPRINT_SYSTEM_PROMPT)
    revise_plan()          → user-driven Claude revision loop
    approve_and_generate() → Normalise → Phase 3 (Validation) → Phase 5 (PPTX)
    Phase 4 is intentionally skipped: user has already reviewed and approved.

Knowledge graph intelligence (not raw text):
    The context fed to Claude is a structured graph digest:
      - Top concepts ranked by degree centrality (most-connected nodes)
      - Strongest relationships (edge strength ≥ threshold, with reasoning)
      - Consulting patterns and their problem/approach summaries
      - Per-document evidence summary
    This ensures Claude reasons from graph topology, not flat document dumps.

Prompt files:
    backend/deliverables/prompts/{deliverable_type}.md
    These files are loaded but their content is summarised into _DELIVERABLE_SUMMARIES
    (inside _build_blueprint_message) — the full ~26 KB files are NOT sent in the
    user message to avoid IBM Gateway 502s under load.
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

# ─────────────────────────────────────────────────────────────────────────────
# Concept prioritisation — preservation + theme coverage + type affinity
# ─────────────────────────────────────────────────────────────────────────────

# How many concepts the intelligence brief carries, per deliverable type.  Bounds
# the prompt token budget (flat in corpus size) and makes the Executive Summary the
# leanest / most selective.
_DIGEST_CONCEPT_BUDGET: dict[str, int] = {
    "executive_summary": 20,
    "client_101":        28,
    "client_201":        32,
}
_DEFAULT_DIGEST_BUDGET = 30

# Per-type keyword affinity — nudges selection toward the content each deck needs.
_TYPE_AFFINITY: dict[str, frozenset] = {
    "executive_summary": frozenset({
        "strategy", "strategic", "opportunity", "value", "growth", "risk",
        "decision", "recommendation", "market", "revenue", "impact", "priority",
        "outcome", "adoption", "roi",
    }),
    "client_101": frozenset({
        "market", "opportunity", "value", "adoption", "business", "customer",
        "benefit", "growth", "revenue", "cost", "demand", "positioning",
        "ecosystem", "case", "driver",
    }),
    "client_201": frozenset({
        "architecture", "protocol", "integration", "governance", "operating",
        "implementation", "security", "infrastructure", "platform", "deployment",
        "dependency", "framework", "api", "data", "control", "compliance",
        "custody", "settlement", "ledger", "node",
    }),
}


def _tokenise(text: str) -> set[str]:
    """Lowercased alphanumeric tokens ≥3 chars, stopwords removed."""
    if not text:
        return set()
    return {
        tok for tok in re.split(r"[^a-z0-9]+", text.lower())
        if len(tok) >= 3 and tok not in _STOPWORDS
    }


def _detect_themes(G: nx.DiGraph, node_ids: list[int]) -> dict[int, int]:
    """
    Partition candidate concepts into themes via greedy modularity community
    detection on the undirected subgraph.  Returns node_id → theme_id.  Falls back
    to a single theme when the graph is too small or detection fails.
    """
    if len(node_ids) < 4:
        return {n: 0 for n in node_ids}
    try:
        from networkx.algorithms.community import greedy_modularity_communities
        sub = G.subgraph(node_ids).to_undirected()
        comms = greedy_modularity_communities(sub)
    except Exception:
        return {n: 0 for n in node_ids}
    theme: dict[int, int] = {}
    for i, com in enumerate(comms):
        for n in com:
            theme[n] = i
    # Isolated nodes (no qualifying edges) each become their own singleton theme.
    next_id = len(list(comms))
    for n in node_ids:
        if n not in theme:
            theme[n] = next_id
            next_id += 1
    return theme


def _concept_affinity(concept, kw_set: frozenset) -> float:
    """0..1 affinity of a concept to a deliverable type's keyword set."""
    if not kw_set or concept is None:
        return 0.0
    text = f"{concept.name or ''} {concept.type or ''} {concept.description or ''}"
    return min(len(_tokenise(text) & kw_set), 3) / 3.0


def _prioritise_nodes(
    candidate_ids: list[int],
    concept_by_id: dict,
    degree: dict[int, int],
    pattern_concept_ids: set[int],
    title_tokens: set[str],
    doc_profiles: list[str],
    focus_kw: list[str],
    deliverable_type: str,
    themes: dict[int, int],
    budget: int,
) -> tuple[list[int], dict[int, list[str]]]:
    """
    Select up to `budget` concepts — preserving strategically critical ones and
    covering multiple themes rather than ranking on graph degree alone.

    A concept is CRITICAL (and reserved a slot regardless of connectivity) when it
    sits in a consulting pattern (opportunity/recommendation/risk context), appears
    in a document title, is referenced across ≥2 documents, or matches the user's
    focus area.  This is what stops a low-degree but pivotal concept (e.g. a named
    product that is the central topic of the docs) from being dropped.

    Returns (ordered_selected_ids, criticality_flags_by_id).
    """
    if not candidate_ids:
        return [], {}

    from collections import defaultdict

    max_deg = max((degree.get(n, 0) for n in candidate_ids), default=1) or 1
    aff_kw = _TYPE_AFFINITY.get(deliverable_type, frozenset())

    crit_score: dict[int, float] = {}
    crit_flags: dict[int, list[str]] = {}
    score: dict[int, float] = {}

    for nid in candidate_ids:
        c = concept_by_id.get(nid)
        name_l = ((c.name if c else "") or "").lower()
        name_toks = _tokenise(name_l)
        flags: list[str] = []
        cs = 0.0
        if nid in pattern_concept_ids:                       # opportunity/reco/risk
            cs += 1.0; flags.append("in-pattern")
        if name_toks and (name_toks & title_tokens):         # in a document title
            cs += 1.0; flags.append("doc-title")
        mentions = sum(1 for p in doc_profiles if name_l and name_l in p)
        if mentions >= 2:                                    # cross-document topic
            cs += min(mentions, 4) * 0.3; flags.append(f"{mentions}-docs")
        if focus_kw and any(k in name_l for k in focus_kw):  # user-requested
            cs += 1.0; flags.append("focus")
        crit_score[nid] = cs
        crit_flags[nid] = flags

        deg_n = degree.get(nid, 0) / max_deg
        conf = (c.confidence if c else 0.0) or 0.0
        aff = _concept_affinity(c, aff_kw)
        score[nid] = 0.40 * deg_n + 0.25 * min(cs, 2.0) / 2.0 + 0.20 * aff + 0.15 * conf

    # ── Reserve slots for critical concepts (guaranteed inclusion) ────────────
    reserve_cap = max(1, budget // 2)
    critical = sorted(
        [n for n in candidate_ids if crit_score[n] >= 1.0],
        key=lambda n: (crit_score[n], score[n]), reverse=True,
    )[:reserve_cap]
    selected: list[int] = list(critical)
    sel_set = set(selected)

    # ── Theme-coverage fill for the rest (round-robin, per-theme cap) ─────────
    by_theme: dict[int, list[int]] = defaultdict(list)
    for n in sorted((x for x in candidate_ids if x not in sel_set),
                    key=lambda n: score[n], reverse=True):
        by_theme[themes.get(n, -1)].append(n)
    theme_order = sorted(by_theme, key=lambda t: score[by_theme[t][0]], reverse=True)

    per_theme_cap = max(1, int(round(budget * 0.5)))
    theme_count: dict[int, int] = defaultdict(int)
    for n in selected:
        theme_count[themes.get(n, -1)] += 1

    while len(selected) < budget and any(by_theme[t] for t in theme_order):
        progressed = False
        for t in theme_order:
            if len(selected) >= budget:
                break
            if not by_theme[t] or theme_count[t] >= per_theme_cap:
                continue
            selected.append(by_theme[t].pop(0))
            theme_count[t] += 1
            progressed = True
        if not progressed:  # every theme at its cap — relax to finish the budget
            for t in theme_order:
                while by_theme[t] and len(selected) < budget:
                    selected.append(by_theme[t].pop(0))
            break

    ordered = sorted(set(selected),
                     key=lambda n: (crit_score[n] >= 1.0, score[n]), reverse=True)[:budget]
    return ordered, crit_flags


def _build_graph_intelligence(
    G: nx.DiGraph,
    db: Session,
    workspace_id: int,
    relevant_node_ids: set[int],
    focus_area: str | None,
    deliverable_type: str = "",
) -> tuple[str, list[SourceRef], list[int], list[int]]:
    """
    Build a structured graph intelligence digest for Claude — not a flat text dump.

    Concept selection is preservation-first and theme-aware (see _prioritise_nodes)
    and tuned per deliverable type, NOT a raw top-N-by-degree cut.  The brief size is
    bounded by a per-type concept budget, so it stays flat as the corpus grows.

    Returns
    -------
    graph_digest : str  (multi-section: §0 prioritisation, §1 concepts, §2
        relationships, §3 patterns, §4 evidence)
    source_refs  : list[SourceRef]
    concept_ids  : list[int]
    doc_ids      : list[int]
    """
    # ── 1. Candidate pool: degree for every relevant node ─────────────────────
    node_degree: dict[int, int] = {}
    for nid in relevant_node_ids:
        if G.has_node(nid):
            node_degree[nid] = G.in_degree(nid) + G.out_degree(nid)
    candidate_ids = list(node_degree)

    cand_concepts = (
        db.query(Concept).filter(Concept.id.in_(candidate_ids)).all()
        if candidate_ids else []
    )
    concept_by_id: dict[int, Concept] = {c.id: c for c in cand_concepts}

    # ── 2. Preservation signals (patterns, doc titles, cross-doc mentions) ────
    ws_patterns = db.query(ConsultingPattern).filter(
        ConsultingPattern.workspace_id == workspace_id
    ).all()
    pattern_concept_ids: set[int] = set()
    for p in ws_patterns:
        for x in (p.related_concept_ids or []):
            try:
                pattern_concept_ids.add(int(x))
            except (TypeError, ValueError):
                pass

    ws_docs = db.query(Document).filter(Document.workspace_id == workspace_id).all()
    title_tokens: set[str] = set()
    doc_profiles: list[str] = []
    for d in ws_docs:
        title_tokens |= _tokenise(d.title or "")
        # `topics` is a JSON column — may be a list, a string, or None. Coerce to str.
        topics = getattr(d, "topics", None)
        topics_str = " ".join(str(t) for t in topics) if isinstance(topics, list) else (topics or "")
        # Bounded profile (title + topics + head of body) for cross-doc mentions —
        # keeps this O(1) per doc as the corpus scales.
        profile = " ".join(filter(None, [
            d.title or "", topics_str, (d.raw_text or "")[:3000],
        ])).lower()
        doc_profiles.append(profile)

    focus_kw = [k.lower() for k in _get_focus_keywords(focus_area)] if focus_area else []

    # ── 3. Theme-aware, preservation-first selection ──────────────────────────
    budget = _DIGEST_CONCEPT_BUDGET.get(deliverable_type, _DEFAULT_DIGEST_BUDGET)
    themes = _detect_themes(G, candidate_ids)
    top_nodes, crit_flags = _prioritise_nodes(
        candidate_ids, concept_by_id, node_degree, pattern_concept_ids,
        title_tokens, doc_profiles, focus_kw, deliverable_type, themes, budget,
    )
    concepts = [concept_by_id[n] for n in top_nodes if n in concept_by_id]
    themes_selected = sorted({themes.get(n, -1) for n in top_nodes})

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
    patterns = ws_patterns  # already fetched for preservation signals
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

    # §0 — how content was prioritised (transparency + steers the LLM to lead
    # with the preserved, cross-theme concepts rather than treating all equally).
    preserved = [
        f"  • {concept_by_id[n].name} [{', '.join(crit_flags[n])}]"
        for n in top_nodes
        if n in concept_by_id and crit_flags.get(n)
    ][:12]
    prioritisation_lines = [
        f"  Deliverable type: {deliverable_type or 'general'} — selection tuned for it.",
        f"  Concepts selected: {len(top_nodes)} of {len(candidate_ids)} candidates "
        f"(per-type budget {budget}); more documents refine this selection, never enlarge it.",
        f"  Themes represented: {len(themes_selected)} (no single theme may dominate the deck).",
    ]
    if preserved:
        prioritisation_lines.append(
            "  Preserved critical concepts (kept regardless of graph connectivity):"
        )
        prioritisation_lines.extend(preserved)
    parts.append("§0 PRIORITISATION\n" + "\n".join(prioritisation_lines))

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
You are a principal management consultant at IBM Consulting acting as a consulting writer.

YOUR ROLE: Generate consulting-quality slide CONTENT.
YOU DO NOT: design presentations, invent layouts, choose visual arrangements.
THE TEMPLATE: determines design. YOU: determine content.

═══════════════════════════════════════════════════════════════════════
TWO-STAGE GENERATION (MANDATORY)
═══════════════════════════════════════════════════════════════════════

Stage 1 — Extract facts from the knowledge graph:
  Read the Graph Intelligence Brief carefully.
  Identify the key concepts, relationships, patterns, and evidence.
  Note what the graph ACTUALLY says — not what you assume.

Stage 2 — Transform facts into consulting-quality communication:
  Rewrite every graph fact as executive-level consulting language.
  The graph provides raw intelligence.
  You provide consulting-quality communication.
  Do NOT paste graph concepts directly onto slides.
  Transform them into complete, executive-level statements.

The final content must be boardroom-ready — not raw graph output.

═══════════════════════════════════════════════════════════════════════
ABSOLUTE PROHIBITIONS
═══════════════════════════════════════════════════════════════════════

NEVER generate speaker notes or presenter coaching.
The following phrases must NEVER appear anywhere in slide content:
  ✗ "This slide establishes..."
  ✗ "This slide shows..."
  ✗ "This slide provides..."
  ✗ "If the audience asks..."
  ✗ "Use this slide to..."
  ✗ "The presenter should..."
  ✗ "As the presenter..."
  ✗ "This section covers..."

These are speaker notes.  They must never appear in slide titles, bullets, boxes, or columns.
Generate only content intended to appear on the slide itself.

NEVER generate label titles:
  ✗ "Technology Architecture"    ✓ "ISO 20022 reduces reconciliation cost by 40% across all corridors"
  ✗ "Market Opportunity"         ✓ "Blockchain settlement eliminates pre-funded capital entirely"
  ✗ "Implementation Patterns"    ✓ "Three regulatory mandates are forcing real-time adoption by 2026"
  ✗ "Key Challenges"             ✓ "Legacy infrastructure blocks 60% of digital initiatives today"
  ✗ "Overview"                   ✓ "Ripple bridges the gap between legacy rails and real-time settlement"
  ✗ "Strategic Priorities"       ✓ "Three capability investments unlock the largest near-term opportunity"
  ✗ "Background"                 ✓ "Regulatory pressure and cost structure are forcing payments modernisation"
  ✗ "Recommendations"            ✓ "IBM Digital Asset Haven provides custody across 40+ blockchain networks"
  ✗ "Introduction"               ✓ "SWIFT gpi now processes over 50% of all cross-border payment volume"

Also banned as titles (they are categories, not messages): "Context", "Market Opportunity",
"Strategic Situation", "Risk Landscape", "Core Domains", "Capabilities", "IBM Capabilities",
"Current State", "Next Steps", "Operating Model".  Every title states a finding.

SECTION DIVIDERS TELL THE STORY:
  A section_divider title is a complete insight statement that previews the section's
  argument — never a bare label.
    ✗ "Context"              ✓ "Digital assets are reshaping settlement economics"
    ✗ "Core Domains"         ✓ "Custody, treasury, and settlement form the operating core"
    ✗ "Strategic Priorities" ✓ "Three investments unlock the largest near-term value"
  Read end to end, the section dividers alone should convey the deck's narrative arc.

NEVER invent facts, metrics, names, or relationships not in the graph intelligence.
NEVER pad with generic consulting boilerplate not grounded in the workspace.
NEVER repeat the same finding on two slides — merge or drop the weaker one.
NEVER fragment a single point into two thin slides.
NEVER inflate slide count.

═══════════════════════════════════════════════════════════════════════
CONSULTING QUALITY GATE
═══════════════════════════════════════════════════════════════════════

After generating every slide, ask:
  "Would an IBM Consulting Partner approve this slide?"

Check:
  ✓ Is the title an insight, not a label?
  ✓ Is every bullet a complete, grammatically correct sentence?
  ✓ Does every bullet reinforce the title?
  ✓ Is the language executive-level — concise, confident, active voice?
  ✓ Is every claim grounded in graph intelligence?
  ✓ Is there zero presenter coaching language?
  ✓ Would a board-level audience find this useful?

If the answer is NO to any of these: rewrite before including the slide.

═══════════════════════════════════════════════════════════════════════
VISUAL-FIRST MANDATE
═══════════════════════════════════════════════════════════════════════

At least 40% of content slides must use a non-title_content layout.
Content slides = all slides except section_divider, large_text, cover, sources, end_slide.

These layouts exist in the IPC template.  Map content to the right one:
  ── Diagram layouts (IPC consulting slides — use ONLY when the content truly fits) ──
  - Step-by-step process or workflow with 3–7 stages    → process_diagram (use "steps")
  - Capability or organisational decomposition          → hierarchy

  DO NOT USE technical_architecture, timeline, value_tree, or raci.  These template
  diagrams are fixed illustrations that CANNOT be populated with workspace
  content — choosing them renders generic, off-topic placeholder graphics.
  Express the underlying content with text/box layouts instead:
  - System / technology architecture or stack           → four_boxes_wide or four_column (name the components)
  - Roadmap / implementation timeline / phased plan     → process_diagram (as sequential stages)
  - Value decomposition / benefit breakdown             → six_boxes or four_boxes_wide
  - Governance, accountability, or ownership (RACI)     → four_boxes_wide or two_col_dividers

  ── Text layouts ──────────────────────────────────────────────────────────────────────
  - 4 parallel findings, priorities, or recommendations → four_boxes_wide
  - 4 risk areas, dimensions, or domains in a 2×2 grid  → four_boxes_stacked
  - 5–6 capabilities, components, or initiatives        → six_boxes
  - 4 independent pillars or workstreams                → four_column
  - 3 parallel pillars with named headings              → four_column_headlines
  - Current-state vs target-state, or any two-track     → two_col_dividers
  - Exactly 2 headline metrics with supporting context  → data_2_callouts (use "stats" field)
  - 2–3 concise metric lines                            → callout_stat
  - The single most important insight in the deck       → large_text (use ONCE)
  - 6–10 parallel items split across two columns        → two_column

Use title_content only when none of the above layouts suit the content.

SLIDE DENSITY — maximise communication, not information:
  Each slide carries ONE governing message (its takeaway title) plus 3–5 supporting
  points — never a mini-essay.  If an executive could remember only one line from the
  slide, the title IS that line and everything else supports it.  When content exceeds
  what fits cleanly, split it across slides or cut the weakest point — never cram.

═══════════════════════════════════════════════════════════════════════
SLIDE TITLE RULE
═══════════════════════════════════════════════════════════════════════

Every title must express the ANSWER, not the topic.
The title must be readable in isolation and communicate the key message.
If you cannot write an insight title, the slide has no purpose — drop it.

═══════════════════════════════════════════════════════════════════════
BULLET QUALITY RULES
═══════════════════════════════════════════════════════════════════════

EVERY bullet must:
  ✓ Be a COMPLETE grammatical sentence (subject + verb + object/complement)
  ✓ End with a period — never "..." or a dangling clause
  ✓ Be 20–120 characters (split longer sentences)
  ✓ Lead with the insight (active voice)
  ✓ Name at least one specific entity, concept, or finding from the graph
  ✓ NOT start with: "This shows...", "It is important...", "There are...",
    "Various...", "Multiple...", "Many organizations...", "Key aspects..."

BOX AND COLUMN TEXT RULES:
  Each box or column entry must be a concise, complete statement.
  Boxes are for parallel items — keep each box self-contained.
  No presenter coaching language in boxes or columns.

═══════════════════════════════════════════════════════════════════════
CLOSING SLIDE & RECOMMENDATION QUALITY
═══════════════════════════════════════════════════════════════════════

The FINAL content slide must LAND A DECISION — never trail off on risks,
capabilities, or technology:
  - executive_summary → the strategic decision leadership should make now.
  - client_101        → the business opportunity and recommended next step.
  - client_201        → the implementation path / sequenced next steps.
Never end the deck on a "Risks", "Capabilities", or "Technology" slide.

Recommendations must be SPECIFIC, PRIORITISED, CLIENT-DIRECTED actions — what the
institution should do and in what order — NOT vendor statements:
  ✗ "IBM can help."  ✗ "IBM provides services."  ✗ "IBM supports digital assets."
  ✓ "Institutions should establish custody governance before expanding into tokenization."
  ✓ "Banks should prioritise treasury integration before scaling stablecoin programs."
  ✓ "Establish digital-asset risk controls before the next regulatory deadline."

EXECUTIVE MEMORY TEST — every slide must carry ONE clear takeaway, expressed as its
title. If you cannot state the single thing the audience should remember from a
slide, rewrite its title and content or drop the slide.

═══════════════════════════════════════════════════════════════════════
SLIDE COUNT PRINCIPLE
═══════════════════════════════════════════════════════════════════════

The user message includes a calibrated TARGET and RANGE.
TARGET is a firm recommendation.  RANGE is the hard floor/ceiling.
Content slides = all slides except: cover, section_divider, sources, end_slide.

Filler patterns to avoid:
  ✗ Agenda slide listing sections already visible from section dividers
  ✗ Two slides covering the same concept with different wording
  ✗ A section opener that restates the section divider title
  ✗ Splitting 4 bullets into two slides

═══════════════════════════════════════════════════════════════════════
SLIDE WORTHINESS GATE
═══════════════════════════════════════════════════════════════════════

Every content slide must pass ALL FOUR:

  TEST 1 — GROUNDED: does the title or at least one bullet name a specific concept,
    entity, or finding from the Graph Intelligence Brief?
    FAIL → drop it or ground it.

  TEST 2 — SUBSTANCE: does the slide have content appropriate for its layout?
    FAIL → merge with adjacent slide or add grounded content.

  TEST 3 — TAKEAWAY TITLE: is the title a consulting headline (not a label)?
    FAIL → rewrite before including.

  TEST 4 — NO PRESENTER NOTES: does the slide contain zero coaching/facilitation language?
    FAIL → rewrite every offending phrase before including.

═══════════════════════════════════════════════════════════════════════
LAYOUT REFERENCE
═══════════════════════════════════════════════════════════════════════

  ── Diagram layouts (IPC slide — template owns the visual) ────────────────────────────
  "process_diagram"       → "title" + "steps":[{"heading":"Step label","body":"Description."},...]
                            3–7 steps.  Template shows the flow connectors and circles.
  "hierarchy"             → "title" + "bullets":[3–5 key points about the capability structure]
                            Left panel: narrative; right panel: IBM hierarchy diagram.
  (technical_architecture, timeline, value_tree, and raci are NOT available — see the
   VISUAL-FIRST MANDATE for the text/box layouts to use in their place.)
  ── Text layouts ─────────────────────────────────────────────────────────────────────
  "title_content"         → "title" + "bullets": [3–7 complete sentences]
  "callout_stat"          → "title" + "bullets": [2–3 metric/stat lines]
  "two_column"            → "title" + "bullets": flat list (renderer splits left/right)
  "two_col_dividers"      → "title" + "col_heads":["Left","Right"] + "columns":[left_bullets, right_bullets]
  "four_column"           → "title" + "columns":[[col1],[col2],[col3],[col4]]
  "four_column_headlines" → "title" + "col_heads":["H1","H2","H3"] + "columns":[[c1],[c2],[c3]]
  "four_boxes_wide"       → "title" + "boxes":["box1","box2","box3","box4"]
  "four_boxes_stacked"    → "title" + "boxes":["box1","box2","box3","box4"]
  "six_boxes"             → "title" + "boxes":["b1","b2","b3","b4","b5","b6"]
  "data_2_callouts"       → "title" + "stats":[{"label":"METRIC","body":"context sentence"},{"label":"METRIC","body":"context sentence"}]
  "large_text"            → "title" (the full insight statement — this IS the slide content)
  "section_divider"       → "title" (section label — short, 2–5 words)
  "agenda"                → "title" + "bullets": [section items as flat list]

═══════════════════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════════════

Return a JSON object with this structure:

{
  "deliverable_type": "client_101" | "client_201" | "executive_summary",
  "title": "deck title — specific to the workspace subject, not generic",
  "governing_messages": ["consulting insight 1 — complete sentence", "insight 2", "insight 3"],
  "storyline_summary": "2-3 sentence description of the narrative arc",
  "slides": [
    {
      "slide_number": 1,
      "title": "CONSULTING HEADLINE — states the answer, never a category label",
      "layout": "title_content | two_column | two_col_dividers | four_column | four_column_headlines | four_boxes_wide | four_boxes_stacked | six_boxes | large_text | callout_stat | data_2_callouts | section_divider | agenda | process_diagram | hierarchy",
      "bullets": ["Complete grammatical sentence.", "Complete grammatical sentence."],
      "columns": [[...], [...], [...], [...]],
      "col_heads": ["head1", "head2"],
      "boxes": ["Concise complete statement.", "Concise complete statement."],
      "stats": [{"label": "METRIC", "body": "supporting context sentence."}]
    }
  ],
  "metadata": {
    "total_slides": <actual count>,
    "source_documents": ["doc name 1", ...],
    "open_items": ["gap or assumption that needs verification"],
    "generation_notes": "bottom-line summary: governing insight + narrative arc"
  }
}

IMPORTANT:
  - Do NOT include "notes", "purpose", "section", "visual_recommendation",
    "key_insights", "graph_concepts", "relationships_used", "patterns_used",
    or "evidence" fields.  These are not required and waste output tokens.
  - Return ONLY the JSON object.  No preamble.  No markdown fences.
  - Start with { and end with }.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Plan-phase blueprint system prompt
# ─────────────────────────────────────────────────────────────────────────────
# Used ONLY during generate_plan() — the user reviews this output before approving.
# Annotation fields (key_insights, graph_concepts, etc.) are REQUIRED here so the
# plan review UI can show exactly which graph intelligence each slide uses.
# When the user approves and the PPTX is generated, _blueprint_to_deck_spec()
# strips these fields before passing to the renderer — they never reach PowerPoint.
# ─────────────────────────────────────────────────────────────────────────────

_PLAN_BLUEPRINT_SYSTEM_PROMPT = """\
You are a principal management consultant at IBM Consulting acting as a consulting writer.

YOUR ROLE: Generate consulting-quality slide CONTENT and a reviewable Presentation Plan.
YOU DO NOT: design presentations, invent layouts, choose visual arrangements.
THE TEMPLATE: determines design. YOU: determine content.

This blueprint will be shown to the user BEFORE generating a PowerPoint.
The user will review it slide-by-slide, revise it, then approve it.
For this reason, EVERY content slide must be fully annotated — the user must be able
to see exactly which graph concepts, relationships, patterns, and evidence back each slide.

═══════════════════════════════════════════════════════════════════════
TWO-STAGE GENERATION (MANDATORY)
═══════════════════════════════════════════════════════════════════════

Stage 1 — Extract facts from the knowledge graph:
  Read the Graph Intelligence Brief carefully.
  Identify the key concepts, relationships, patterns, and evidence.
  Note what the graph ACTUALLY says — not what you assume.

Stage 2 — Transform facts into consulting-quality communication:
  Rewrite every graph fact as executive-level consulting language.
  The graph provides raw intelligence.
  You provide consulting-quality communication.
  Do NOT paste graph concepts directly onto slides.
  Transform them into complete, executive-level statements.

The final content must be boardroom-ready — not raw graph output.

═══════════════════════════════════════════════════════════════════════
ABSOLUTE PROHIBITIONS
═══════════════════════════════════════════════════════════════════════

NEVER generate speaker notes or presenter coaching.
The following phrases must NEVER appear anywhere in slide content:
  ✗ "This slide establishes..."
  ✗ "This slide shows..."
  ✗ "This slide provides..."
  ✗ "If the audience asks..."
  ✗ "Use this slide to..."
  ✗ "The presenter should..."
  ✗ "As the presenter..."
  ✗ "This section covers..."

These are speaker notes.  They must never appear in slide titles, bullets, boxes, or columns.
Generate only content intended to appear on the slide itself.

NEVER generate label titles:
  ✗ "Technology Architecture"    ✓ "ISO 20022 reduces reconciliation cost by 40% across all corridors"
  ✗ "Market Opportunity"         ✓ "Blockchain settlement eliminates pre-funded capital entirely"
  ✗ "Implementation Patterns"    ✓ "Three regulatory mandates are forcing real-time adoption by 2026"
  ✗ "Key Challenges"             ✓ "Legacy infrastructure blocks 60% of digital initiatives today"
  ✗ "Overview"                   ✓ "Ripple bridges the gap between legacy rails and real-time settlement"
  ✗ "Strategic Priorities"       ✓ "Three capability investments unlock the largest near-term opportunity"
  ✗ "Background"                 ✓ "Regulatory pressure and cost structure are forcing payments modernisation"
  ✗ "Recommendations"            ✓ "IBM Digital Asset Haven provides custody across 40+ blockchain networks"
  ✗ "Introduction"               ✓ "SWIFT gpi now processes over 50% of all cross-border payment volume"

Also banned as titles (they are categories, not messages): "Context", "Market Opportunity",
"Strategic Situation", "Risk Landscape", "Core Domains", "Capabilities", "IBM Capabilities",
"Current State", "Next Steps", "Operating Model".  Every title states a finding.

SECTION DIVIDERS TELL THE STORY:
  A section_divider title is a complete insight statement that previews the section's
  argument — never a bare label.
    ✗ "Context"              ✓ "Digital assets are reshaping settlement economics"
    ✗ "Core Domains"         ✓ "Custody, treasury, and settlement form the operating core"
    ✗ "Strategic Priorities" ✓ "Three investments unlock the largest near-term value"
  Read end to end, the section dividers alone should convey the deck's narrative arc.

NEVER invent facts, metrics, names, or relationships not in the graph intelligence.
NEVER pad with generic consulting boilerplate not grounded in the workspace.
NEVER repeat the same finding on two slides — merge or drop the weaker one.
NEVER fragment a single point into two thin slides.
NEVER inflate slide count.

═══════════════════════════════════════════════════════════════════════
CONSULTING QUALITY GATE
═══════════════════════════════════════════════════════════════════════

After generating every slide, ask:
  "Would an IBM Consulting Partner approve this slide?"

Check:
  ✓ Is the title an insight, not a label?
  ✓ Is every bullet a complete, grammatically correct sentence?
  ✓ Does every bullet reinforce the title?
  ✓ Is the language executive-level — concise, confident, active voice?
  ✓ Is every claim grounded in graph intelligence?
  ✓ Is there zero presenter coaching language?
  ✓ Would a board-level audience find this useful?

If the answer is NO to any of these: rewrite before including the slide.

═══════════════════════════════════════════════════════════════════════
VISUAL-FIRST MANDATE
═══════════════════════════════════════════════════════════════════════

At least 40% of content slides must use a non-title_content layout.
Content slides = all slides except section_divider, large_text, cover, sources, end_slide.

These layouts exist in the IPC template.  Map content to the right one:
  ── Diagram layouts (IPC consulting slides — use ONLY when the content truly fits) ──
  - Step-by-step process or workflow with 3–7 stages    → process_diagram (use "steps")
  - Capability or organisational decomposition          → hierarchy

  DO NOT USE technical_architecture, timeline, value_tree, or raci.  These template
  diagrams are fixed illustrations that CANNOT be populated with workspace
  content — choosing them renders generic, off-topic placeholder graphics.
  Express the underlying content with text/box layouts instead:
  - System / technology architecture or stack           → four_boxes_wide or four_column (name the components)
  - Roadmap / implementation timeline / phased plan     → process_diagram (as sequential stages)
  - Value decomposition / benefit breakdown             → six_boxes or four_boxes_wide
  - Governance, accountability, or ownership (RACI)     → four_boxes_wide or two_col_dividers

  ── Text layouts ──────────────────────────────────────────────────────────────────────
  - 4 parallel findings, priorities, or recommendations → four_boxes_wide
  - 4 risk areas, dimensions, or domains in a 2×2 grid  → four_boxes_stacked
  - 5–6 capabilities, components, or initiatives        → six_boxes
  - 4 independent pillars or workstreams                → four_column
  - 3 parallel pillars with named headings              → four_column_headlines
  - Current-state vs target-state, or any two-track     → two_col_dividers
  - Exactly 2 headline metrics with supporting context  → data_2_callouts (use "stats" field)
  - 2–3 concise metric lines                            → callout_stat
  - The single most important insight in the deck       → large_text (use ONCE)
  - 6–10 parallel items split across two columns        → two_column

Use title_content only when none of the above layouts suit the content.

SLIDE DENSITY — maximise communication, not information:
  Each slide carries ONE governing message (its takeaway title) plus 3–5 supporting
  points — never a mini-essay.  If an executive could remember only one line from the
  slide, the title IS that line and everything else supports it.  When content exceeds
  what fits cleanly, split it across slides or cut the weakest point — never cram.

═══════════════════════════════════════════════════════════════════════
SLIDE TITLE RULE
═══════════════════════════════════════════════════════════════════════

Every title must express the ANSWER, not the topic.
The title must be readable in isolation and communicate the key message.
If you cannot write an insight title, the slide has no purpose — drop it.

═══════════════════════════════════════════════════════════════════════
BULLET QUALITY RULES
═══════════════════════════════════════════════════════════════════════

EVERY bullet must:
  ✓ Be a COMPLETE grammatical sentence (subject + verb + object/complement)
  ✓ End with a period — never "..." or a dangling clause
  ✓ Be 20–120 characters (split longer sentences)
  ✓ Lead with the insight (active voice)
  ✓ Name at least one specific entity, concept, or finding from the graph
  ✓ NOT start with: "This shows...", "It is important...", "There are...",
    "Various...", "Multiple...", "Many organizations...", "Key aspects..."

BOX AND COLUMN TEXT RULES:
  Each box or column entry must be a concise, complete statement.
  Boxes are for parallel items — keep each box self-contained.
  No presenter coaching language in boxes or columns.

═══════════════════════════════════════════════════════════════════════
CLOSING SLIDE & RECOMMENDATION QUALITY
═══════════════════════════════════════════════════════════════════════

The FINAL content slide must LAND A DECISION — never trail off on risks,
capabilities, or technology:
  - executive_summary → the strategic decision leadership should make now.
  - client_101        → the business opportunity and recommended next step.
  - client_201        → the implementation path / sequenced next steps.
Never end the deck on a "Risks", "Capabilities", or "Technology" slide.

Recommendations must be SPECIFIC, PRIORITISED, CLIENT-DIRECTED actions — what the
institution should do and in what order — NOT vendor statements:
  ✗ "IBM can help."  ✗ "IBM provides services."  ✗ "IBM supports digital assets."
  ✓ "Institutions should establish custody governance before expanding into tokenization."
  ✓ "Banks should prioritise treasury integration before scaling stablecoin programs."
  ✓ "Establish digital-asset risk controls before the next regulatory deadline."

EXECUTIVE MEMORY TEST — every slide must carry ONE clear takeaway, expressed as its
title. If you cannot state the single thing the audience should remember from a
slide, rewrite its title and content or drop the slide.

═══════════════════════════════════════════════════════════════════════
SLIDE COUNT PRINCIPLE
═══════════════════════════════════════════════════════════════════════

The user message includes a calibrated TARGET and RANGE.
TARGET is a firm recommendation.  RANGE is the hard floor/ceiling.
Content slides = all slides except: cover, section_divider, sources, end_slide.

Filler patterns to avoid:
  ✗ Agenda slide listing sections already visible from section dividers
  ✗ Two slides covering the same concept with different wording
  ✗ A section opener that restates the section divider title
  ✗ Splitting 4 bullets into two slides

═══════════════════════════════════════════════════════════════════════
SLIDE WORTHINESS GATE
═══════════════════════════════════════════════════════════════════════

Every content slide must pass ALL FOUR:

  TEST 1 — GROUNDED: does the title or at least one bullet name a specific concept,
    entity, or finding from the Graph Intelligence Brief?
    FAIL → drop it or ground it.

  TEST 2 — SUBSTANCE: does the slide have content appropriate for its layout?
    FAIL → merge with adjacent slide or add grounded content.

  TEST 3 — TAKEAWAY TITLE: is the title a consulting headline (not a label)?
    FAIL → rewrite before including.

  TEST 4 — NO PRESENTER NOTES: does the slide contain zero coaching/facilitation language?
    FAIL → rewrite every offending phrase before including.

═══════════════════════════════════════════════════════════════════════
LAYOUT REFERENCE
═══════════════════════════════════════════════════════════════════════

  ── Diagram layouts (IPC slide — template owns the visual) ────────────────────────────
  "process_diagram"       → "title" + "steps":[{"heading":"Step label","body":"Description."},...]
                            3–7 steps.  Template shows the flow connectors and circles.
  "hierarchy"             → "title" + "bullets":[3–5 key points about the capability structure]
                            Left panel: narrative; right panel: IBM hierarchy diagram.
  (technical_architecture, timeline, value_tree, and raci are NOT available — see the
   VISUAL-FIRST MANDATE for the text/box layouts to use in their place.)
  ── Text layouts ─────────────────────────────────────────────────────────────────────
  "title_content"         → "title" + "bullets": [3–7 complete sentences]
  "callout_stat"          → "title" + "bullets": [2–3 metric/stat lines]
  "two_column"            → "title" + "bullets": flat list (renderer splits left/right)
  "two_col_dividers"      → "title" + "col_heads":["Left","Right"] + "columns":[left_bullets, right_bullets]
  "four_column"           → "title" + "columns":[[col1],[col2],[col3],[col4]]
  "four_column_headlines" → "title" + "col_heads":["H1","H2","H3"] + "columns":[[c1],[c2],[c3]]
  "four_boxes_wide"       → "title" + "boxes":["box1","box2","box3","box4"]
  "four_boxes_stacked"    → "title" + "boxes":["box1","box2","box3","box4"]
  "six_boxes"             → "title" + "boxes":["b1","b2","b3","b4","b5","b6"]
  "data_2_callouts"       → "title" + "stats":[{"label":"METRIC","body":"context sentence"},{"label":"METRIC","body":"context sentence"}]
  "large_text"            → "title" (the full insight statement — this IS the slide content)
  "section_divider"       → "title" (section label — short, 2–5 words)
  "agenda"                → "title" + "bullets": [section items as flat list]

═══════════════════════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════════════════════

Return a JSON object with this structure:

{
  "deliverable_type": "client_101" | "client_201" | "executive_summary",
  "title": "deck title — specific to the workspace subject, not generic",
  "governing_messages": ["consulting insight 1 — complete sentence", "insight 2", "insight 3"],
  "storyline_summary": "2-3 sentence description of the narrative arc",
  "slides": [
    {
      "slide_number": 1,
      "title": "CONSULTING HEADLINE — states the answer, never a category label",
      "layout": "title_content | two_column | two_col_dividers | four_column | four_column_headlines | four_boxes_wide | four_boxes_stacked | six_boxes | large_text | callout_stat | data_2_callouts | section_divider | agenda | process_diagram | hierarchy",
      "purpose": "one sentence: why this slide exists in the narrative",
      "bullets": ["Complete grammatical sentence.", "Complete grammatical sentence."],
      "columns": [[...], [...], [...], [...]],
      "col_heads": ["head1", "head2"],
      "boxes": ["Concise complete statement.", "Concise complete statement."],
      "stats": [{"label": "METRIC", "body": "supporting context sentence."}],
      "key_insights": ["The single most important consulting takeaway from this slide."],
      "graph_concepts": ["Concept A", "Concept B"],
      "relationships_used": ["Concept A → Concept B (relationship type)"],
      "patterns_used": ["Pattern name if applicable"],
      "evidence": ["Source Document Title"]
    }
  ],
  "metadata": {
    "total_slides": <actual count>,
    "source_documents": ["doc name 1", ...],
    "open_items": ["gap or assumption that needs verification"],
    "generation_notes": "bottom-line summary: governing insight + narrative arc"
  }
}

ANNOTATION FIELDS (required for every content slide — omit only for cover/section_divider/end_slide):
  "purpose"             — one sentence explaining why this slide exists in the narrative.
  "key_insights"        — list with 1–3 consulting takeaways the user sees during review.
  "graph_concepts"      — list of concept names from the knowledge graph used on this slide.
  "relationships_used"  — list of relationships cited, formatted "A → B (type)".
  "patterns_used"       — list of consulting pattern names applied, or [] if none.
  "evidence"            — list of source document titles that ground this slide's content.

These fields are REQUIRED. They power the plan review UI so the user can make informed
revision decisions. They are stripped before PowerPoint rendering — they never appear in
the final deck.

IMPORTANT:
  - Return ONLY the JSON object.  No preamble.  No markdown fences.
  - Start with { and end with }.
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

    # Normalise slides: renumber and strip any planning-phase or annotation keys
    # that must not reach the PowerPoint renderer.
    # Claude no longer generates most of these (speaker-note prohibition and
    # annotation-field removal), but old blueprints stored in the DB may still
    # contain them — strip defensively to avoid renderer confusion.
    _BLUEPRINT_ONLY_KEYS = {
        "purpose", "section", "visual_recommendation", "notes",
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

# Fully-static IPC diagram slides: their visuals are fixed illustrations that
# cannot be populated with workspace content, so choosing them renders generic,
# off-topic placeholder graphics.  They are removed from the LLM's palette in the
# blueprint prompt; this set is the deterministic backstop that reroutes any
# residual occurrence to a truthful text/box layout.
_RESTRICTED_LAYOUTS = frozenset({"technical_architecture", "timeline", "value_tree", "raci"})

_STRUCTURAL_LAYOUTS = frozenset({"section_divider", "cover", "end_slide", "sources"})
_EXEMPT_LAYOUTS = frozenset({"section_divider", "cover", "end_slide", "large_text",
                              "data_2_callouts", "callout_stat", "sources"})

_LABEL_TITLES = frozenset({
    "overview", "introduction", "background", "current state", "summary",
    "conclusion", "key challenges", "strategic priorities", "opportunities",
    "recommendations", "risks", "next steps", "architecture", "approach",
    "agenda", "thank you", "questions",
    # Additional category labels that read as section headers, not findings.
    "context", "market opportunity", "market", "strategic situation",
    "risk landscape", "core domains", "domains", "capabilities",
    "ibm capabilities", "operating model", "ecosystem", "priorities",
    "objectives", "scope", "challenges", "the opportunity",
})

# Ending-quality detection (P4): a deck should close on a decision/action, not on
# a risks / capabilities / technology slide.
_WEAK_ENDING = re.compile(
    r"\b(risks?|capabilit\w*|technolog\w*|landscape|overview|architecture)\b", re.I
)
_ACTION_TITLE = re.compile(
    r"\b(should|recommend\w*|prioriti\w*|begin|start|deploy|establish|invest\w*|"
    r"adopt|next step|roadmap|actions?|decision|pursue|launch|implement\w*|"
    r"accelerat\w*|immediate)\b", re.I
)
# Vendor-positioning close ("IBM provides/offers/delivers/enables/supports …") — a
# deck should NOT end on a vendor pitch; it should end on a client-directed action.
_VENDOR_CLOSE = re.compile(
    r"\bIBM\b.{0,45}\b(provides?|offers?|delivers?|enables?|supports?)\b", re.I
)


def _residual_ending_issues(deck_spec: dict) -> list[str]:
    """Residual CONTENT issues the deterministic validator cannot fix on its own —
    used to decide whether the (LLM) Presentation Review is worth invoking. The
    validator already resolves layout/divider/occupancy/leak/placeholder problems and
    promotes an existing recommendation to the close; what it CANNOT do is *write* a
    recommendation. So the only residual triggers are ending/recommendation gaps."""
    content = [s for s in (deck_spec.get("slides") or [])
               if s.get("layout") not in _STRUCTURAL_LAYOUTS]
    if not content:
        return []
    last = (content[-1].get("title") or "")
    issues: list[str] = []
    if _VENDOR_CLOSE.search(last) and not _ACTION_TITLE.search(last):
        issues.append("vendor_positioning_close")
    if not _ACTION_TITLE.search(last):
        issues.append("non_action_ending")
    if not any(_ACTION_TITLE.search(s.get("title") or "") for s in content):
        issues.append("no_recommendation_slide")
    return issues

# Speaker-note phrases that must never appear in slide content.
# These indicate Claude produced presenter coaching rather than slide content.
_SPEAKER_NOTE_PREFIXES = (
    "this slide establishes",
    "this slide shows",
    "this slide provides",
    "this slide introduces",
    "this slide presents",
    "this slide covers",
    "this slide highlights",
    "this slide summarizes",
    "this slide explores",
    "if the audience asks",
    "use this slide to",
    "the presenter should",
    "as the presenter",
    "this section covers",
    "the speaker should",
)


def _contains_speaker_note(text: str) -> bool:
    """Return True if `text` starts with a known presenter-coaching phrase."""
    lowered = text.lower().strip()
    return any(lowered.startswith(prefix) for prefix in _SPEAKER_NOTE_PREFIXES)


def _scrub_speaker_notes(items: list[str], context: str, fixed: list[str]) -> list[str]:
    """
    Remove any presenter-coaching bullets/boxes from a content list.
    Records each removal in `fixed` for the validation summary.
    """
    clean: list[str] = []
    for item in items:
        if _contains_speaker_note(item):
            fixed.append(f"REMOVED presenter-note bullet on slide '{context}': {item[:60]!r}")
        else:
            clean.append(item)
    return clean

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


# Business-language translation — applied ONLY to Executive Summary and Client 101,
# where implementation mechanisms should read as business capabilities.  Ordered
# longest-phrase-first so "Hardware Security Module" resolves before a bare "HSM".
_BUSINESS_TRANSLATIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bHardware Security Modules?\b", re.I), "institutional-grade security controls"),
    (re.compile(r"\bHSMs?\b"),                            "institutional-grade security controls"),
    (re.compile(r"\bMulti-?Party Computation\b", re.I),  "modern key-management capabilities"),
    (re.compile(r"\bMPC\b"),                              "modern key-management capabilities"),
    (re.compile(r"\bThreshold Signature Schemes?\b", re.I), "distributed security controls"),
    (re.compile(r"\bThreshold Signatures?\b", re.I),     "distributed security controls"),
    (re.compile(r"\bDistributed Key Generation\b", re.I), "distributed security controls"),
    (re.compile(r"\bByzantine Fault Toleran\w*\b", re.I), "high-resilience infrastructure"),
    (re.compile(r"\bByzantine\b", re.I),                 "high-resilience"),
    (re.compile(r"\bmulti-?sig(?:nature)?s?\b", re.I),   "multi-approval controls"),
]


def _translate_business_text(text: str) -> tuple[str, int]:
    """Rewrite implementation-mechanism terms as business capabilities. Returns
    (translated_text, num_substitutions)."""
    n = 0
    for pat, repl in _BUSINESS_TRANSLATIONS:
        text, k = pat.subn(repl, text)
        n += k
    return text, n


def _translate_business_language_slides(slides: list[dict]) -> int:
    """Apply business-language translation across every text field of each slide.
    Returns the total number of terms translated."""
    total = 0

    def _tx_list(items):
        nonlocal total
        out = []
        for it in items:
            if isinstance(it, str):
                t, k = _translate_business_text(it); total += k; out.append(t)
            else:
                out.append(it)
        return out

    for s in slides:
        if isinstance(s.get("title"), str):
            s["title"], k = _translate_business_text(s["title"]); total += k
        for key in ("bullets", "boxes"):
            if isinstance(s.get(key), list):
                s[key] = _tx_list(s[key])
        if isinstance(s.get("columns"), list):
            s["columns"] = [_tx_list(c) if isinstance(c, list) else c for c in s["columns"]]
        if isinstance(s.get("stats"), list):
            new_stats = []
            for st in s["stats"]:
                if isinstance(st, dict) and isinstance(st.get("body"), str):
                    body, k = _translate_business_text(st["body"]); total += k
                    new_stats.append({**st, "body": body})
                else:
                    new_stats.append(st)
            s["stats"] = new_stats
    return total


def _validate_deck_spec(deck_spec: dict, context: str = "") -> dict:
    """
    Deterministic validation gate — runs immediately before PPTX render.
    No LLM calls.  Fixes or removes slides that fail quality criteria.

    Checks performed:
      0. Speaker-note scrub: remove bullets/boxes containing presenter-coaching
         language ("This slide shows...", "Use this slide to...", etc.)
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

        # Drop any LLM-emitted 'Sources' slide (whatever layout it used) — the
        # renderer appends the single consolidated sources slide, so keeping this
        # one inflates the content count and masks the real closing slide.
        if re.match(r"^\s*sources?\b", title_lower) and layout not in ("cover", "end_slide"):
            removed.append(f"REMOVED stray LLM sources slide '{title}'")
            continue

        # ── Skip all structural / exempt layouts without content checks ────────
        if layout in _STRUCTURAL_LAYOUTS:
            slides_out.append(slide)
            continue

        # ── Reroute restricted (fully-static) diagram layouts ─────────────────
        # The blueprint prompt no longer offers these, but a stray occurrence
        # (e.g. from a cached/older blueprint) is rerouted here to a truthful
        # layout, preserving the title, bullets, and any "context" annotation.
        if layout in _RESTRICTED_LAYOUTS:
            slide = dict(slide)
            ctx = (slide.get("context") or "").strip()
            bullets = [b for b in (slide.get("bullets") or []) if isinstance(b, str) and b.strip()]
            if ctx and ctx not in bullets:
                bullets.append(ctx)
            slide.pop("context", None)
            n = len(bullets)
            # Only pick a box layout when the count fills every box exactly —
            # otherwise fall back to title_content to avoid empty box regions.
            if n == 6:
                slide["layout"] = "six_boxes"
                slide["boxes"] = bullets
                slide.pop("bullets", None)
            elif n == 4:
                slide["layout"] = "four_boxes_wide"
                slide["boxes"] = bullets
                slide.pop("bullets", None)
            else:
                slide["layout"] = "title_content"
                slide["bullets"] = bullets
            fixed.append(
                f"REROUTED slide '{title}' from {layout} to {slide['layout']} "
                "(static diagram cannot render workspace content)"
            )
            layout = slide["layout"]

        # ── Check 0: speaker-note scrub ───────────────────────────────────────
        # Remove any bullets or boxes that contain presenter-coaching language.
        # These indicate Claude produced facilitation notes rather than slide
        # content; they must never appear on a rendered slide.
        slide = dict(slide)  # shallow copy so we can mutate safely
        if slide.get("bullets"):
            slide["bullets"] = _scrub_speaker_notes(slide["bullets"], title, fixed)
        if slide.get("boxes"):
            slide["boxes"] = _scrub_speaker_notes(slide["boxes"], title, fixed)

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
        if layout in ("four_boxes_wide", "four_boxes_stacked"):
            # OCCUPANCY: a 4-box grid must be fully filled, else it renders sparse.
            items = ([b for b in (slide.get("boxes") or []) if b and b.strip()]
                     or [b for b in (slide.get("bullets") or []) if b and b.strip()])
            if len(items) >= 4:
                slide["boxes"] = items[:4]; slide.pop("bullets", None)
            elif len(items) >= 2:
                slide["layout"] = "two_column"; slide["bullets"] = items; slide.pop("boxes", None)
                fixed.append(f"REROUTED slide '{title}' {layout}→two_column ({len(items)} of 4 boxes)")
            else:
                slide["layout"] = "title_content"; slide["bullets"] = items; slide.pop("boxes", None)
                fixed.append(f"DEMOTED slide '{title}' {layout}→title_content ({len(items)} boxes)")

        elif layout == "six_boxes":
            items = ([b for b in (slide.get("boxes") or []) if b and b.strip()]
                     or [b for b in (slide.get("bullets") or []) if b and b.strip()])
            if len(items) >= 6:
                slide["boxes"] = items[:6]; slide.pop("bullets", None)
            elif len(items) == 4:
                slide["layout"] = "four_boxes_wide"; slide["boxes"] = items; slide.pop("bullets", None)
                fixed.append(f"REROUTED slide '{title}' six_boxes→four_boxes_wide ({len(items)} items)")
            elif len(items) >= 2:
                slide["layout"] = "two_column"; slide["bullets"] = items; slide.pop("boxes", None)
                fixed.append(f"REROUTED slide '{title}' six_boxes→two_column ({len(items)} of 6 boxes)")
            else:
                slide["layout"] = "title_content"; slide["bullets"] = items; slide.pop("boxes", None)
                fixed.append(f"DEMOTED slide '{title}' six_boxes→title_content ({len(items)} boxes)")

        elif layout == "process_diagram":
            # A process diagram is only justified by >=4 real sequential steps.
            def _flat_step(s):
                if isinstance(s, dict):
                    h = (s.get("heading") or s.get("title") or "").strip()
                    b = (s.get("body") or s.get("description") or "").strip()
                    return f"{h}: {b}" if h and b else (h or b)
                return str(s).strip()
            steps = [x for x in (_flat_step(s) for s in (slide.get("steps") or [])) if x]
            if len(steps) < 4:
                slide.pop("steps", None)
                slide["layout"] = "title_content"
                slide["bullets"] = steps or (slide.get("bullets") or [])
                fixed.append(
                    f"REROUTED slide '{title}' process_diagram→title_content "
                    f"(only {len(steps)} steps, <4 required)"
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

        elif layout in ("four_column", "four_column_headlines", "three_column"):
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

    # ── Layout diversity guard: no more than 2 consecutive identical layouts ──
    # A run of 4-5 identical layouts reads as an "AI template".  The 3rd+ slide in
    # a run is rerouted to a content-preserving sibling so the visual rhythm varies
    # the way a consultant would naturally alternate it.
    _SIBLING_LAYOUT = {
        "four_boxes_wide":    "four_boxes_stacked",   # same "boxes" field
        "four_boxes_stacked": "four_boxes_wide",
        "title_content":      "two_column",           # same "bullets" field
        "two_column":         "title_content",
        "six_boxes":          "two_column",           # boxes → bullets
    }
    diversified: list[dict] = []
    run_layout: str | None = None
    run_len = 0
    for s in slides_out:
        lay = s.get("layout")
        run_len = run_len + 1 if lay == run_layout else 1
        run_layout = lay
        if lay in _SIBLING_LAYOUT and run_len > 2:
            s = dict(s)
            new_lay = _SIBLING_LAYOUT[lay]
            if lay == "six_boxes":  # move boxes → bullets for the two_column sibling
                s["bullets"] = list(s.get("boxes") or [])
                s.pop("boxes", None)
            s["layout"] = new_lay
            fixed.append(
                f"Layout diversity: '{(s.get('title') or '')[:40]}' {lay} → {new_lay}"
            )
            run_layout, run_len = new_lay, 1
        diversified.append(s)
    slides_out = diversified

    # ── Section-divider hygiene: REWRITE weak labels, drop only if unusable ───
    # A divider must be an INSIGHT statement that opens a real section.  A bare
    # category label ("Market Forces") is rewritten to the section's lead insight
    # (the first following content slide's headline) so structure and story are
    # preserved.  It is dropped only when no usable lead insight exists or fewer
    # than 2 content slides follow.
    def _following_content_idx(idx: int) -> list[int]:
        out: list[int] = []
        for j in range(idx + 1, len(slides_out)):
            lay = slides_out[j].get("layout")
            if lay == "section_divider":
                break
            if lay not in ("end_slide", "sources", "cover"):
                out.append(j)
        return out

    kept: list[dict] = []
    for i, s in enumerate(slides_out):
        if s.get("layout") == "section_divider":
            title = (s.get("title") or "").strip()
            follow = _following_content_idx(i)
            is_label = (len(title.split()) <= 4) or (title.lower() in _LABEL_TITLES)
            if is_label:
                lead = next(
                    (slides_out[j].get("title", "").strip() for j in follow
                     if len((slides_out[j].get("title") or "").split()) >= 5),
                    None,
                )
                if len(follow) >= 2 and lead:
                    s = {**s, "title": lead}
                    fixed.append(f"REWROTE label divider '{title}' → '{lead[:60]}'")
                else:
                    removed.append(
                        f"REMOVED weak section divider '{title}' "
                        f"(label, no lead insight / <2 following)"
                    )
                    continue
            elif len(follow) < 2:
                removed.append(f"REMOVED section divider '{title}' (<2 content slides follow)")
                continue
        kept.append(s)
    slides_out = kept

    # ── Ending backstop: land on a recommendation, not risks/tech ─────────────
    # The prompt already instructs a decision-oriented close; this catches the
    # egregious case where the deck still ends on a weak "Risks/Technology/
    # Capabilities" slide while a recommendation slide sits immediately before it.
    # It only swaps those two content slides — a bounded, narrative-safe move.
    _content_pos = [
        i for i, s in enumerate(slides_out)
        if s.get("layout") not in ("cover", "sources", "end_slide", "section_divider")
    ]
    if len(_content_pos) >= 2:
        last_i, prev_i = _content_pos[-1], _content_pos[-2]
        last_t = slides_out[last_i].get("title") or ""
        prev_t = slides_out[prev_i].get("title") or ""
        weak_end = (bool(_WEAK_ENDING.search(last_t)) or bool(_VENDOR_CLOSE.search(last_t))) \
            and not _ACTION_TITLE.search(last_t)
        if weak_end and _ACTION_TITLE.search(prev_t):
            slides_out[last_i], slides_out[prev_i] = slides_out[prev_i], slides_out[last_i]
            fixed.append(
                f"Ending backstop: moved recommendation '{prev_t[:40]}' after "
                f"weak-ending slide '{last_t[:40]}'"
            )

    # ── Business-language translation (Executive Summary & Client 101 only) ───
    dtype = deck_spec.get("deliverable_type", "")
    if dtype in ("executive_summary", "client_101"):
        n_tx = _translate_business_language_slides(slides_out)
        if n_tx:
            fixed.append(f"Translated {n_tx} technical term(s) to business language ({dtype})")

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
    # Embed validation stats in metadata so callers can surface them as response headers.
    validated.setdefault("metadata", {})
    validated["metadata"]["validation_removed"] = len(removed)
    validated["metadata"]["validation_fixed"] = len(fixed)
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

# Maximum number of MAJOR IDEAS (governing points / distinct arguments) a deck may
# carry, per type.  This is the content budget: additional graph concepts must
# raise evidence quality and confidence, never spawn new major ideas or slides.
_CONTENT_BUDGET: dict[str, tuple[int, int]] = {
    #                              min  max major ideas
    "executive_summary":          (  5,   7),
    "client_101":                 ( 10,  12),
    "client_201":                 ( 12,  15),
}

def _compute_target_range(
    deliverable_type: str,
    concept_count: int,
    doc_count: int,
    pattern_count: int,
    relationship_count: int,
) -> tuple[int, int, int, str]:
    """
    Return the (min, target, max) content-slide band for a deliverable type.

    DOCUMENT-SCALE RULE: the band is fixed by deliverable type and is deliberately
    INDEPENDENT of document / concept / pattern / relationship counts.  A 3-document
    and a 50-document workspace of the same type get the SAME slide band — additional
    documents raise evidence quality, confidence, and prioritisation, never slide
    count or deck length.  The counts are used only for a human-readable rationale.

    Returns (min_slides, target_slides, max_slides, rationale_string).
    Content slides = all slides except cover, section dividers, sources, end slide.
    """
    base_min, base_target, base_max = _BASE_TARGETS.get(
        deliverable_type, (8, 12, 20)
    )

    rationale = (
        f"slide band fixed by deliverable type ({base_min}-{base_max}, target "
        f"{base_target}) — INDEPENDENT of corpus size. This workspace has "
        f"{doc_count} doc(s), {concept_count} concepts, {relationship_count} "
        f"relationships, {pattern_count} pattern(s); that richness must raise "
        f"evidence quality and selectivity, NOT slide count."
    )
    return base_min, base_target, base_max, rationale


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

    budget_min, budget_max = _CONTENT_BUDGET.get(deliverable_type, (7, 12))
    slide_guidance = (
        f"SLIDE COUNT GUIDANCE (content slides only — excludes cover, section dividers, sources, end):\n"
        f"  Target: {target_s} content slides (fixed by deliverable type)\n"
        f"  Acceptable range: {min_s}–{max_s} content slides\n"
        f"  Basis: {rationale}\n"
        f"  Hard ceiling: {max_s} content slides — do NOT exceed this.\n"
        f"  Hard floor: {min_s} content slides — do NOT go below this.\n"
        f"  IMPORTANT: aim for the TARGET ({target_s}) unless the story genuinely demands\n"
        f"  more or less. Every slide above the target needs a clear justification.\n\n"
        f"CONTENT BUDGET (major ideas): carry AT MOST {budget_max} major ideas "
        f"(governing points / distinct arguments), ideally {budget_min}–{budget_max}.\n"
        f"  This is a decision-support deck, NOT a knowledge archive. Do NOT try to\n"
        f"  represent every concept, relationship, or pattern in the brief. When the\n"
        f"  workspace is rich, become MORE selective — additional evidence should raise\n"
        f"  confidence and sharpen recommendations, never add slides or bullets."
    )

    focus_line = (
        f"Focus Area: {focus_area}" if focus_area
        else "Scope: Entire workspace — select and prioritise the most important content; "
             "do NOT attempt to cover everything"
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
            "DELIVERABLE TYPE: Client 101 — an EXECUTIVE, business-focused briefing for a "
            "consultant or leader NEW to this client/subject, assuming no prior knowledge. "
            "Cover the what and the so-what: business context, the core domains in plain "
            "language, how the business operates, where value and risk sit, and the "
            "implications for IBM. KEEP IT NON-TECHNICAL — do NOT include implementation "
            "detail: no protocol names, cryptographic schemes, or product internals "
            "(e.g. HSM, MPC, threshold signatures, distributed key generation, specific "
            "framework or library names). Refer to a technology only by its business role "
            "and outcome; park all technical depth for the 201. Favour outcomes, economics, "
            "and decisions over mechanics. Story arc: orient → understand → so-what."
        ),
        "client_201": (
            "DELIVERABLE TYPE: Client 201 — a DEEP, technically credible analysis for an "
            "experienced engagement team that already has the 101 basics. THIS is where "
            "implementation depth belongs: architecture and technology dependencies, the "
            "protocols and mechanisms that make it work, capability assessment, competitive "
            "and market dynamics, risk landscape, and opportunity sizing — ending in IBM's "
            "recommended approach. Assume the 101 and do not repeat it. Story arc: "
            "situation → complication → resolution → recommendations."
        ),
        "executive_summary": (
            "DELIVERABLE TYPE: Executive Summary — a focused leadership briefing on the "
            f"{'focus area: ' + focus_area if focus_area else 'most significant workspace theme'}. "
            "Cover: the single governing insight, key findings, strategic implications, risks, "
            "and the recommended next action. Concise: no more than 8–10 content slides. "
            "KEEP IT NON-TECHNICAL — describe any technology purely by its business outcome and "
            "decision relevance; do NOT name implementation mechanisms."
        ),
    }
    deliverable_summary = _DELIVERABLE_SUMMARIES.get(
        deliverable_type,
        f"DELIVERABLE TYPE: {deliverable_type}",
    )

    # Prominent, per-type audience-depth directive.  Executive Summary and Client 101
    # are business-level and must not surface implementation mechanisms; Client 201 is
    # where that depth belongs.  This is repeated separately from the summary because
    # the depth boundary is the single most common differentiation failure.
    _NONTECH = (
        "AUDIENCE DEPTH: business/executive — KEEP IT NON-TECHNICAL. Refer to a "
        "technology only by its business role and outcome. Do NOT name implementation "
        "mechanisms anywhere (no HSM / Hardware Security Module, MPC / multi-party "
        "computation, threshold signatures, distributed key generation, consensus or "
        "key-management schemes, or protocol/framework internals). Park all such depth "
        "for the Client 201."
    )
    _DEPTH_DIRECTIVE = {
        "executive_summary": _NONTECH,
        "client_101": _NONTECH,
        "client_201": (
            "AUDIENCE DEPTH: technical engagement team — implementation depth is EXPECTED. "
            "Name the architecture, mechanisms, protocols, and dependencies precisely "
            "(custody key management, settlement rails, integration points, governance)."
        ),
    }
    depth_directive = _DEPTH_DIRECTIVE.get(deliverable_type, "")

    return (
        f"Workspace: {workspace_name}\n"
        f"{focus_line}\n\n"
        f"{deliverable_summary}\n\n"
        f"{depth_directive}\n\n"
        f"{slide_guidance}\n\n"
        f"{graph_digest}\n\n"
        f"=== TASK ===\n"
        f"Produce the presentation blueprint JSON for the deliverable type above. Ensure:\n"
        f"  - Content slide count is {min_s}–{max_s} (target {target_s})\n"
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
        G, db, workspace_id, relevant_node_ids, effective_focus, deliverable_type
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
    # VALIDATION — deterministic pre-render gate (runs FIRST now)
    # No LLM calls. Reroutes/occupancy, divider hygiene, ending backstop (incl.
    # vendor-close detection), leak/placeholder scrub, bullet termination.
    # ═══════════════════════════════════════════════════════════════════════
    deck_spec = _validate_deck_spec(
        deck_spec,
        context=f"ws={workspace_id} type={deliverable_type}",
    )

    # ═══════════════════════════════════════════════════════════════════════
    # PHASE 4 — CONDITIONAL PRESENTATION REVIEW
    # Controlled attribution showed PR is a no-op on decks the deterministic
    # validator already leaves clean. So PR (an extra LLM call, ~12-30s, extra
    # provider-5xx exposure) now runs ONLY when a residual CONTENT gap remains
    # that the validator cannot synthesise — i.e. the deck still lacks a
    # client-directed recommendation close. On clean decks PR is skipped; on
    # flagged decks it runs and the output is re-validated.
    # ═══════════════════════════════════════════════════════════════════════
    _pr_issues = _residual_ending_issues(deck_spec)
    if _pr_issues:
        logger.info(
            "[deliverable ws=%d type=%s] Phase 4: presentation review TRIGGERED by %s",
            workspace_id, deliverable_type, _pr_issues,
        )
        deck_spec = _review_deck_spec(
            deck_spec,
            context=f"ws={workspace_id} type={deliverable_type}",
        )
        deck_spec = _validate_deck_spec(
            deck_spec,
            context=f"ws={workspace_id} type={deliverable_type}",
        )
        logger.info(
            "[deliverable ws=%d] Phase 4 complete: %d slides after conditional review",
            workspace_id, len(deck_spec.get("slides", [])),
        )
    else:
        logger.info(
            "[deliverable ws=%d type=%s] Phase 4: presentation review SKIPPED "
            "(deck already clean — deterministic validator sufficient)",
            workspace_id, deliverable_type,
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
