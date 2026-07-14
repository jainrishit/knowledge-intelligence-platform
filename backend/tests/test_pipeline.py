"""
Ingestion pipeline tests — full parse->extract flow with mocked LLM,
pipeline failure path (file not found, LLM error), and DB status transitions.

Key design: the pipeline internally calls SessionLocal() from app.db.session.
We patch 'app.ingestion.pipeline.SessionLocal' to redirect it to the test
engine's Session factory, keeping the pipeline off the production DB.
"""
from __future__ import annotations
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine, StaticPool
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Document, Concept, Relationship, ConsultingPattern


# Each test creates its own named in-memory SQLite so parallel tests don't
# collide. The shared-cache URI lets us open multiple connections to the same
# in-memory DB within a single test.

def _make_session(name: str = "pipeline_test"):
    engine = create_engine(
        f"sqlite:///file:{name}?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False, "uri": True},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


def _teardown(engine):
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────

def _make_real_pdf(text: str) -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 72), text)
    return doc.tobytes()


CONCEPT_JSON = (
    '[{"name": "ISO 20022", "type": "Standard", "confidence": 0.9, '
    '"description": "A payment standard.", '
    '"source_excerpt": "ISO 20022 is the global standard.", '
    '"reasoning": "mentioned multiple times"}]'
)
RELATIONSHIP_JSON = "[]"
PATTERN_JSON = "[]"


# ══════════════════════════════════════════════════════════════════════
# PIPELINE — HAPPY PATH
# ══════════════════════════════════════════════════════════════════════

@patch("app.extraction.pattern_agent.chat", return_value=PATTERN_JSON)
@patch("app.extraction.relationship_agent.chat", return_value=RELATIONSHIP_JSON)
@patch("app.extraction.concept_agent.chat", return_value=CONCEPT_JSON)
def test_pipeline_sets_status_complete(mock_concept, mock_rel, mock_pattern):
    engine, Session = _make_session("pl_happy")
    db = Session()
    tmp_path = None
    try:
        from app.db.models import Workspace
        ws = Workspace(name="Test WS")
        db.add(ws)
        db.commit()

        # Text must be >100 chars so extract_concepts doesn't skip due to length guard
        pdf_bytes = _make_real_pdf(
            "ISO 20022 is a global payment messaging standard used by financial institutions "
            "worldwide to exchange electronic messages and enable interoperability across "
            "different payment systems and banking networks internationally."
        )
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name

        doc = Document(
            workspace_id=ws.id,
            filename=tmp_path,
            file_type="pdf",
            title="ISO 20022 Overview",
            upload_status="pending",
        )
        db.add(doc)
        db.commit()
        # Capture IDs before db.close() expires the ORM objects
        doc_id = doc.id
        ws_id = ws.id
        db.close()

        from app.ingestion.pipeline import run_ingestion_pipeline
        # Redirect pipeline's SessionLocal to the test engine
        with patch("app.ingestion.pipeline.SessionLocal", Session):
            run_ingestion_pipeline(doc_id)

        db2 = Session()
        doc2 = db2.get(Document, doc_id)
        assert doc2.upload_status == "complete", (
            f"Expected complete, got: {doc2.upload_status} — {doc2.error_message}"
        )
        assert doc2.raw_text is not None
        assert len(doc2.raw_text) > 0

        concepts = db2.query(Concept).filter(Concept.workspace_id == ws_id).all()
        assert len(concepts) >= 1
        assert concepts[0].name == "ISO 20022"
        assert concepts[0].source_document_id == doc_id
        assert concepts[0].source_excerpt is not None
        db2.close()
    finally:
        if tmp_path:
            os.unlink(tmp_path)
        _teardown(engine)


@patch("app.extraction.pattern_agent.chat", return_value=PATTERN_JSON)
@patch("app.extraction.relationship_agent.chat", return_value=RELATIONSHIP_JSON)
@patch("app.extraction.concept_agent.chat", return_value=CONCEPT_JSON)
def test_pipeline_populates_raw_text(mock_concept, mock_rel, mock_pattern):
    engine, Session = _make_session("pl_raw_text")
    db = Session()
    tmp_path = None
    try:
        from app.db.models import Workspace
        ws = Workspace(name="RawText WS")
        db.add(ws)
        db.commit()

        content = "SWIFT is the Society for Worldwide Interbank Financial Telecommunication."
        pdf_bytes = _make_real_pdf(content)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name

        doc = Document(
            workspace_id=ws.id, filename=tmp_path, file_type="pdf",
            title="SWIFT Overview", upload_status="pending",
        )
        db.add(doc)
        db.commit()
        doc_id = doc.id
        db.close()

        from app.ingestion.pipeline import run_ingestion_pipeline
        with patch("app.ingestion.pipeline.SessionLocal", Session):
            run_ingestion_pipeline(doc_id)

        db2 = Session()
        doc2 = db2.get(Document, doc_id)
        assert "SWIFT" in doc2.raw_text
        db2.close()
    finally:
        if tmp_path:
            os.unlink(tmp_path)
        _teardown(engine)


# ══════════════════════════════════════════════════════════════════════
# PIPELINE — FAILURE PATHS
# ══════════════════════════════════════════════════════════════════════

def test_pipeline_missing_file_sets_failed():
    engine, Session = _make_session("pl_missing_file")
    db = Session()
    try:
        from app.db.models import Workspace
        ws = Workspace(name="Fail WS")
        db.add(ws)
        db.commit()

        doc = Document(
            workspace_id=ws.id,
            filename="/nonexistent/path/to/file.pdf",
            file_type="pdf",
            title="Ghost",
            upload_status="pending",
        )
        db.add(doc)
        db.commit()
        doc_id = doc.id
        db.close()

        from app.ingestion.pipeline import run_ingestion_pipeline
        with patch("app.ingestion.pipeline.SessionLocal", Session):
            run_ingestion_pipeline(doc_id)

        db2 = Session()
        doc2 = db2.get(Document, doc_id)
        assert doc2.upload_status == "failed"
        assert doc2.error_message is not None
        db2.close()
    finally:
        _teardown(engine)


def test_pipeline_nonexistent_document_id_is_noop():
    """Calling pipeline with a non-existent document ID must not raise."""
    engine, Session = _make_session("pl_noop")
    Base.metadata.create_all(bind=engine)
    try:
        from app.ingestion.pipeline import run_ingestion_pipeline
        # Should return silently without exception
        with patch("app.ingestion.pipeline.SessionLocal", Session):
            run_ingestion_pipeline(document_id=99999999)
    finally:
        _teardown(engine)


@patch("app.extraction.concept_agent.chat", side_effect=RuntimeError("LLM API error"))
def test_pipeline_llm_error_sets_failed(mock_llm):
    engine, Session = _make_session("pl_llm_error")
    db = Session()
    tmp_path = None
    try:
        from app.db.models import Workspace
        ws = Workspace(name="LLM Fail WS")
        db.add(ws)
        db.commit()

        # Use enough text that extract_concepts doesn't skip due to <100 char guard
        pdf_bytes = _make_real_pdf(
            "ISO 20022 is a global payment messaging standard used by financial institutions "
            "worldwide to exchange electronic messages and enable interoperability across "
            "different payment systems and banking networks internationally."
        )
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name

        doc = Document(
            workspace_id=ws.id, filename=tmp_path, file_type="pdf",
            title="ISO POV", upload_status="pending",
        )
        db.add(doc)
        db.commit()
        doc_id = doc.id
        db.close()

        from app.ingestion.pipeline import run_ingestion_pipeline
        with patch("app.ingestion.pipeline.SessionLocal", Session):
            run_ingestion_pipeline(doc_id)

        db2 = Session()
        doc2 = db2.get(Document, doc_id)
        assert doc2.upload_status == "failed"
        assert "LLM API error" in (doc2.error_message or "")
        db2.close()
    finally:
        if tmp_path:
            os.unlink(tmp_path)
        _teardown(engine)


# ══════════════════════════════════════════════════════════════════════
# CONCEPT EXTRACTION — UNIT
# ══════════════════════════════════════════════════════════════════════

@patch("app.extraction.concept_agent.chat")
def test_concept_extraction_stores_source_document_id(mock_chat):
    """Every extracted concept must have a non-null source_document_id."""
    mock_chat.return_value = CONCEPT_JSON
    engine, Session = _make_session("pl_src_doc_id")
    db = Session()
    try:
        from app.db.models import Workspace
        ws = Workspace(name="Concept WS")
        db.add(ws)
        db.commit()

        doc = Document(
            workspace_id=ws.id,
            filename="fake.pdf",
            file_type="pdf",
            title="Test Doc",
            upload_status="processing",
            # Must be >100 chars to pass extract_concepts length guard
            raw_text=(
                "ISO 20022 is a global payment messaging standard used across the world "
                "by banks and financial institutions to enable interoperability between "
                "different payment systems and settlement networks globally."
            ),
        )
        db.add(doc)
        db.commit()

        from app.extraction.concept_agent import extract_concepts
        concepts = extract_concepts(db, doc)

        assert len(concepts) > 0
        for c in concepts:
            # doc.id is still accessible since we have not closed the session
            assert c.source_document_id == doc.id, "source_document_id must be set"
            assert c.source_excerpt is not None
            assert len(c.source_excerpt) >= 5
    finally:
        _teardown(engine)


@patch("app.extraction.concept_agent.chat")
def test_concept_extraction_confidence_gate(mock_chat):
    """Concepts below confidence threshold are not stored."""
    mock_chat.return_value = (
        '[{"name": "Generic Thing", "type": "General", "confidence": 0.3, '
        '"description": "Low confidence.", "source_excerpt": "mentioned once", '
        '"reasoning": "peripheral"}]'
    )
    engine, Session = _make_session("pl_conf_gate")
    db = Session()
    try:
        from app.db.models import Workspace
        ws = Workspace(name="Gate WS")
        db.add(ws)
        db.commit()

        doc = Document(
            workspace_id=ws.id, filename="x.pdf", file_type="pdf",
            title="X", upload_status="processing",
            raw_text="A " * 200,
        )
        db.add(doc)
        db.commit()

        from app.extraction.concept_agent import extract_concepts
        concepts = extract_concepts(db, doc)
        assert len(concepts) == 0, "Low-confidence concept must be rejected"
    finally:
        _teardown(engine)


@patch("app.extraction.concept_agent.chat")
def test_concept_extraction_deduplicates(mock_chat):
    """Same concept name should not be stored twice."""
    mock_chat.return_value = (
        '[{"name":"ISO 20022","type":"Standard","confidence":0.9,'
        '"description":"Standard.","source_excerpt":"ISO 20022 is the standard.",'
        '"reasoning":"central"},'
        '{"name":"iso 20022","type":"Standard","confidence":0.85,'
        '"description":"Standard again.","source_excerpt":"ISO 20022 is used widely.",'
        '"reasoning":"duplicate"}]'
    )
    engine, Session = _make_session("pl_dedup")
    db = Session()
    try:
        from app.db.models import Workspace
        ws = Workspace(name="Dedup WS")
        db.add(ws)
        db.commit()

        doc = Document(
            workspace_id=ws.id, filename="x.pdf", file_type="pdf",
            title="X", upload_status="processing",
            raw_text="ISO 20022 " * 100,
        )
        db.add(doc)
        db.commit()

        from app.extraction.concept_agent import extract_concepts
        concepts = extract_concepts(db, doc)

        names = [c.name.lower() for c in concepts]
        assert names.count("iso 20022") <= 1, "Duplicate concept names must be deduplicated"
    finally:
        _teardown(engine)
