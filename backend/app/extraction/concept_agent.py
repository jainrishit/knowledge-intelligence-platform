"""
Concept extraction agent.

Extracts named concepts from document text using the ICA LLM client.
Concepts below the CONCEPT_CONFIDENCE_MIN threshold are discarded.
Uses hierarchical chunking with overlap so no context is lost at chunk boundaries.
"""
from __future__ import annotations
import json
import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Concept, Document
from app.ingestion.parsers import chunk_text_hierarchical
from app.llm import chat

logger = logging.getLogger(__name__)

CONCEPT_SYSTEM_PROMPT = """\
You are Bob, an AI-native knowledge compiler for IBM Consulting.
Extract ALL significant concepts from the consulting document text provided.
You work strictly from what is written — never introduce general knowledge, external facts, or internet search results.

## What counts as a concept
A NAMED, REUSABLE entity that appears in IBM consulting work:
- Industry standards (ISO 20022, SWIFT, FIX protocol)
- Technologies & platforms (Kubernetes, AWS, SAP S/4HANA)
- IBM methodologies & frameworks
- Business processes (payment settlement, trade finance, KYC)
- Architecture patterns (microservices, event-driven, CQRS)
- Regulatory requirements (GDPR, MiFID II, Basel III)
- Organisational capabilities (API management, cloud governance)

NOT concepts: generic words (introduction, section, approach), vague phrases, pronouns.

## Confidence scoring
- 0.9–1.0 : explicitly named multiple times, central to the document
- 0.7–0.9 : clearly mentioned and relevant
- 0.5–0.7 : mentioned but peripheral
- < 0.5   : uncertain — EXCLUDE

## Output
Return ONLY a JSON array — no prose, no markdown fences:
[
  {
    "name": "ISO 20022",
    "type": "Payment Standard",
    "confidence": 0.95,
    "description": "1-2 sentence description based solely on this document",
    "source_excerpt": "verbatim 1-3 sentence quote from the document",
    "reasoning": "mentioned 4 times as the core migration target"
  }
]

Rules:
- source_excerpt MUST be a verbatim quote. Never fabricate.
- confidence < 0.6 → exclude entirely.
- type must be one of: Technology, Standard, Methodology, Framework, Process, Capability, Regulation, Pattern, Tool, General
- Minimum 5 concepts for documents > 300 words.
- Deduplicate by name (case-insensitive).
- Return [] if text is too short or too generic.
"""


class ConceptItem(BaseModel):
    name: str = Field(..., min_length=1)
    type: str = Field(default="General")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    description: str = Field(default="")
    source_excerpt: str = Field(..., min_length=5)
    reasoning: str = Field(default="")


def _call_llm(text_chunk: str) -> list[dict[str, Any]]:
    """Call LLM and parse concept JSON. Retries once on parse failure."""
    for attempt in range(2):
        try:
            raw = chat(
                system=CONCEPT_SYSTEM_PROMPT,
                user=f"Document text:\n\n{text_chunk}",
                max_tokens=4096,
            )
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except (json.JSONDecodeError, IndexError) as e:
            if attempt == 0:
                logger.warning(f"Concept extraction JSON parse failed (attempt 1), retrying: {e}")
            else:
                logger.error(f"Concept extraction JSON parse failed after retry: {e}")
                return []
    return []


def extract_concepts(db: Session, doc: Document) -> list[Concept]:
    """
    Extract concepts from doc.raw_text and persist to DB.
    Uses hierarchical chunking with overlap; filters by confidence threshold.
    """
    if not doc.raw_text or len(doc.raw_text.strip()) < 100:
        logger.info(f"[doc={doc.id}] Text too short for concept extraction, skipping.")
        return []

    chunks = chunk_text_hierarchical(
        doc.raw_text,
        max_chars=settings.chunk_size * 4,
        overlap_chars=settings.chunk_overlap,
    )

    created: list[Concept] = []
    seen_names: set[str] = set()

    existing_names = {
        c.name.lower()
        for c in db.query(Concept).filter(Concept.workspace_id == doc.workspace_id).all()
    }

    for chunk in chunks:
        raw_items = _call_llm(chunk)
        for raw in raw_items:
            try:
                item = ConceptItem(**raw)
            except ValidationError as ve:
                logger.warning(f"[doc={doc.id}] Rejected concept (validation failed): {raw} — {ve}")
                continue

            if item.confidence < settings.concept_confidence_min:
                logger.debug(
                    f"[doc={doc.id}] Skipped low-confidence concept '{item.name}' "
                    f"(confidence={item.confidence:.2f} < {settings.concept_confidence_min})"
                )
                continue

            name_lower = item.name.lower().strip()
            if name_lower in seen_names or name_lower in existing_names:
                continue

            concept = Concept(
                workspace_id=doc.workspace_id,
                name=item.name.strip(),
                type=item.type,
                description=item.description,
                source_document_id=doc.id,
                source_excerpt=item.source_excerpt,
                confidence=item.confidence,
            )
            db.add(concept)
            seen_names.add(name_lower)
            existing_names.add(name_lower)
            created.append(concept)

    db.commit()
    logger.info(f"[doc={doc.id}] Extracted {len(created)} concepts (confidence ≥ {settings.concept_confidence_min}).")
    return created
