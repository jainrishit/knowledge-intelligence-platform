"""
Ingestion pipeline — called as a FastAPI BackgroundTask after upload.
Orchestrates parsing → concept extraction → relationship extraction →
graph update → pattern extraction (threshold-gated).

Spreadsheet files (xlsx, xls, csv) are routed through the dedicated
Spreadsheet Intelligence Pipeline which preserves workbook structure
before extraction.  All other file types follow the document pipeline.
"""
from __future__ import annotations
import logging
from sqlalchemy.orm import Session

from app.db.models import Concept, Document, Relationship
from app.db.session import SessionLocal
from app.ingestion.parsers import parse_document
from app.extraction.concept_agent import extract_concepts
from app.extraction.relationship_agent import extract_relationships
from app.extraction.pattern_agent import extract_patterns
from app.extraction.pattern_evolution import record_pattern_run, should_run_pattern_extraction
from app.graph.memory_manager import graph_memory_manager

logger = logging.getLogger(__name__)

_SPREADSHEET_TYPES = {"xlsx", "xls", "csv"}


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
    db: Session = SessionLocal()
    try:
        doc = db.get(Document, document_id)
        if doc is None:
            logger.error("Document %d not found in DB.", document_id)
            return

        doc.upload_status = "processing"
        db.commit()

        if doc.file_type in _SPREADSHEET_TYPES:
            _run_spreadsheet_pipeline(db, doc)
        else:
            _run_document_pipeline(db, doc)

        logger.info("[doc=%d] Updating graph memory", document_id)
        graph_memory_manager.add_document_contributions(doc.workspace_id, document_id, db)

        concept_count = (
            db.query(Concept).filter(Concept.workspace_id == doc.workspace_id).count()
        )
        rel_count = (
            db.query(Relationship).filter(Relationship.workspace_id == doc.workspace_id).count()
        )

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

        doc.upload_status = "complete"
        db.commit()
        logger.info("[doc=%d] Ingestion complete.", document_id)

    except Exception as e:
        logger.exception("[doc=%d] Ingestion failed: %s", document_id, e)
        db.rollback()
        doc = db.get(Document, document_id)
        if doc:
            doc.upload_status = "failed"
            doc.error_message = str(e)
            db.commit()
    finally:
        db.close()


def _run_document_pipeline(db: Session, doc: Document) -> None:
    """Parse and extract concepts/relationships from a text-based document."""
    with open(doc.filename, "rb") as f:
        file_bytes = f.read()

    raw_text, file_metadata = parse_document(file_bytes, doc.file_type)
    doc.raw_text = raw_text
    if not doc.title and file_metadata.get("title"):
        doc.title = file_metadata["title"]
    db.commit()

    logger.info("[doc=%d] Starting concept extraction", doc.id)
    extract_concepts(db, doc)

    logger.info("[doc=%d] Starting relationship extraction", doc.id)
    extract_relationships(db, doc)


def _run_spreadsheet_pipeline(db: Session, doc: Document) -> None:
    """Route to the Spreadsheet Intelligence Pipeline for xlsx/xls/csv files."""
    from app.ingestion.spreadsheets.spreadsheet_processor import process_spreadsheet
    logger.info("[doc=%d] Starting spreadsheet intelligence pipeline (type=%s)", doc.id, doc.file_type)
    process_spreadsheet(db, doc)
