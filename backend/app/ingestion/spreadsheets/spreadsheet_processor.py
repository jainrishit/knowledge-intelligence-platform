"""
Spreadsheet processor — top-level orchestrator for the spreadsheet intelligence pipeline.

Entry point for the ingestion pipeline:
    process_spreadsheet(db, doc) → None

Flow:
    1. Parse workbook (excel_parser / csv_parser) → WorkbookData
    2. Analyse sheets → SheetAnalysis[]
    3. Detect tables → DetectedTable[]
    4. Extract schema → WorkbookSchema (structural prior)
    5. Extract concepts via LLM (structure-aware prompts) → Concept[]
    6. Extract relationships via existing relationship agent → Relationship[]
    7. Record SpreadsheetIngestionRun audit row
    8. Return — pipeline.py handles graph memory + pattern threshold
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import Document, Concept, Relationship, SpreadsheetIngestionRun
from app.ingestion.spreadsheets.excel_parser import parse_excel, WorkbookData
from app.ingestion.spreadsheets.csv_parser import parse_csv
from app.ingestion.spreadsheets.sheet_analyzer import analyse_workbook
from app.ingestion.spreadsheets.table_detector import detect_tables, table_to_text_block
from app.ingestion.spreadsheets.schema_extractor import extract_schema, schema_to_context
from app.extraction.spreadsheet_concept_agent import extract_spreadsheet_concepts
from app.extraction.relationship_agent import extract_relationships

logger = logging.getLogger(__name__)


def process_spreadsheet(db: Session, doc: Document) -> None:
    """
    Run the full spreadsheet intelligence pipeline for doc.

    Populates doc.raw_text with the synthesised workbook text (for compatibility
    with the existing QA / deliverable retrieval path) and writes Concept and
    Relationship rows exactly as the document pipeline does.

    Raises ValueError on parse failure — the caller (pipeline.py) handles
    status transitions and error recording.
    """
    started_at = datetime.now(timezone.utc)
    concepts_extracted = 0
    relationships_extracted = 0
    tables_detected = 0
    sheets_processed = 0
    status = "complete"

    try:
        # ── 1. Parse ───────────────────────────────────────────────────
        with open(doc.filename, "rb") as f:
            file_bytes = f.read()

        workbook = _parse_by_type(file_bytes, doc.file_type, doc.filename)
        doc.raw_text = workbook.raw_text
        if not doc.title:
            doc.title = workbook.name
        db.commit()

        logger.info(
            "[doc=%d] Spreadsheet parsed: %d sheets, file_type=%s",
            doc.id, workbook.sheet_count, doc.file_type,
        )

        # ── 2. Analyse ─────────────────────────────────────────────────
        analyses = analyse_workbook(workbook.sheets)
        schema = extract_schema(workbook, analyses)
        schema_context = schema_to_context(schema)

        # ── 3. Detect tables + extract concepts per sheet ──────────────
        all_concepts: list[Concept] = []
        for sheet, analysis in zip(workbook.sheets, analyses):
            if not analysis.is_useful:
                continue
            tables = detect_tables(sheet, analysis)
            tables_detected += len(tables)
            sheets_processed += 1

            for table in tables:
                table_text = table_to_text_block(table)
                new_concepts = extract_spreadsheet_concepts(
                    db=db,
                    doc=doc,
                    table_text=table_text,
                    schema_context=schema_context,
                    sheet_type=analysis.sheet_type,
                    domain_hints=analysis.domain_hints,
                    table=table,
                )
                all_concepts.extend(new_concepts)
                concepts_extracted += len(new_concepts)

        logger.info(
            "[doc=%d] Spreadsheet concept extraction: %d concepts from %d tables across %d sheets",
            doc.id, concepts_extracted, tables_detected, sheets_processed,
        )

        # ── 4. Relationship extraction (reuses existing agent) ─────────
        concept_count = db.query(Concept).filter(Concept.workspace_id == doc.workspace_id).count()
        if concept_count >= 2:
            new_rels = extract_relationships(db, doc)
            relationships_extracted = len(new_rels)
            logger.info("[doc=%d] Spreadsheet relationship extraction: %d relationships", doc.id, relationships_extracted)

    except Exception:
        status = "failed"
        logger.exception("[doc=%d] Spreadsheet processing failed", doc.id)
        raise
    finally:
        completed_at = datetime.now(timezone.utc)
        _record_run(
            db=db,
            doc=doc,
            started_at=started_at,
            completed_at=completed_at,
            sheets_processed=sheets_processed,
            tables_detected=tables_detected,
            concepts_extracted=concepts_extracted,
            relationships_extracted=relationships_extracted,
            status=status,
        )


def _parse_by_type(file_bytes: bytes, file_type: str, filename: str) -> WorkbookData:
    if file_type == "csv":
        return parse_csv(file_bytes, filename)
    return parse_excel(file_bytes, filename)


def _record_run(
    db: Session,
    doc: Document,
    started_at: datetime,
    completed_at: datetime,
    sheets_processed: int,
    tables_detected: int,
    concepts_extracted: int,
    relationships_extracted: int,
    status: str,
) -> None:
    try:
        run = SpreadsheetIngestionRun(
            workspace_id=doc.workspace_id,
            document_id=doc.id,
            started_at=started_at,
            completed_at=completed_at,
            file_type=doc.file_type,
            sheets_processed=sheets_processed,
            tables_detected=tables_detected,
            concepts_extracted=concepts_extracted,
            relationships_extracted=relationships_extracted,
            status=status,
        )
        db.add(run)
        db.commit()
    except Exception:
        logger.exception("[doc=%d] Failed to record SpreadsheetIngestionRun", doc.id)
        db.rollback()
