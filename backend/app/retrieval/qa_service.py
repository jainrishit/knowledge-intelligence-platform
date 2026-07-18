"""
Evidence-grounded QA service.

Retrieval strategy (designed for consistency):
  1. Keyword extraction from the question — local tokeniser for short
     questions (≤6 words / ≤60 chars); LLM extractor for longer ones.
  2. Cumulative, confidence-weighted semantic node scoring (top-k=20)
  3. Strength-filtered BFS from all seed nodes
  4. Deterministic context assembly: sorted by relevance score DESC then
     concept confidence DESC; near-duplicate excerpts are deduplicated
  5. Grounded generation at temperature=0 for factual stability

Performance notes
-----------------
• Short questions no longer fire an LLM keyword-extraction call — the
  local tokeniser handles them in microseconds.
• Document lookups inside the concept loop are batched into a single
  SELECT IN query rather than one db.get() per concept (N+1 fix).
"""
from __future__ import annotations
import hashlib
import json
import logging
import string

from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Concept, Document, ConsultingPattern, ChatMessage
from app.graph.memory_manager import graph_memory_manager
from app.graph.traversal import find_nodes_semantic, get_neighbourhood
from app.llm_client import chat
from app.schemas import AskResponse, SourceRef

logger = logging.getLogger(__name__)

# ─── Keyword extraction thresholds ────────────────────────────────────────────
# Most assistant questions are short and specific ("What is ISO 20022?",
# "How does Ripple work?", "Explain CBDC adoption").  Raise the threshold so
# the local tokeniser handles the majority of real queries without an LLM call.
_LOCAL_KW_THRESHOLD_CHARS  = 120
_LOCAL_KW_THRESHOLD_TOKENS = 12

_STOPWORDS = frozenset({
    # Articles / prepositions / conjunctions
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for",
    "with", "by", "from", "into", "onto", "upon", "via", "per",
    # Auxiliary / linking verbs
    "is", "are", "was", "were", "be", "been", "being", "am",
    "has", "have", "had", "do", "does", "did",
    "will", "would", "shall", "should", "may", "might", "must", "can", "could",
    # Question / demonstrative pronouns
    "that", "this", "these", "those", "as", "it", "its", "about",
    "what", "how", "why", "when", "where", "who", "which",
    "tell", "me", "you", "i", "my", "your", "their", "our",
    # Common question-structure verbs — appear in Q&A phrasing but carry
    # no concept meaning and burn seed slots when used as keywords.
    "explain", "describe", "list", "show", "give", "get", "find",
    "work", "works", "working",
    "exist", "exists", "existing",
    "happen", "happens",
    "make", "makes", "made",
    "use", "uses", "used", "using",
    "need", "needs", "needed",
    "look", "like",
    # Vague structural nouns that won't match concept names
    "risk", "risks",             # matches nothing — real risks are named (e.g. "Counterparty Risk")
    "opportunity", "opportunities",
    "issue", "issues",
    "challenge", "challenges",
    "system", "systems",         # will match "IBM Z" etc. already via BFS; doesn't help seeds
    "stakeholder", "stakeholders",
    "dependency", "dependencies",  # "tokenization depends_on X" found via graph, not this keyword
    "capability", "capabilities",
    "interaction", "interactions",
    "relationship", "relationships",
    "aspect", "aspects",
    "initiative", "initiatives",
    "example", "examples",
    "also", "any", "all", "some", "more", "most", "many", "few", "much",
    "just", "then", "than", "now", "still", "already", "yet",
    "not", "no", "yes",
    "its", "their", "our",
})

QA_SYSTEM_PROMPT = """\
You are an AI knowledge compiler and assistant for IBM Consulting.
You MUST answer ONLY from the context passages provided below — this is your only source of truth.
Do NOT use general knowledge, training data, internet search, or any external information.
If the context does not support the answer to the question, you MUST respond with exactly:
"I could not find this in the uploaded knowledge. The question may fall outside the documents in this workspace."

For every factual claim in your answer, cite the source document inline using [Source: Document Name].
When multiple passages describe the same topic, synthesise them into a single coherent answer rather
than repeating or summarising each passage independently. Use the most specific and complete information
available. Never fabricate citations. You are showing your work — every claim must trace back to a
specific document in the workspace.
"""

KEYWORD_EXTRACTION_PROMPT = """\
Extract the 3-7 most important topic keywords or concept names from the following question.
Return ONLY a JSON array of strings. No prose.
Example: ["ISO 20022", "payment messaging", "SWIFT"]
"""


def _extract_keywords_local(question: str) -> list[str]:
    """
    Fast local keyword extractor — zero LLM calls.

    Tokenises by splitting on whitespace and punctuation, lowercases, drops
    stopwords and very short tokens, returns up to 7 keywords preserving
    original capitalisation for proper nouns.
    """
    tokens = question.translate(
        str.maketrans(string.punctuation, " " * len(string.punctuation))
    ).split()
    keywords: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        lw = t.lower()
        if len(lw) >= 3 and lw not in _STOPWORDS and lw not in seen:
            seen.add(lw)
            keywords.append(t)
    return keywords[:7] or [question]


def _extract_keywords_llm(question: str) -> list[str]:
    """Extract search keywords from the question using the LLM at temperature=0."""
    try:
        raw = chat(
            system=KEYWORD_EXTRACTION_PROMPT,
            user=question,
            max_tokens=256,
            temperature=0.0,
            operation="keyword_extraction",
        )
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw)
    except Exception as e:
        logger.warning("LLM keyword extraction failed: %s", e)
        return [w.strip(".,?!") for w in question.split() if len(w) > 4]


def _get_keywords(question: str) -> list[str]:
    """
    Choose between local extraction and LLM extraction based on input length.

    Short questions (≤60 chars or ≤6 words): local tokeniser — microseconds.
    Long/complex questions: LLM extractor for higher accuracy.
    """
    words = question.split()
    if (
        len(question) <= _LOCAL_KW_THRESHOLD_CHARS
        or len(words) <= _LOCAL_KW_THRESHOLD_TOKENS
    ):
        return _extract_keywords_local(question)
    return _extract_keywords_llm(question)


def _excerpt_fingerprint(text: str) -> str:
    """Return a short hash of a normalised excerpt for deduplication."""
    normalised = " ".join(text.lower().split())
    return hashlib.md5(normalised[:200].encode()).hexdigest()


def ask_workspace(db: Session, workspace_id: int, question: str) -> AskResponse:
    """
    Full evidence-grounded Q&A pipeline designed for retrieval consistency.

    Key consistency properties:
    - Exact concept name matching: if the question contains a concept name that
      exists verbatim in the graph, it is always injected as a high-confidence seed
      (score=1.0) regardless of keyword extraction results.
    - Keyword extraction is local for short questions, LLM for long ones.
    - Seed nodes are selected by cumulative scoring across all keywords.
    - Near-duplicate source excerpts are deduplicated before LLM generation.
    - Final generation is at temperature=0 for factual stability.
    - Document records are fetched in a single batch query (no N+1).
    """
    import time as _time
    _t_start = _time.perf_counter()

    # ── Step 1: Keyword extraction ────────────────────────────────────────────
    _t = _time.perf_counter()
    keywords = _get_keywords(question)
    logger.info("[ws=%d qa] step=keywords  kw=%s  elapsed=%.1fms",
                workspace_id, keywords, (_time.perf_counter() - _t) * 1000)

    # ── Step 2: Graph load ────────────────────────────────────────────────────
    _t = _time.perf_counter()
    G = graph_memory_manager.get_workspace_graph(workspace_id, db)
    logger.info("[ws=%d qa] step=graph_load  nodes=%d edges=%d  elapsed=%.1fms",
                workspace_id, G.number_of_nodes(), G.number_of_edges(),
                (_time.perf_counter() - _t) * 1000)

    # ── Step 3: Seed node retrieval ──────────────────────────────────────────
    _t = _time.perf_counter()
    scored_seeds = find_nodes_semantic(G, keywords, top_k=settings.qa_top_k)

    # ── Exact concept name injection ─────────────────────────────────────────
    # If the question contains a concept name verbatim (case-insensitive), inject
    # it as a perfect-score seed regardless of keyword extraction results.
    # This ensures "Tell me about IBM Digital Asset Haven" never returns "not found"
    # when that concept exists in the graph.
    q_lower = question.lower()
    seed_ids = {nid for nid, _ in scored_seeds}
    exact_matches: list[tuple[int, float]] = []
    for node_id in G.nodes():
        node_data = G.nodes[node_id]
        concept_name = (node_data.get("name") or "").lower()
        if concept_name and len(concept_name) >= 4 and concept_name in q_lower:
            if node_id not in seed_ids:
                exact_matches.append((node_id, 1.0))
                seed_ids.add(node_id)
    if exact_matches:
        logger.info("[ws=%d qa] step=exact_match  injected=%d  names=%s",
                    workspace_id, len(exact_matches),
                    [G.nodes[nid].get("name") for nid, _ in exact_matches[:5]])
        scored_seeds = list(scored_seeds) + exact_matches

    # Fall back to direct DB name scan when the graph has no matching nodes
    if not scored_seeds:
        kw_lower = [k.lower() for k in keywords]
        db_matched = db.query(Concept).filter(Concept.workspace_id == workspace_id).all()
        scored_seeds = [
            (c.id, 0.7)
            for c in db_matched
            if any(kw in c.name.lower() for kw in kw_lower)
        ]

    logger.info("[ws=%d qa] step=seed_retrieval  seeds=%d  elapsed=%.1fms",
                workspace_id, len(scored_seeds), (_time.perf_counter() - _t) * 1000)

    seed_score: dict[int, float] = {nid: score for nid, score in scored_seeds}
    all_relevant_nodes: set[int] = set()
    # Use a dict keyed by (src, tgt) to deduplicate edges across multiple
    # seed neighbourhood expansions — same edge must not inflate scores twice.
    all_relevant_edges_deduped: dict[tuple[int, int], dict] = {}

    # ── Step 4: BFS traversal ─────────────────────────────────────────────────
    _t = _time.perf_counter()
    for node_id, seed_relevance in scored_seeds:
        nbr_nodes, nbr_edges = get_neighbourhood(
            G,
            node_id,
            hops=settings.graph_hop_depth,
            strength_threshold=settings.graph_strength_threshold,
            max_nodes=settings.graph_max_nodes,
        )
        all_relevant_nodes.update(nbr_nodes)
        for src, tgt, edata in nbr_edges:
            all_relevant_edges_deduped[(src, tgt)] = edata
    logger.info("[ws=%d qa] step=bfs_traversal  relevant_nodes=%d relevant_edges=%d  elapsed=%.1fms",
                workspace_id, len(all_relevant_nodes), len(all_relevant_edges_deduped),
                (_time.perf_counter() - _t) * 1000)

    # ── Precision: boost neighbour scores by relationship strength ────────────
    for (src, tgt), edata in all_relevant_edges_deduped.items():
        edge_str = float(edata.get("strength", 0.5))
        for nid in (src, tgt):
            if nid not in seed_score:
                best_connecting = max(
                    (seed_score[s] for s in (src, tgt) if s in seed_score),
                    default=0.0,
                )
                seed_score[nid] = best_connecting * edge_str * 0.8

    # ── Precision cap constants ───────────────────────────────────────────────
    # Seed nodes (direct keyword matches) always go in; non-seed concepts are
    # included only if their derived score meets the floor.
    _MAX_CONTEXT_CONCEPTS = 50   # hard cap on concept excerpts sent to the LLM
    _MIN_NONSEED_SCORE    = 0.10 # non-seed concepts below this score are dropped

    context_parts: list[str] = []
    source_refs: list[SourceRef] = []
    seen_doc_ids: set[int] = set()
    seen_excerpt_hashes: set[str] = set()

    if all_relevant_nodes:
        concepts = db.query(Concept).filter(Concept.id.in_(list(all_relevant_nodes))).all()

        # Stable sort: seed_score DESC → concept.confidence DESC → concept.id ASC
        concepts.sort(
            key=lambda c: (
                -seed_score.get(c.id, 0.0),
                -float(getattr(c, "confidence", 0.8)),
                c.id,
            )
        )

        # ── Precision filter: drop low-relevance non-seed concepts ────────────
        # Seed nodes (original keyword matches) are always retained.
        # Non-seed concepts contributed by BFS expansion are only retained if
        # their derived score ≥ _MIN_NONSEED_SCORE.  This keeps the context
        # focused on genuinely relevant material without discarding direct hits.
        seed_node_ids: set[int] = {nid for nid, _ in scored_seeds}
        concepts = [
            c for c in concepts
            if c.id in seed_node_ids or seed_score.get(c.id, 0.0) >= _MIN_NONSEED_SCORE
        ]

        # ── Batch-load all referenced documents (single query, no N+1) ────────
        doc_ids_needed: set[int] = {
            c.source_document_id for c in concepts if c.source_document_id
        }
        doc_by_id: dict[int, Document] = {}
        if doc_ids_needed:
            docs = db.query(Document).filter(Document.id.in_(doc_ids_needed)).all()
            doc_by_id = {d.id: d for d in docs}

        # concept_name_by_id covers the *filtered* set — the precision filter drops
        # low-relevance BFS neighbours.  When a relationship edge has one endpoint
        # outside the filtered set, we still want to render it so the model can see
        # how the queried concept connects to the wider graph.  Use a broader lookup
        # that includes ALL BFS nodes (not just filtered ones) for name resolution.
        all_bfs_concepts = db.query(Concept).filter(Concept.id.in_(list(all_relevant_nodes))).all()
        concept_name_by_id = {c.id: c.name for c in all_bfs_concepts}

        # ── Relationship-aware context: include the relationship chain ─────────
        # Only include high-specificity relationships (not bare related_to) so
        # the model can reason about HOW concepts connect, not just THAT they do.
        _HIGH_SPECIFICITY_TYPES = {
            "depends_on", "requires", "implements", "extends", "is_part_of",
            "integrates_with", "replaces", "enables", "causes", "mitigates",
            "supports", "precedes", "triggers", "produces", "consumes",
            "governs", "complies_with", "regulates", "owned_by", "used_by",
            "impacts", "contrasts_with", "competes_with",
        }
        # Seed-first relationship inclusion: edges directly involving a seed
        # concept are always included before the strength-sorted fill-in.
        # Without this, a concept like Red Hat OpenShift (seed, strength=0.84)
        # has its 3 relationships (strength 0.85-0.90) pushed past slot 30 by
        # 30 other graph edges with strength ≥ 0.93 that are unrelated to the
        # query. The user's question would receive no relationship context even
        # though 3 directly relevant edges exist.
        # Build two tiers of seed IDs:
        # - top_seed_ids: the highest-scoring seed(s) — directly named/queried concepts.
        #   Their edges are always included regardless of strength or cap.
        # - all_seed_ids: all seed nodes — their edges get priority in Pass 1.
        #
        # Problem this solves: a question like "Tell me about Red Hat OpenShift" has
        # id=162 as seed (score=0.84) with 3 relevant edges (strength 0.85-0.90). But
        # 30 other seeds exist with dozens of high-strength (≥0.93) edges that fill all
        # 15 Pass-1 slots before the RHO-adjacent edges are reached. Solution: the top
        # seed's edges bypass the cap entirely.
        _sorted_seeds = sorted(scored_seeds, key=lambda x: -x[1])
        _top_seed_score = _sorted_seeds[0][1] if _sorted_seeds else 0.0
        # "Top seeds" = concepts scored within 10% of the best seed score
        top_seed_ids: set[int] = {
            nid for nid, score in _sorted_seeds
            if score >= _top_seed_score * 0.9 and score > 0.5
        }
        all_seed_ids: set[int] = {nid for nid, _ in scored_seeds}

        def _make_rel_line(src: int, tgt: int, edata: dict) -> str | None:
            rel_type = edata.get("relationship_type", "related_to")
            if rel_type not in _HIGH_SPECIFICITY_TYPES:
                return None
            src_name = concept_name_by_id.get(src)
            tgt_name = concept_name_by_id.get(tgt)
            if not (src_name and tgt_name):
                return None
            strength = float(edata.get("strength", 0.7))
            reasoning = edata.get("reasoning", "")
            line = f"  {src_name}  →[{rel_type}, strength={strength:.2f}]→  {tgt_name}"
            if reasoning:
                line += f"\n    ({reasoning[:120]})"
            return line

        relationship_lines: list[str] = []
        rels_shown: set[tuple[int, int]] = set()
        _TOTAL_REL_CAP = 30

        # Pass 0: top-seed-adjacent edges — no cap, always included (max ~10 edges
        # per top-seed concept; won't explode context for typical graph degrees).
        for (src, tgt), edata in sorted(
            all_relevant_edges_deduped.items(),
            key=lambda x: -float(x[1].get("strength", 0.0)),
        ):
            if (src, tgt) in rels_shown:
                continue
            if src not in top_seed_ids and tgt not in top_seed_ids:
                continue
            line = _make_rel_line(src, tgt, edata)
            if line:
                relationship_lines.append(line)
                rels_shown.add((src, tgt))

        # Pass 1: other seed-adjacent edges, up to half the total cap
        _SEED_REL_CAP = _TOTAL_REL_CAP // 2
        for (src, tgt), edata in sorted(
            all_relevant_edges_deduped.items(),
            key=lambda x: -float(x[1].get("strength", 0.0)),
        ):
            if len(relationship_lines) >= _SEED_REL_CAP:
                break
            if (src, tgt) in rels_shown:
                continue
            if src not in all_seed_ids and tgt not in all_seed_ids:
                continue
            line = _make_rel_line(src, tgt, edata)
            if line:
                relationship_lines.append(line)
                rels_shown.add((src, tgt))

        # Pass 2: fill remaining slots with top-strength edges from the full BFS set
        for (src, tgt), edata in sorted(
            all_relevant_edges_deduped.items(),
            key=lambda x: -float(x[1].get("strength", 0.0)),
        ):
            if len(relationship_lines) >= _TOTAL_REL_CAP:
                break
            if (src, tgt) in rels_shown:
                continue
            line = _make_rel_line(src, tgt, edata)
            if line:
                relationship_lines.append(line)
                rels_shown.add((src, tgt))

        if relationship_lines:
            context_parts.append(
                "KNOWLEDGE GRAPH RELATIONSHIPS (how these concepts connect):\n"
                + "\n".join(relationship_lines)
            )

        concepts_added = 0
        for c in concepts:
            if concepts_added >= _MAX_CONTEXT_CONCEPTS:
                break

            # A concept with no excerpt is still answerable if it has a description.
            # Skip only when BOTH are empty — never skip a seed concept entirely.
            has_excerpt = bool(c.source_excerpt and c.source_excerpt.strip())
            has_desc = bool(c.description and c.description.strip())
            if not has_excerpt and not has_desc:
                continue

            # Deduplicate by excerpt fingerprint when one is present.
            if has_excerpt:
                fp = _excerpt_fingerprint(c.source_excerpt)
                if fp in seen_excerpt_hashes:
                    continue
                seen_excerpt_hashes.add(fp)

            doc_name = "Unknown Document"
            if c.source_document_id:
                doc = doc_by_id.get(c.source_document_id)
                if doc:
                    doc_name = doc.title or doc.filename

            # Build the context block. When the excerpt is very short (<80 chars)
            # the description carries more answerable content — lead with it and
            # include the excerpt as supporting evidence.
            if has_excerpt and len(c.source_excerpt) >= 80:
                context_parts.append(
                    f"[Concept: {c.name}] ({c.type})\n"
                    f"Description: {c.description}\n"
                    f"Excerpt from '{doc_name}': {c.source_excerpt}"
                )
            elif has_desc:
                excerpt_line = (
                    f"\nExcerpt from '{doc_name}': {c.source_excerpt}"
                    if has_excerpt else ""
                )
                context_parts.append(
                    f"[Concept: {c.name}] ({c.type})\n"
                    f"Description: {c.description}{excerpt_line}"
                )
            else:
                context_parts.append(
                    f"[Concept: {c.name}] ({c.type})\n"
                    f"Excerpt from '{doc_name}': {c.source_excerpt}"
                )

            concepts_added += 1
            if c.source_document_id and c.source_document_id not in seen_doc_ids:
                seen_doc_ids.add(c.source_document_id)
                ref_excerpt = c.source_excerpt[:300] if has_excerpt else (c.description or "")[:300]
                source_refs.append(SourceRef(
                    document_id=c.source_document_id,
                    document_name=doc_name,
                    excerpt=ref_excerpt,
                ))

        # Consulting patterns that overlap with matched concepts
        patterns = (
            db.query(ConsultingPattern)
            .filter(ConsultingPattern.workspace_id == workspace_id)
            .all()
        )
        for p in patterns:
            rel_ids = p.related_concept_ids or []
            if any(nid in rel_ids for nid in all_relevant_nodes):
                steps = "\n".join(f"  {s}" for s in (p.ibm_approach or []))
                context_parts.append(
                    f"[Pattern: {p.name}]\n"
                    f"Problem: {p.problem_statement}\n"
                    f"IBM Approach:\n{steps}"
                )
                for did in (p.source_document_ids or []):
                    if did not in seen_doc_ids:
                        doc = doc_by_id.get(int(did)) or db.get(Document, int(did))
                        if doc:
                            seen_doc_ids.add(did)
                            source_refs.append(SourceRef(
                                document_id=did,
                                document_name=doc.title or doc.filename,
                                excerpt="(Pattern source)",
                            ))

    if not context_parts:
        answer = (
            "I could not find this in the uploaded knowledge. "
            "The question may fall outside the documents in this workspace."
        )
        _save_chat(db, workspace_id, question, answer, [])
        logger.info("[ws=%d qa] COMPLETE  result=not_found  total_elapsed=%.1fs",
                    workspace_id, _time.perf_counter() - _t_start)
        return AskResponse(answer=answer, sources=[])

    context_text = "\n\n---\n\n".join(context_parts)
    context_chars = len(context_text)
    logger.info("[ws=%d qa] step=context_assembly  parts=%d chars=%d (~%d tokens)  elapsed=%.1fms",
                workspace_id, len(context_parts), context_chars, context_chars // 4,
                (_time.perf_counter() - _t_start) * 1000)

    # ── Step 5: Claude call ───────────────────────────────────────────────────
    _t = _time.perf_counter()
    try:
        answer = chat(
            system=QA_SYSTEM_PROMPT,
            user=f"Context:\n{context_text}\n\nQuestion: {question}",
            max_tokens=2048,
            temperature=0.0,
            operation="qa_generation",
        )
    except Exception as e:
        logger.error("[ws=%d qa] step=claude_call FAILED  elapsed=%.1fs  error=%s",
                     workspace_id, _time.perf_counter() - _t, e)
        answer = "An error occurred while generating the answer. Please try again."
        source_refs = []
    else:
        logger.info("[ws=%d qa] step=claude_call DONE  answer_chars=%d  elapsed=%.1fs",
                    workspace_id, len(answer), _time.perf_counter() - _t)

    # Only override Claude's answer with "not found" when we are certain no
    # relevant context was sent at all (context_parts was empty → we never
    # called Claude for a real answer, handled above).  Do NOT override a real
    # Claude answer just because source_refs happened to be empty — the answer
    # itself is the ground truth.  The previous logic was silently replacing valid
    # answers with "not found" whenever source_refs was empty after the Claude call,
    # which happened for concepts whose only source document had already been seen.

    _save_chat(db, workspace_id, question, answer, [s.document_id for s in source_refs])
    logger.info("[ws=%d qa] COMPLETE  total_elapsed=%.1fs",
                workspace_id, _time.perf_counter() - _t_start)
    return AskResponse(answer=answer, sources=source_refs)


def _save_chat(db: Session, workspace_id: int, question: str, answer: str, doc_ids: list[int]) -> None:
    db.add(ChatMessage(workspace_id=workspace_id, role="user", content=question, source_document_ids=[]))
    db.add(ChatMessage(workspace_id=workspace_id, role="assistant", content=answer, source_document_ids=doc_ids))
    db.commit()
