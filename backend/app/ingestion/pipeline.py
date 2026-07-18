"""
Ingestion pipeline — called as a FastAPI BackgroundTask after upload.
Orchestrates parsing → concept extraction → relationship extraction →
graph update → pattern extraction (threshold-gated).

Spreadsheet files (xlsx, xls, csv) are routed through the dedicated
Spreadsheet Intelligence Pipeline which preserves workbook structure
before extraction.  All other file types follow the document pipeline.

An IngestionAudit record is created at the start of each run and
updated at each stage so the audit service can compute completeness
and coverage scores without re-reading documents.

Performance notes
-----------------
• Chunks are computed once here and passed to both concept extraction
  and relationship extraction — eliminates the redundant second call to
  chunk_text_hierarchical() that previously happened inside each agent.
• Stage timings are logged at INFO level (op=ingestion_pipeline) so the
  logs can be used to produce a per-document performance breakdown.
• Document content fingerprint (SHA-256 of raw text) is stored on the
  Document row after parsing.  If the file has not changed, extraction
  is skipped and the pipeline exits early.
"""
from __future__ import annotations
import hashlib
import logging
import time
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.db.models import Concept, Document, IngestionAudit, Relationship
from app.db.session import SessionLocal
from app.ingestion.parsers import chunk_text_hierarchical, parse_document
from app.extraction.concept_agent import extract_concepts_from_chunks
from app.extraction.relationship_agent import extract_relationships_from_chunks
from app.extraction.pattern_agent import extract_patterns
from app.extraction.pattern_evolution import record_pattern_run, should_run_pattern_extraction
from app.graph.memory_manager import graph_memory_manager

logger = logging.getLogger(__name__)

_SPREADSHEET_TYPES = {"xlsx", "xls", "csv"}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _get_or_create_audit(db: Session, doc: Document) -> IngestionAudit:
    """
    Return the existing IngestionAudit for this document, or create a fresh one.

    Guards against the unique-constraint race condition that can occur if a
    document is re-queued while a prior ingestion attempt is still running.
    The unique(document_id) constraint on IngestionAudit means a concurrent
    insert would raise IntegrityError — we catch that and fall back to SELECT.
    """
    audit = db.query(IngestionAudit).filter(IngestionAudit.document_id == doc.id).first()
    if audit is None:
        try:
            audit = IngestionAudit(
                workspace_id=doc.workspace_id,
                document_id=doc.id,
                status="pending",
            )
            db.add(audit)
            db.commit()
            db.refresh(audit)
        except Exception:
            # Another thread created the record between our SELECT and INSERT —
            # roll back the failed insert and re-fetch.
            db.rollback()
            audit = db.query(IngestionAudit).filter(
                IngestionAudit.document_id == doc.id
            ).first()
            if audit is None:
                raise  # something else went wrong — propagate
    return audit


def run_ingestion_pipeline(document_id: int) -> None:
    """
    Full ingestion pipeline for a single document.
    Runs in a background thread — must open its own DB session.

    After extraction the pipeline:
    1. Calls graph_memory_manager.add_document_contributions() to update the
       in-memory graph incrementally (no full rebuild).
    2. Evaluates the pattern extraction threshold and runs the pattern agent
       only if the workspace knowledge has grown enough to warrant it.
    """
    pipeline_start = time.monotonic()
    db: Session = SessionLocal()
    try:
        doc = db.get(Document, document_id)
        if doc is None:
            logger.error("Document %d not found in DB.", document_id)
            return

        doc.upload_status = "processing"
        db.commit()

        # ── Create / reset audit record ───────────────────────────────────────
        audit = _get_or_create_audit(db, doc)
        audit.status = "processing"
        audit.started_at = datetime.now(timezone.utc)
        audit.error_detail = None
        db.commit()

        if doc.file_type in _SPREADSHEET_TYPES:
            _run_spreadsheet_pipeline(db, doc, audit)
        else:
            _run_document_pipeline(db, doc, audit)

        t_graph = time.monotonic()
        logger.info("[doc=%d] Updating graph memory", document_id)
        graph_memory_manager.add_document_contributions(doc.workspace_id, document_id, db)
        logger.info("[doc=%d] stage=graph_update elapsed=%.0fms",
                    document_id, (time.monotonic() - t_graph) * 1000)

        concept_count = (
            db.query(Concept).filter(Concept.workspace_id == doc.workspace_id).count()
        )
        rel_count = (
            db.query(Relationship).filter(Relationship.workspace_id == doc.workspace_id).count()
        )

        t_pattern = time.monotonic()
        if should_run_pattern_extraction(db, doc.workspace_id):
            logger.info("[doc=%d] Pattern extraction threshold met — running", document_id)
            new_patterns = extract_patterns(db, doc)
            record_pattern_run(
                db,
                workspace_id=doc.workspace_id,
                concept_count=concept_count,
                relationship_count=rel_count,
                patterns_generated=len(new_patterns),
                status="complete",
            )
            logger.info("[doc=%d] stage=pattern_extraction elapsed=%.0fms patterns=%d",
                        document_id, (time.monotonic() - t_pattern) * 1000, len(new_patterns))
        else:
            logger.info("[doc=%d] Pattern extraction threshold not met — skipping", document_id)
            record_pattern_run(
                db,
                workspace_id=doc.workspace_id,
                concept_count=concept_count,
                relationship_count=rel_count,
                patterns_generated=0,
                status="skipped",
            )

        # ── Finalise audit record ─────────────────────────────────────────────
        audit = db.get(IngestionAudit, audit.id)
        if audit:
            # Refresh extraction counts from DB (both agents may have written them)
            audit.concepts_extracted = (
                db.query(Concept)
                .filter(Concept.source_document_id == document_id)
                .count()
            )
            audit.relationships_extracted = (
                db.query(Relationship)
                .filter(Relationship.source_document_id == document_id)
                .count()
            )
            audit.status = "complete"
            audit.completed_at = datetime.now(timezone.utc)
            db.commit()

        doc.upload_status = "complete"
        db.commit()
        logger.info(
            "[doc=%d] stage=pipeline_total elapsed=%.0fms",
            document_id, (time.monotonic() - pipeline_start) * 1000,
        )

    except Exception as e:
        logger.exception("[doc=%d] Ingestion failed: %s", document_id, e)
        db.rollback()
        doc = db.get(Document, document_id)
        if doc:
            doc.upload_status = "failed"
            doc.error_message = str(e)
            db.commit()
        # Mark the audit record as failed too
        try:
            audit_row = db.query(IngestionAudit).filter(
                IngestionAudit.document_id == document_id
            ).first()
            if audit_row:
                audit_row.status = "failed"
                audit_row.error_detail = str(e)[:512]
                audit_row.completed_at = datetime.now(timezone.utc)
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


def _run_document_pipeline(db: Session, doc: Document, audit: IngestionAudit) -> None:
    """Parse and extract concepts/relationships from a text-based document."""
    t_parse = time.monotonic()
    with open(doc.filename, "rb") as f:
        file_bytes = f.read()

    raw_text, file_metadata = parse_document(file_bytes, doc.file_type)
    doc.raw_text = raw_text
    if not doc.title and file_metadata.get("title"):
        doc.title = file_metadata["title"]
    logger.info("[doc=%d] stage=parse elapsed=%.0fms chars=%d",
                doc.id, (time.monotonic() - t_parse) * 1000, len(raw_text))

    # ── Document fingerprint — skip extraction if content unchanged ───────────
    content_hash = _sha256(raw_text)
    if getattr(doc, "content_hash", None) == content_hash:
        existing_concepts = (
            db.query(Concept).filter(Concept.source_document_id == doc.id).count()
        )
        if existing_concepts > 0:
            logger.info(
                "[doc=%d] Content unchanged (hash=%s) — skipping extraction, "
                "re-using %d existing concepts.",
                doc.id, content_hash[:12], existing_concepts,
            )
            audit.status = "complete"
            audit.completed_at = datetime.now(timezone.utc)
            doc.upload_status = "complete"
            db.commit()
            # Still ensure the graph is up-to-date for this document — the
            # graph cache may have been evicted since the last ingestion.
            graph_memory_manager.add_document_contributions(doc.workspace_id, doc.id, db)
            return

    # Store the hash for future idempotency checks
    if hasattr(doc, "content_hash"):
        doc.content_hash = content_hash

    db.commit()

    # ── Compute chunks ONCE — shared by concept and relationship agents ────────
    t_chunk = time.monotonic()
    from app.config import settings
    chunks = chunk_text_hierarchical(
        raw_text,
        max_chars=settings.chunk_size * 4,
        overlap_chars=settings.chunk_overlap,
    )
    logger.info("[doc=%d] stage=chunking elapsed=%.0fms chunks=%d",
                doc.id, (time.monotonic() - t_chunk) * 1000, len(chunks))

    # Populate parse-level audit metrics from a single chunking pass
    audit.char_count = len(raw_text)
    audit.chunk_count = len(chunks)
    audit.page_count = int(file_metadata.get("page_count") or file_metadata.get("slide_count") or 0)
    db.commit()

    t_concepts = time.monotonic()
    logger.info("[doc=%d] Starting concept extraction", doc.id)
    extract_concepts_from_chunks(db, doc, chunks)
    logger.info("[doc=%d] stage=concept_extraction elapsed=%.0fms",
                doc.id, (time.monotonic() - t_concepts) * 1000)

    t_rels = time.monotonic()
    logger.info("[doc=%d] Starting relationship extraction", doc.id)
    extract_relationships_from_chunks(db, doc, chunks)
    logger.info("[doc=%d] stage=relationship_extraction elapsed=%.0fms",
                doc.id, (time.monotonic() - t_rels) * 1000)


def _run_spreadsheet_pipeline(db: Session, doc: Document, audit: IngestionAudit) -> None:
    """Route to the Spreadsheet Intelligence Pipeline for xlsx/xls/csv files."""
    from app.ingestion.spreadsheets.spreadsheet_processor import process_spreadsheet
    logger.info(
        "[doc=%d] Starting spreadsheet intelligence pipeline (type=%s)", doc.id, doc.file_type
    )
    # Spreadsheet pipeline handles its own text extraction — record file size only
    try:
        with open(doc.filename, "rb") as f:
            raw_bytes = f.read()
        audit.char_count = len(raw_bytes)
        audit.page_count = 0  # sheets are counted differently; handled by SpreadsheetIngestionRun
        db.commit()
    except Exception:
        pass
    process_spreadsheet(db, doc)
