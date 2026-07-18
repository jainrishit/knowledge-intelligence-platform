"""
Concept extraction agent.

Extracts named concepts from document text using the ICA LLM client.
Concepts below the CONCEPT_CONFIDENCE_MIN threshold are discarded.
Uses hierarchical chunking with overlap so no context is lost at chunk boundaries.

Performance notes
-----------------
• extract_concepts_from_chunks() is the primary entry point.  The pipeline
  passes pre-computed chunks so text is never chunked twice.
• Per-chunk LLM calls are dispatched concurrently via ThreadPoolExecutor
  (bounded at MAX_CONCURRENT_CHUNK_CALLS workers) so that a 10-chunk
  document does not wait for 10 sequential network round-trips.
• extract_concepts() is kept for backward compatibility with tests / callers
  that do not yet pass chunks explicitly.
"""
from __future__ import annotations
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import Concept, Document
from app.ingestion.parsers import chunk_text_hierarchical
from app.llm_client import chat

logger = logging.getLogger(__name__)

# Maximum concurrent LLM calls per document.
# 8 workers processes a 12-chunk document in ~2 batches, saving round-trip waits.
MAX_CONCURRENT_CHUNK_CALLS = 8

CONCEPT_SYSTEM_PROMPT = """\
You are an AI knowledge compiler for IBM Consulting.
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
                # 8192 — raised from 4096; large PPTX chunks with 20+ concepts
                # each having description + source_excerpt were truncating JSON.
                max_tokens=8192,
                operation="concept_extraction",
            )
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw)
        except (json.JSONDecodeError, IndexError) as e:
            if attempt == 0:
                logger.warning("Concept extraction JSON parse failed (attempt 1), retrying: %s", e)
            else:
                logger.error("Concept extraction JSON parse failed after retry: %s", e)
                return []
    return []


def extract_concepts_from_chunks(
    db: Session,
    doc: Document,
    chunks: list[str],
) -> list[Concept]:
    """
    Extract concepts from pre-computed chunks and persist to DB.

    Dispatches per-chunk LLM calls concurrently (up to MAX_CONCURRENT_CHUNK_CALLS)
    so that a multi-chunk document does not wait for chunks sequentially.
    """
    if not chunks:
        logger.info("[doc=%d] No chunks provided for concept extraction, skipping.", doc.id)
        return []

    existing_names: set[str] = {
        c.name.lower()
        for c in db.query(Concept).filter(Concept.workspace_id == doc.workspace_id).all()
    }

    # ── Dispatch LLM calls concurrently ──────────────────────────────────────
    t0 = time.monotonic()
    raw_results: list[list[dict[str, Any]]] = [[] for _ in chunks]
    first_exception: Exception | None = None

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHUNK_CALLS) as executor:
        future_to_idx = {executor.submit(_call_llm, chunk): i for i, chunk in enumerate(chunks)}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                raw_results[idx] = future.result()
            except Exception as exc:
                logger.warning("[doc=%d] Chunk %d concept extraction failed: %s", doc.id, idx, exc)
                raw_results[idx] = []
                if first_exception is None:
                    first_exception = exc

    # Log the first chunk failure but continue — partial extraction is better
    # than marking the whole document failed because one chunk timed out.
    if first_exception is not None:
        logger.warning(
            "[doc=%d] %d chunk(s) failed during concept extraction (first error: %s). "
            "Proceeding with results from successful chunks.",
            doc.id, sum(1 for r in raw_results if not r), first_exception,
        )

    logger.info(
        "[doc=%d] Concept LLM calls: %d chunks completed in %.0fms",
        doc.id, len(chunks), (time.monotonic() - t0) * 1000,
    )

    # ── Merge results — deduplicate across chunks ─────────────────────────────
    created: list[Concept] = []
    seen_names: set[str] = set()

    for raw_items in raw_results:
        for raw in raw_items:
            try:
                item = ConceptItem(**raw)
            except ValidationError as ve:
                logger.warning("[doc=%d] Rejected concept (validation failed): %s — %s", doc.id, raw, ve)
                continue

            if item.confidence < settings.concept_confidence_min:
                logger.debug(
                    "[doc=%d] Skipped low-confidence concept '%s' (confidence=%.2f < %.2f)",
                    doc.id, item.name, item.confidence, settings.concept_confidence_min,
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
    logger.info(
        "[doc=%d] Extracted %d concepts (confidence ≥ %.2f) from %d chunks.",
        doc.id, len(created), settings.concept_confidence_min, len(chunks),
    )
    return created


def extract_concepts(db: Session, doc: Document) -> list[Concept]:
    """
    Backward-compatible entry point — chunks the document text internally
    and delegates to extract_concepts_from_chunks().

    Prefer calling extract_concepts_from_chunks() directly from the pipeline
    so that chunking only happens once per document.
    """
    if not doc.raw_text or len(doc.raw_text.strip()) < 100:
        logger.info("[doc=%d] Text too short for concept extraction, skipping.", doc.id)
        return []

    chunks = chunk_text_hierarchical(
        doc.raw_text,
        max_chars=settings.chunk_size * 4,
        overlap_chars=settings.chunk_overlap,
    )
    return extract_concepts_from_chunks(db, doc, chunks)
