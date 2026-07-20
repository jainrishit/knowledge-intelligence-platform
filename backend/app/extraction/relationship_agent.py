"""
Relationship extraction agent.

Discovers relationships between concepts already in the workspace.
Each relationship carries a strength score (0.0–1.0); relationships
below 0.5 are discarded. Concept names are fuzzy-matched against the
workspace concept list to resolve minor LLM naming variations.
"""
from __future__ import annotations
import json
import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session
from thefuzz import process as fuzzy_process

from app.config import settings
from app.db.models import Concept, Document, Relationship, ALLOWED_RELATIONSHIP_TYPES
from app.ingestion.parsers import chunk_text_hierarchical
from app.llm_client import chat

logger = logging.getLogger(__name__)

RELATIONSHIP_SYSTEM_PROMPT = """\
You are an AI knowledge compiler for IBM Consulting.
Discover relationships between concepts compiled from IBM consulting documents.
Work strictly from the provided document text — no external knowledge, no fabrication.

Given a list of known concepts and a document excerpt, identify relationships
ONLY between concepts that already appear in the concept list.

## Strength scoring
- 0.9–1.0 : explicit, repeated, central to the document's argument
- 0.7–0.9 : clearly stated in the document
- 0.5–0.7 : implied or mentioned briefly
- < 0.5   : uncertain — EXCLUDE

## Output — JSON array only, no prose, no markdown:
[
  {
    "source": "exact concept name from the list",
    "target": "exact concept name from the list",
    "relationship_type": "one of: depends_on, requires, implements, extends, contrasts_with, enables, is_part_of, related_to",
    "strength": 0.85,
    "reasoning": "document states X directly requires Y for settlement"
  }
]

Rules:
- Only use concept names from the provided list. No invention.
- Only create a relationship if the document text explicitly supports it.
- strength < 0.5 → exclude entirely.
- No self-referential relationships (source == target).
- Return [] if no clear relationships can be inferred.
"""

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
                max_tokens=2048,
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


def extract_relationships(db: Session, doc: Document) -> list[Relationship]:
    """Extract relationships for the given document's workspace."""
    all_concepts = (
        db.query(Concept)
        .filter(Concept.workspace_id == doc.workspace_id)
        .all()
    )
    if len(all_concepts) < 2:
        logger.info("[doc=%d] Fewer than 2 concepts in workspace, skipping relationship extraction.", doc.id)
        return []

    name_to_id = {c.name.lower(): c.id for c in all_concepts}
    concept_list_text = "\n".join(f"- {c.name} ({c.type})" for c in all_concepts)

    chunks = chunk_text_hierarchical(
        doc.raw_text or "",
        max_chars=settings.chunk_size * 4,
        overlap_chars=settings.chunk_overlap,
    )

    created: list[Relationship] = []
    existing_pairs: set[tuple[int, int, str]] = set()

    for rel in db.query(Relationship).filter(Relationship.workspace_id == doc.workspace_id).all():
        existing_pairs.add((rel.source_concept_id, rel.target_concept_id, rel.relationship_type))

    for chunk in chunks:
        raw_items = _call_llm(concept_list_text, chunk)
        for raw in raw_items:
            try:
                item = RelationshipItem(**raw)
            except ValidationError as ve:
                logger.warning("[doc=%d] Rejected relationship: %s — %s", doc.id, raw, ve)
                continue

            if item.strength < 0.5:
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
    logger.info("[doc=%d] Extracted %d relationships.", doc.id, len(created))
    return created
