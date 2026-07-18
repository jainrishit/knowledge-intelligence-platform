"""
Relationship extraction agent.

Discovers relationships between concepts already in the workspace.
Each relationship carries a strength score (0.0–1.0); relationships
below 0.4 are discarded. Concept names are fuzzy-matched against the
workspace concept list to resolve minor LLM naming variations.

Relationship type vocabulary (see ALLOWED_RELATIONSHIP_TYPES in models.py):

  Structural / architectural:
    depends_on, requires, implements, extends, is_part_of,
    integrates_with, replaces

  Causal / enabling:
    enables, causes, mitigates, supports

  Process / workflow:
    precedes, triggers, produces, consumes

  Regulatory / governance:
    governs, complies_with, regulates

  Stakeholder / business:
    owned_by, used_by, impacts

  Comparison / contrast:
    contrasts_with, competes_with

  Generic fallback:
    related_to  (use only when no specific type fits)

Performance notes
-----------------
• extract_relationships_from_chunks() is the primary entry point.  The
  pipeline passes pre-computed chunks so text is never chunked twice.
• Per-chunk LLM calls are dispatched concurrently (MAX_CONCURRENT_CHUNK_CALLS)
  so that a 10-chunk document does not serially wait for 10 round-trips.
• extract_relationships() is kept for backward compatibility.
"""
from __future__ import annotations
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session
from thefuzz import process as fuzzy_process

from app.config import settings
from app.db.models import Concept, Document, Relationship, ALLOWED_RELATIONSHIP_TYPES
from app.ingestion.parsers import chunk_text_hierarchical
from app.llm_client import chat

logger = logging.getLogger(__name__)

# Maximum concurrent LLM calls per document.
MAX_CONCURRENT_CHUNK_CALLS = 8

RELATIONSHIP_SYSTEM_PROMPT = """\
You are a knowledge graph architect for IBM Consulting.
Your task: identify ALL meaningful relationships between concepts extracted from consulting documents.
Work strictly from the provided document text — no external knowledge, no fabrication.

Only create a relationship if the document text explicitly or strongly implies it.

EXTRACTION MANDATE — be comprehensive, not conservative:
You MUST extract every relationship the text supports, not just the most obvious ones.
A sparse graph is far worse than a slightly over-connected graph.
For EVERY concept pair mentioned in proximity, ask:
  - Does the text say one depends on, requires, uses, produces, or enables the other?
  - Does one standard/regulation govern or mandate the other?
  - Is one technology a component, replacement, or extension of another?
  - Do they appear in a before/after, cause/effect, or input/output relationship?
  If yes to any → extract the relationship.

═══════════════════════════════════════════════════════
RELATIONSHIP TYPE VOCABULARY — choose the MOST SPECIFIC type
═══════════════════════════════════════════════════════

Structural / architectural:
  depends_on       — A cannot function without B
  requires         — A mandates the presence or use of B
  implements       — A is a concrete realisation of B
  extends          — A builds on or enhances B
  is_part_of       — A is a component or sub-element of B
  integrates_with  — A connects to or interoperates with B
  replaces         — A supersedes or displaces B

Causal / enabling:
  enables          — A makes B possible
  causes           — A directly produces B as an outcome
  mitigates        — A reduces or controls B (especially risks)
  supports         — A provides capability or backing for B

Process / workflow:
  precedes         — A happens before B in a sequence
  triggers         — A initiates or activates B
  produces         — A outputs or generates B
  consumes         — A uses up or processes B

Regulatory / governance:
  governs          — A sets rules or standards for B
  complies_with    — A conforms to B
  regulates        — A exercises authority or oversight over B

Stakeholder / business:
  owned_by         — A is owned or controlled by B (entity)
  used_by          — A is used by B (entity or process)
  impacts          — A materially affects B (outcomes, costs, risks)

Comparison / contrast:
  contrasts_with   — A and B are alternatives or opposites
  competes_with    — A and B serve the same function and compete

Generic fallback — ONLY when no specific type applies:
  related_to

═══════════════════════════════════════════════════════
STRENGTH SCORING
═══════════════════════════════════════════════════════
  0.85–1.0 : explicit, central to the document's argument
  0.65–0.85: clearly stated
  0.40–0.65: strongly implied by context
  < 0.40   : uncertain — EXCLUDE

═══════════════════════════════════════════════════════
PRIORITY RELATIONSHIPS TO DETECT
═══════════════════════════════════════════════════════
Always look for:
  - Technology A → depends_on / integrates_with → Technology B
  - Process A → precedes / triggers / produces → Process B
  - Standard A → governs / regulates → Technology B
  - Risk A → mitigates / causes → Outcome B
  - System A → enables / supports → Capability B
  - Entity A → owns / uses / impacts → System B
  - New A → replaces / contrasts_with / competes_with → Old B

═══════════════════════════════════════════════════════
OUTPUT FORMAT
═══════════════════════════════════════════════════════
Return a JSON array only. No prose. No markdown fences.
[
  {
    "source": "exact concept name from the provided list",
    "target": "exact concept name from the provided list",
    "relationship_type": "one type from the vocabulary above",
    "strength": 0.85,
    "reasoning": "document states X directly enables Y for settlement"
  }
]

Rules:
  - Only use concept names from the provided list.
  - Only create a relationship if the document text supports it.
  - strength < 0.40 → exclude entirely.
  - No self-referential relationships (source == target).
  - Prefer specific types over related_to.
  - Return [] if no clear relationships can be inferred.
"""

# Accept relationships down to 0.35 — lower than before to capture more
# implied relationships without letting noise through.
STRENGTH_FLOOR = 0.35

FUZZY_THRESHOLD = 80


class RelationshipItem(BaseModel):
    source: str
    target: str
    relationship_type: str = "related_to"
    strength: float = Field(default=0.7, ge=0.0, le=1.0)
    reasoning: str = Field(default="")


def _fuzzy_resolve(name: str, name_to_id: dict[str, int]) -> int | None:
    if name.lower() in name_to_id:
        return name_to_id[name.lower()]
    result = fuzzy_process.extractOne(name, list(name_to_id.keys()))
    if result and result[1] >= FUZZY_THRESHOLD:
        logger.debug("Fuzzy matched '%s' → '%s' (score %s)", name, result[0], result[1])
        return name_to_id[result[0]]
    return None


def _call_llm(concept_list_text: str, doc_chunk: str) -> list[dict[str, Any]]:
    for attempt in range(2):
        try:
            raw = chat(
                system=RELATIONSHIP_SYSTEM_PROMPT,
                user=(
                    f"Known concepts:\n{concept_list_text}\n\n"
                    f"Document excerpt:\n{doc_chunk}"
                ),
                # 8192 tokens — raised from 4096 because a focused concept list
                # can still yield 30-60 relationships per chunk, each with a
                # reasoning field.  4096 was consistently truncating the JSON
                # array mid-object, causing all parse attempts to fail.
                max_tokens=8192,
                operation="relationship_extraction",
            )
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except (json.JSONDecodeError, IndexError) as e:
            if attempt == 0:
                logger.warning("Relationship extraction JSON parse failed (attempt 1): %s", e)
            else:
                logger.error("Relationship extraction JSON parse failed after retry: %s", e)
                return []
    return []


def _chunk_concept_list(all_concepts: list[Concept], chunk_text: str) -> tuple[str, list[Concept]]:
    """
    Return (concept_list_text, filtered_concepts) containing only concepts
    that are likely mentioned in this specific chunk.

    Strategy:
      1. Keep concepts whose name appears as a substring of the chunk (case-insensitive).
      2. Always keep the top-10 highest-confidence concepts as anchors — they are
         the workspace's most central ideas and deserve a chance even in chunks
         where they aren't explicitly named.
      3. Cap at MAX_CONCEPTS_PER_CHUNK to avoid overwhelming Claude.

    This dramatically improves relationship quality: Claude sees a focused list of
    ~15–25 concepts that actually appear in the text, not a wall of 200 unrelated names.
    """
    MAX_CONCEPTS_PER_CHUNK = 40
    ANCHOR_COUNT = 10

    chunk_lower = chunk_text.lower()

    # Sort by confidence descending so anchors are the most important concepts
    sorted_concepts = sorted(all_concepts, key=lambda c: getattr(c, "confidence", 0.8), reverse=True)
    anchor_set = set(id(c) for c in sorted_concepts[:ANCHOR_COUNT])

    filtered: list[Concept] = []
    for c in sorted_concepts:
        if id(c) in anchor_set or c.name.lower() in chunk_lower:
            filtered.append(c)
        if len(filtered) >= MAX_CONCEPTS_PER_CHUNK:
            break

    # Fallback: if fewer than 5 matched, use all (tiny workspace)
    if len(filtered) < 5:
        filtered = sorted_concepts[:MAX_CONCEPTS_PER_CHUNK]

    text = "\n".join(f"- {c.name} ({c.type or 'General'})" for c in filtered)
    return text, filtered


def extract_relationships_from_chunks(
    db: Session,
    doc: Document,
    chunks: list[str],
) -> list[Relationship]:
    """
    Extract relationships from pre-computed chunks and persist to DB.

    Dispatches per-chunk LLM calls concurrently (up to MAX_CONCURRENT_CHUNK_CALLS).
    Each chunk receives a focused concept list (only concepts likely in that chunk
    plus high-confidence anchors) rather than the full workspace concept list.
    """
    all_concepts = (
        db.query(Concept)
        .filter(Concept.workspace_id == doc.workspace_id)
        .all()
    )
    if len(all_concepts) < 2:
        logger.info("[doc=%d] Fewer than 2 concepts in workspace, skipping relationship extraction.", doc.id)
        return []

    if not chunks:
        logger.info("[doc=%d] No chunks provided for relationship extraction, skipping.", doc.id)
        return []

    name_to_id = {c.name.lower(): c.id for c in all_concepts}

    existing_pairs: set[tuple[int, int, str]] = {
        (rel.source_concept_id, rel.target_concept_id, rel.relationship_type)
        for rel in db.query(Relationship).filter(Relationship.workspace_id == doc.workspace_id).all()
    }

    # ── Build per-chunk concept lists ────────────────────────────────────────
    # Each chunk gets a focused list: concepts that appear in the chunk text
    # plus high-confidence anchor concepts, capped at MAX_CONCEPTS_PER_CHUNK.
    chunk_concept_texts: list[str] = []
    for chunk in chunks:
        clist_text, _ = _chunk_concept_list(all_concepts, chunk)
        chunk_concept_texts.append(clist_text)

    # ── Dispatch LLM calls concurrently ──────────────────────────────────────
    t0 = time.monotonic()
    raw_results: list[list[dict[str, Any]]] = [[] for _ in chunks]
    first_exception: Exception | None = None

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHUNK_CALLS) as executor:
        future_to_idx = {
            executor.submit(_call_llm, chunk_concept_texts[i], chunk): i
            for i, chunk in enumerate(chunks)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                raw_results[idx] = future.result()
            except Exception as exc:
                logger.warning(
                    "[doc=%d] Chunk %d relationship extraction failed: %s", doc.id, idx, exc
                )
                raw_results[idx] = []
                if first_exception is None:
                    first_exception = exc

    # Log the first chunk failure but continue — partial relationship extraction
    # is far better than marking the whole document failed because one chunk
    # timed out or returned malformed JSON.
    if first_exception is not None:
        logger.warning(
            "[doc=%d] %d chunk(s) failed during relationship extraction (first error: %s). "
            "Proceeding with results from successful chunks.",
            doc.id, sum(1 for r in raw_results if not r), first_exception,
        )

    logger.info(
        "[doc=%d] Relationship LLM calls: %d chunks completed in %.0fms",
        doc.id, len(chunks), (time.monotonic() - t0) * 1000,
    )

    # ── Merge and persist ─────────────────────────────────────────────────────
    created: list[Relationship] = []

    for raw_items in raw_results:
        for raw in raw_items:
            try:
                item = RelationshipItem(**raw)
            except ValidationError as ve:
                logger.warning("[doc=%d] Rejected relationship: %s — %s", doc.id, raw, ve)
                continue

            if item.strength < STRENGTH_FLOOR:
                continue

            if item.relationship_type not in ALLOWED_RELATIONSHIP_TYPES:
                item.relationship_type = "related_to"

            source_id = _fuzzy_resolve(item.source, name_to_id)
            target_id = _fuzzy_resolve(item.target, name_to_id)

            if source_id is None or target_id is None:
                continue
            if source_id == target_id:
                continue

            key = (source_id, target_id, item.relationship_type)
            if key in existing_pairs:
                continue

            rel = Relationship(
                workspace_id=doc.workspace_id,
                source_concept_id=source_id,
                target_concept_id=target_id,
                relationship_type=item.relationship_type,
                source_document_id=doc.id,
                strength=item.strength,
                reasoning=item.reasoning or None,
            )
            db.add(rel)
            existing_pairs.add(key)
            created.append(rel)

    db.commit()
    logger.info("[doc=%d] Extracted %d relationships from %d chunks.", doc.id, len(created), len(chunks))
    return created


def extract_relationships(db: Session, doc: Document) -> list[Relationship]:
    """
    Backward-compatible entry point — chunks the document text internally
    and delegates to extract_relationships_from_chunks().

    Prefer calling extract_relationships_from_chunks() directly from the
    pipeline so that chunking only happens once per document.
    """
    chunks = chunk_text_hierarchical(
        doc.raw_text or "",
        max_chars=settings.chunk_size * 4,
        overlap_chars=settings.chunk_overlap,
    )
    return extract_relationships_from_chunks(db, doc, chunks)
