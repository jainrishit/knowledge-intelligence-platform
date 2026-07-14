"""
Ingestion pipeline — called as a FastAPI BackgroundTask after upload.
Orchestrates parsing → concept extraction → relationship extraction → pattern extraction.
"""
from __future__ import annotations
import logging
from sqlalchemy.orm import Session

from app.db.models import Document
from app.db.session import SessionLocal
from app.ingestion.parsers import parse_document
from app.extraction.concept_agent import extract_concepts
from app.extraction.relationship_agent import extract_relationships
from app.extraction.pattern_agent import extract_patterns

logger = logging.getLogger(__name__)


def run_ingestion_pipeline(document_id: int) -> None:
    """
    Full ingestion pipeline for a single document.
    Runs in a background thread — must open its own DB session.
    """
    db: Session = SessionLocal()
    try:
        doc = db.get(Document, document_id)
        if doc is None:
            logger.error(f"Document {document_id} not found in DB.")
            return

        doc.upload_status = "processing"
        db.commit()

        with open(doc.filename, "rb") as f:
            file_bytes = f.read()

        raw_text, file_metadata = parse_document(file_bytes, doc.file_type)
        doc.raw_text = raw_text
        if not doc.title and file_metadata.get("title"):
            doc.title = file_metadata["title"]
        db.commit()

        logger.info(f"[doc={document_id}] Starting concept extraction")
        extract_concepts(db, doc)

        logger.info(f"[doc={document_id}] Starting relationship extraction")
        extract_relationships(db, doc)

        logger.info(f"[doc={document_id}] Starting pattern extraction")
        extract_patterns(db, doc)

        doc.upload_status = "complete"
        db.commit()
        logger.info(f"[doc={document_id}] Ingestion complete.")

    except Exception as e:
        logger.exception(f"[doc={document_id}] Ingestion failed: {e}")
        db.rollback()
        doc = db.get(Document, document_id)
        if doc:
            doc.upload_status = "failed"
            doc.error_message = str(e)
            db.commit()
    finally:
        db.close()
