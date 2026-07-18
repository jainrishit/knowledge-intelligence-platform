"""
Memory Audit Service tests — score computation, document health, workspace
report, graph integrity summary, deliverable readiness thresholds.

All tests use isolated in-memory SQLite sessions. No LLM calls.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine, StaticPool
from sqlalchemy.orm import sessionmaker

from app.db.models import (
    Base, Concept, ConsultingPattern, Document, IngestionAudit,
    Relationship, Workspace,
)


def _make_session(name: str):
    engine = create_engine(
        f"sqlite:///file:{name}?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False, "uri": True},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine)


def _teardown(engine):
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────

def _ws(db, name="Audit WS"):
    ws = Workspace(name=name)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


def _doc(db, ws_id, title="doc", status="complete"):
    doc = Document(
        workspace_id=ws_id, filename=f"/fake/{title}.pdf",
        file_type="pdf", title=title, upload_status=status,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def _concept(db, ws_id, doc_id, name="ISO 20022", excerpt="ISO 20022 verbatim.", confidence=0.9):
    c = Concept(
        workspace_id=ws_id, name=name, type="Standard",
        description=f"{name} description.", source_document_id=doc_id,
        source_excerpt=excerpt, confidence=confidence,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _rel(db, ws_id, src_id, tgt_id, doc_id, strength=0.8):
    r = Relationship(
        workspace_id=ws_id, source_concept_id=src_id,
        target_concept_id=tgt_id, relationship_type="related_to",
        source_document_id=doc_id, strength=strength,
    )
    db.add(r)
    db.commit()
    db.refresh(r)
    return r


def _audit(db, ws_id, doc_id, char_count=1000, chunk_count=10, page_count=5,
           concepts_extracted=8, rels_extracted=4, status="complete"):
    now = datetime.now(timezone.utc)
    ia = IngestionAudit(
        workspace_id=ws_id, document_id=doc_id,
        char_count=char_count, chunk_count=chunk_count, page_count=page_count,
        concepts_extracted=concepts_extracted, relationships_extracted=rels_extracted,
        status=status, started_at=now, completed_at=now,
    )
    db.add(ia)
    db.commit()
    db.refresh(ia)
    return ia


# ══════════════════════════════════════════════════════════════════════
# SCORE FUNCTIONS — UNIT TESTS
# ══════════════════════════════════════════════════════════════════════

class TestScoreFunctions:
    def test_ingestion_score_all_complete(self):
        from app.graph.audit_service import _ingestion_score
        assert _ingestion_score(10, 10, 0) == 1.0

    def test_ingestion_score_half_complete(self):
        from app.graph.audit_service import _ingestion_score
        score = _ingestion_score(5, 10, 0)
        assert 0.4 < score < 0.6

    def test_ingestion_score_penalised_by_failures(self):
        from app.graph.audit_service import _ingestion_score
        score_no_fail  = _ingestion_score(8, 10, 0)
        score_with_fail = _ingestion_score(8, 10, 2)
        assert score_with_fail < score_no_fail

    def test_ingestion_score_empty_workspace(self):
        from app.graph.audit_service import _ingestion_score
        assert _ingestion_score(0, 0, 0) == 0.0

    def test_extraction_density_score_zero_docs(self):
        from app.graph.audit_service import _extraction_density_score
        assert _extraction_density_score(100, 0) == 0.0

    def test_extraction_density_score_saturates_at_1(self):
        from app.graph.audit_service import _extraction_density_score
        assert _extraction_density_score(1000, 1) == 1.0

    def test_relationship_density_score_zero_concepts(self):
        from app.graph.audit_service import _relationship_density_score
        assert _relationship_density_score(50, 0) == 0.0

    def test_relationship_density_grows_with_rels(self):
        from app.graph.audit_service import _relationship_density_score
        s_low  = _relationship_density_score(5,  100)
        s_high = _relationship_density_score(50, 100)
        assert s_high > s_low

    def test_evidence_coverage_score_full(self):
        from app.graph.audit_service import _evidence_coverage_score
        assert _evidence_coverage_score(100, 100) == 1.0

    def test_evidence_coverage_score_zero(self):
        from app.graph.audit_service import _evidence_coverage_score
        assert _evidence_coverage_score(0, 100) == 0.0

    def test_graph_connectivity_score_no_unconnected(self):
        from app.graph.audit_service import _graph_connectivity_score
        # full coverage, 10 types, strength 1.0 → 0.5*1.0 + 0.3*1.0 + 0.2*1.0 = 1.0
        assert _graph_connectivity_score(1.0, 10, 1.0) == 1.0

    def test_graph_connectivity_score_zero(self):
        from app.graph.audit_service import _graph_connectivity_score
        assert _graph_connectivity_score(0.0, 0, 0.0) == 0.0

    def test_graph_connectivity_score_partial(self):
        from app.graph.audit_service import _graph_connectivity_score
        # coverage=0.8, 5 types (0.5 diversity), strength=0.6
        score = _graph_connectivity_score(0.8, 5, 0.6)
        expected = 0.5 * 0.8 + 0.3 * 0.5 + 0.2 * 0.6
        assert abs(score - expected) < 1e-9

    def test_relationship_coverage_score_all_connected(self):
        from app.graph.audit_service import _relationship_coverage_score
        assert _relationship_coverage_score(10, 0) == 1.0

    def test_relationship_coverage_score_none_connected(self):
        from app.graph.audit_service import _relationship_coverage_score
        assert _relationship_coverage_score(0, 10) == 0.0

    def test_relationship_coverage_score_no_candidates(self):
        from app.graph.audit_service import _relationship_coverage_score
        # Empty workspace — no concepts at all → 0.0 (no credit to give)
        assert _relationship_coverage_score(0, 0) == 0.0

    def test_relationship_coverage_score_connected_no_candidates(self):
        from app.graph.audit_service import _relationship_coverage_score
        # Has connected concepts, no unconnected candidates → full coverage 1.0
        assert _relationship_coverage_score(10, 0) == 1.0

    def test_pattern_coverage_score_no_patterns(self):
        from app.graph.audit_service import _pattern_coverage_score
        assert _pattern_coverage_score(0, 100) == 0.0

    def test_pattern_coverage_score_with_patterns(self):
        from app.graph.audit_service import _pattern_coverage_score
        assert _pattern_coverage_score(10, 100) == 1.0

    def test_memory_confidence_weights_sum(self):
        """Headline score with all sub-scores = 1.0 must equal 1.0."""
        from app.graph.audit_service import _memory_confidence
        assert _memory_confidence(1.0, 1.0, 1.0, 1.0, 1.0, 1.0) == 1.0

    def test_memory_confidence_all_zero(self):
        from app.graph.audit_service import _memory_confidence
        assert _memory_confidence(0.0, 0.0, 0.0, 0.0, 0.0, 0.0) == 0.0


# ══════════════════════════════════════════════════════════════════════
# DOCUMENT AUDIT — get_document_audit()
# ══════════════════════════════════════════════════════════════════════

class TestDocumentAudit:
    def test_basic_document_audit(self):
        engine, Session = _make_session("doc_audit_basic")
        db = Session()
        try:
            ws = _ws(db)
            doc = _doc(db, ws.id)
            _concept(db, ws.id, doc.id, "ISO 20022")
            _audit(db, ws.id, doc.id, char_count=5000, chunk_count=20, page_count=8)

            from app.graph.audit_service import get_document_audit
            record = get_document_audit(db, doc.id)

            assert record is not None
            assert record.document_id == doc.id
            assert record.upload_status == "complete"
            assert record.char_count == 5000
            assert record.chunk_count == 20
            assert record.page_count == 8
            assert record.concepts_extracted >= 1
        finally:
            _teardown(engine)

    def test_document_audit_none_for_missing_doc(self):
        engine, Session = _make_session("doc_audit_missing")
        db = Session()
        try:
            from app.graph.audit_service import get_document_audit
            assert get_document_audit(db, 999999) is None
        finally:
            _teardown(engine)

    def test_document_audit_failed_doc_has_zero_health(self):
        engine, Session = _make_session("doc_audit_failed")
        db = Session()
        try:
            ws = _ws(db)
            doc = _doc(db, ws.id, status="failed")
            _audit(db, ws.id, doc.id, status="failed")

            from app.graph.audit_service import get_document_audit
            record = get_document_audit(db, doc.id)
            assert record.document_health_score == 0.0
        finally:
            _teardown(engine)

    def test_document_audit_evidence_coverage(self):
        engine, Session = _make_session("doc_audit_evidence")
        db = Session()
        try:
            ws = _ws(db)
            doc = _doc(db, ws.id)
            # 3 concepts with evidence, 1 without
            for i in range(3):
                _concept(db, ws.id, doc.id, f"Concept {i}", excerpt=f"excerpt {i}")
            # one concept with no excerpt
            c = Concept(
                workspace_id=ws.id, name="No Evidence", type="General",
                source_document_id=doc.id, source_excerpt=None, confidence=0.8,
            )
            db.add(c)
            db.commit()
            _audit(db, ws.id, doc.id)

            from app.graph.audit_service import get_document_audit
            record = get_document_audit(db, doc.id)
            assert record.concepts_with_evidence == 3
            assert record.concepts_extracted == 4
            assert record.evidence_coverage_pct == 75.0
        finally:
            _teardown(engine)

    def test_document_audit_duration_computed(self):
        engine, Session = _make_session("doc_audit_duration")
        db = Session()
        try:
            ws = _ws(db)
            doc = _doc(db, ws.id)
            from datetime import timedelta
            t0 = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
            ia = IngestionAudit(
                workspace_id=ws.id, document_id=doc.id,
                char_count=100, chunk_count=5, page_count=2,
                status="complete", started_at=t0, completed_at=t0 + timedelta(seconds=42),
            )
            db.add(ia)
            db.commit()

            from app.graph.audit_service import get_document_audit
            record = get_document_audit(db, doc.id)
            assert abs(record.ingestion_duration_seconds - 42.0) < 0.1
        finally:
            _teardown(engine)


# ══════════════════════════════════════════════════════════════════════
# WORKSPACE AUDIT — get_workspace_audit()
# ══════════════════════════════════════════════════════════════════════

class TestWorkspaceAudit:
    def test_workspace_audit_returns_none_for_missing_workspace(self):
        engine, Session = _make_session("ws_audit_missing")
        db = Session()
        try:
            from app.graph.audit_service import get_workspace_audit
            assert get_workspace_audit(db, 999999) is None
        finally:
            _teardown(engine)

    def test_workspace_audit_empty_workspace(self):
        engine, Session = _make_session("ws_audit_empty")
        db = Session()
        try:
            ws = _ws(db)
            from app.graph.audit_service import get_workspace_audit
            report = get_workspace_audit(db, ws.id)
            assert report is not None
            assert report.total_documents == 0
            assert report.total_concepts == 0
            assert report.memory_confidence_score == 0.0
            assert report.client_101_ready is False
            assert report.client_201_ready is False
            assert report.executive_summary_ready is False
        finally:
            _teardown(engine)

    def test_workspace_audit_populated_workspace(self):
        engine, Session = _make_session("ws_audit_populated")
        db = Session()
        try:
            ws = _ws(db)
            doc = _doc(db, ws.id)
            concepts = [_concept(db, ws.id, doc.id, f"Concept {i}") for i in range(10)]
            for j in range(len(concepts) - 1):
                _rel(db, ws.id, concepts[j].id, concepts[j+1].id, doc.id)
            _audit(db, ws.id, doc.id, char_count=10000, chunk_count=50, page_count=20)

            from app.graph.audit_service import get_workspace_audit
            report = get_workspace_audit(db, ws.id)

            assert report.total_documents == 1
            assert report.complete_documents == 1
            assert report.total_concepts == 10
            assert report.total_relationships == 9
            assert report.memory_confidence_score > 0.0
            assert report.ingestion_score > 0.0
            assert report.evidence_coverage_score > 0.0
        finally:
            _teardown(engine)

    def test_workspace_audit_client_101_readiness(self):
        engine, Session = _make_session("ws_audit_101_ready")
        db = Session()
        try:
            ws = _ws(db)
            doc = _doc(db, ws.id)
            for i in range(6):
                _concept(db, ws.id, doc.id, f"C{i}", excerpt=f"excerpt {i}")
            _audit(db, ws.id, doc.id)

            from app.graph.audit_service import get_workspace_audit
            report = get_workspace_audit(db, ws.id)
            assert report.client_101_ready is True
        finally:
            _teardown(engine)

    def test_workspace_audit_concept_classification(self):
        """
        3 concepts in the same document: 2 connected, 1 not.
        The unconnected one shares a doc with ≥2 connected concepts, so it
        becomes an unconnected_candidate (not standalone).
        Legacy orphan_concepts field maps to unconnected_candidates.
        """
        engine, Session = _make_session("ws_audit_classification")
        db = Session()
        try:
            ws = _ws(db)
            doc = _doc(db, ws.id)
            c1 = _concept(db, ws.id, doc.id, "Connected A")
            c2 = _concept(db, ws.id, doc.id, "Connected B")
            _concept(db, ws.id, doc.id, "Unconnected C")  # no relationships
            _rel(db, ws.id, c1.id, c2.id, doc.id)
            _audit(db, ws.id, doc.id)

            from app.graph.audit_service import get_workspace_audit
            report = get_workspace_audit(db, ws.id)
            # Two concepts are connected
            assert report.integrity.connected_concepts == 2
            # One unconnected concept in a relationship-rich doc → candidate
            assert report.integrity.unconnected_candidates == 1
            assert report.integrity.standalone_concepts == 0
            # Legacy field maps to unconnected_candidates
            assert report.integrity.orphan_concepts == 1
            assert report.integrity.orphan_pct > 0.0
            # Coverage: 2 connected / (2 + 1) = 66.7%
            assert report.integrity.relationship_coverage_pct > 0.0
        finally:
            _teardown(engine)

    def test_workspace_audit_standalone_concept_not_penalised(self):
        """
        A concept in its own document (no other connected concepts) should
        be classified as standalone, not as an unconnected_candidate.
        """
        engine, Session = _make_session("ws_audit_standalone")
        db = Session()
        try:
            ws = _ws(db)
            doc1 = _doc(db, ws.id, "doc1")
            doc2 = _doc(db, ws.id, "doc2")
            # doc1: two connected concepts
            c1 = _concept(db, ws.id, doc1.id, "Connected A")
            c2 = _concept(db, ws.id, doc1.id, "Connected B")
            _rel(db, ws.id, c1.id, c2.id, doc1.id)
            # doc2: single concept with no relationships and no other connected concepts
            _concept(db, ws.id, doc2.id, "Standalone X")
            _audit(db, ws.id, doc1.id)
            _audit(db, ws.id, doc2.id)

            from app.graph.audit_service import get_workspace_audit
            report = get_workspace_audit(db, ws.id)
            assert report.integrity.connected_concepts == 2
            assert report.integrity.standalone_concepts == 1
            assert report.integrity.unconnected_candidates == 0
        finally:
            _teardown(engine)

    def test_workspace_audit_failed_document_counted(self):
        engine, Session = _make_session("ws_audit_failed")
        db = Session()
        try:
            ws = _ws(db)
            _doc(db, ws.id, "failed_doc", status="failed")

            from app.graph.audit_service import get_workspace_audit
            report = get_workspace_audit(db, ws.id)
            assert report.failed_documents == 1
            assert report.complete_documents == 0
        finally:
            _teardown(engine)

    def test_workspace_audit_document_list_populated(self):
        engine, Session = _make_session("ws_audit_doc_list")
        db = Session()
        try:
            ws = _ws(db)
            for i in range(3):
                doc = _doc(db, ws.id, f"doc{i}")
                _audit(db, ws.id, doc.id)

            from app.graph.audit_service import get_workspace_audit
            report = get_workspace_audit(db, ws.id)
            assert len(report.documents) == 3
        finally:
            _teardown(engine)
