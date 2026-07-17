"""
Evidence-grounded QA service.

Retrieval strategy (designed for consistency):
  1. LLM keyword extraction from the question (temperature=0)
  2. Cumulative, confidence-weighted semantic node scoring (top-k=20)
  3. Strength-filtered BFS from all seed nodes
  4. Deterministic context assembly: sorted by relevance score DESC then
     concept confidence DESC; near-duplicate excerpts are deduplicated
  5. Grounded generation at temperature=0 for factual stability
"""
from __future__ import annotations
import hashlib
import json
import logging

from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Concept, Document, ConsultingPattern, ChatMessage
from app.graph.memory_manager import graph_memory_manager
from app.graph.traversal import find_nodes_semantic, get_neighbourhood
from app.llm_client import chat
from app.schemas import AskResponse, SourceRef

logger = logging.getLogger(__name__)

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


def _extract_keywords_llm(question: str) -> list[str]:
    """Extract search keywords from the question using the LLM at temperature=0."""
    try:
        raw = chat(
            system=KEYWORD_EXTRACTION_PROMPT,
            user=question,
            max_tokens=256,
            temperature=0.0,
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


def _excerpt_fingerprint(text: str) -> str:
    """Return a short hash of a normalised excerpt for deduplication."""
    normalised = " ".join(text.lower().split())
    return hashlib.md5(normalised[:200].encode()).hexdigest()


def ask_workspace(db: Session, workspace_id: int, question: str) -> AskResponse:
    """
    Full evidence-grounded Q&A pipeline designed for retrieval consistency.

    Key consistency properties:
    - Keyword extraction is deterministic (temperature=0).
    - Seed nodes are selected by cumulative scoring across all keywords
      (not winner-takes-all), so the same question reliably retrieves the
      same top seeds regardless of keyword ordering.
    - Retrieved concepts are assembled in a stable order: relevance score
      DESC, then concept confidence DESC, then concept ID ASC as tie-break.
    - Near-duplicate source excerpts are deduplicated before LLM generation
      so important passages are not pushed out of the context window.
    - Final generation is at temperature=0 to minimise structural variation.
    """

    keywords = _extract_keywords_llm(question)
    logger.info("[ws=%d] Q&A keywords: %s", workspace_id, keywords)

    G = graph_memory_manager.get_workspace_graph(workspace_id, db)
    scored_seeds = find_nodes_semantic(G, keywords, top_k=settings.qa_top_k)

    # Fall back to direct DB name scan when the graph has no matching nodes
    if not scored_seeds:
        kw_lower = [k.lower() for k in keywords]
        db_matched = db.query(Concept).filter(Concept.workspace_id == workspace_id).all()
        scored_seeds = [
            (c.id, 0.7)
            for c in db_matched
            if any(kw in c.name.lower() for kw in kw_lower)
        ]

    seed_score: dict[int, float] = {nid: score for nid, score in scored_seeds}
    all_relevant_nodes: set[int] = set()
    all_relevant_edges: list = []

    for node_id, _score in scored_seeds:
        nbr_nodes, nbr_edges = get_neighbourhood(
            G,
            node_id,
            hops=settings.graph_hop_depth,
            strength_threshold=settings.graph_strength_threshold,
            max_nodes=settings.graph_max_nodes,
        )
        all_relevant_nodes.update(nbr_nodes)
        all_relevant_edges.extend(nbr_edges)

    context_parts: list[str] = []
    source_refs: list[SourceRef] = []
    seen_doc_ids: set[int] = set()
    seen_excerpt_hashes: set[str] = set()

    if all_relevant_nodes:
        concepts = db.query(Concept).filter(Concept.id.in_(list(all_relevant_nodes))).all()

        # Stable sort: seed_score DESC → concept.confidence DESC → concept.id ASC
        # Nodes reached via BFS (not direct seeds) get score 0.0 and sort last.
        concepts.sort(
            key=lambda c: (
                -seed_score.get(c.id, 0.0),
                -float(getattr(c, "confidence", 0.8)),
                c.id,
            )
        )

        for c in concepts:
            if not c.source_excerpt:
                continue

            # Skip near-duplicate excerpts to avoid redundant context
            fp = _excerpt_fingerprint(c.source_excerpt)
            if fp in seen_excerpt_hashes:
                continue
            seen_excerpt_hashes.add(fp)

            doc_name = "Unknown Document"
            if c.source_document_id:
                doc = db.get(Document, c.source_document_id)
                if doc:
                    doc_name = doc.title or doc.filename

            context_parts.append(
                f"[Concept: {c.name}] ({c.type})\n"
                f"Description: {c.description}\n"
                f"Excerpt from '{doc_name}': {c.source_excerpt}"
            )
            if c.source_document_id and c.source_document_id not in seen_doc_ids:
                seen_doc_ids.add(c.source_document_id)
                source_refs.append(SourceRef(
                    document_id=c.source_document_id,
                    document_name=doc_name,
                    excerpt=c.source_excerpt[:300],
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
                        doc = db.get(Document, int(did))
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
        return AskResponse(answer=answer, sources=[])

    context_text = "\n\n---\n\n".join(context_parts)

    try:
        answer = chat(
            system=QA_SYSTEM_PROMPT,
            user=f"Context:\n{context_text}\n\nQuestion: {question}",
            max_tokens=2048,
            temperature=0.0,
        )
    except Exception as e:
        logger.error("QA generation failed: %s", e)
        answer = "An error occurred while generating the answer. Please try again."
        source_refs = []

    not_found_signal = any(
        phrase in answer.lower()
        for phrase in ("not found", "cannot find", "outside the documents", "could not find")
    )
    if not not_found_signal and not source_refs:
        answer = (
            "I could not find this in the uploaded knowledge. "
            "The question may fall outside the documents in this workspace."
        )
        source_refs = []

    _save_chat(db, workspace_id, question, answer, [s.document_id for s in source_refs])
    return AskResponse(answer=answer, sources=source_refs)


def _save_chat(db: Session, workspace_id: int, question: str, answer: str, doc_ids: list[int]) -> None:
    db.add(ChatMessage(workspace_id=workspace_id, role="user", content=question, source_document_ids=[]))
    db.add(ChatMessage(workspace_id=workspace_id, role="assistant", content=answer, source_document_ids=doc_ids))
    db.commit()
