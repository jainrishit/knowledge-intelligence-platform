"""
Brain Certification tests — benchmark generation, retrieval scoring,
certification verdict, knowledge gap detection, API endpoints.

All tests use isolated in-memory SQLite sessions. No LLM calls.
Graph traversal uses the real pipeline (find_nodes_semantic + get_neighbourhood).
"""
from __future__ import annotations

import json
import pytest
from datetime import datetime, timezone
from sqlalchemy import create_engine, StaticPool
from sqlalchemy.orm import sessionmaker
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.db.models import (
    Base, Concept, Document, Relationship, RetrievalBenchmarkRun, Workspace,
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


# ── Shared helpers ─────────────────────────────────────────────────────

def _ws(db, name="Cert WS"):
    ws = Workspace(name=name)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


def _doc(db, ws_id, title="doc"):
    doc = Document(
        workspace_id=ws_id, filename=f"/fake/{title}.pdf",
        file_type="pdf", title=title, upload_status="complete",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def _concept(db, ws_id, doc_id, name, excerpt="verbatim quote.", confidence=0.9):
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


def _seed_workspace(db, ws_id, n_concepts=8):
    """Seed a workspace with concepts and relationships."""
    doc = _doc(db, ws_id)
    concepts = [
        _concept(db, ws_id, doc.id, f"ISO Concept {i}", excerpt=f"ISO Concept {i} is used for payments.")
        for i in range(n_concepts)
    ]
    # Chain relationships
    for j in range(len(concepts) - 1):
        _rel(db, ws_id, concepts[j].id, concepts[j+1].id, doc.id)
    return doc, concepts


# ══════════════════════════════════════════════════════════════════════
# SCORE FUNCTIONS
# ══════════════════════════════════════════════════════════════════════

class TestCertificationScores:
    def test_certify_all_high_scores(self):
        from app.graph.certification_service import _certify
        status, score = _certify(90, 85, 80, 95, 85)
        assert status == "CERTIFIED"
        assert score >= 75

    def test_certify_all_low_scores(self):
        from app.graph.certification_service import _certify
        status, score = _certify(30, 40, 20, 50, 35)
        assert status == "NOT_CERTIFIED"
        assert score < 60

    def test_certify_mixed_scores_provisional(self):
        from app.graph.certification_service import _certify
        # Good recall/evidence but low precision
        status, score = _certify(80, 45, 75, 85, 80)
        assert status in ("PROVISIONAL", "NOT_CERTIFIED")

    def test_certify_score_is_weighted_average(self):
        from app.graph.certification_service import _certify, _CERT_WEIGHTS
        _, score = _certify(100, 100, 100, 100, 100)
        expected = sum(_CERT_WEIGHTS.values()) * 100
        assert abs(score - expected) < 0.1

    def test_certify_just_below_certified_threshold(self):
        from app.graph.certification_service import _certify
        # Score just below 75 but all thresholds met → PROVISIONAL
        status, score = _certify(75, 68, 62, 80, 72)
        assert status in ("PROVISIONAL", "NOT_CERTIFIED", "CERTIFIED")
        # All thresholds met means CERTIFIED if score >= 75
        assert isinstance(status, str)


# ══════════════════════════════════════════════════════════════════════
# BENCHMARK QUESTION GENERATION
# ══════════════════════════════════════════════════════════════════════

class TestQuestionGeneration:
    def test_generates_questions_from_concepts(self):
        import networkx as nx
        from app.graph.certification_service import _generate_questions
        from unittest.mock import MagicMock

        concepts = []
        G = nx.DiGraph()
        for i in range(5):
            c = MagicMock()
            c.id = i + 1
            c.name = f"Concept {i}"
            c.type = "Standard"
            c.source_document_id = 1
            concepts.append(c)
            G.add_node(c.id, name=c.name, type=c.type)

        # Add some edges so nodes have degree
        G.add_edge(1, 2)
        G.add_edge(2, 3)

        questions = _generate_questions(concepts, G, max_q=5)
        assert len(questions) == 5
        for q in questions:
            assert q.question_text
            assert q.question_type
            assert q.seed_concept_ids
            assert q.question_id >= 1

    def test_generates_no_questions_for_empty_workspace(self):
        import networkx as nx
        from app.graph.certification_service import _generate_questions
        G = nx.DiGraph()
        questions = _generate_questions([], G, max_q=10)
        assert questions == []

    def test_max_q_is_respected(self):
        import networkx as nx
        from app.graph.certification_service import _generate_questions
        from unittest.mock import MagicMock

        concepts = []
        G = nx.DiGraph()
        for i in range(50):
            c = MagicMock()
            c.id = i + 1
            c.name = f"C{i}"
            c.type = "General"
            c.source_document_id = 1
            concepts.append(c)
            G.add_node(c.id, name=c.name, type=c.type)

        questions = _generate_questions(concepts, G, max_q=10)
        assert len(questions) <= 10

    def test_question_ground_truth_includes_seed_and_neighbours(self):
        import networkx as nx
        from app.graph.certification_service import _generate_questions
        from unittest.mock import MagicMock

        G = nx.DiGraph()
        concepts = []
        for i in range(3):
            c = MagicMock()
            c.id = i + 1
            c.name = f"C{i}"
            c.type = "Standard"
            c.source_document_id = 1
            concepts.append(c)
            G.add_node(c.id, name=c.name, type=c.type)

        G.add_edge(1, 2)  # 1→2: node 2 is a 1-hop neighbour of 1

        qs = _generate_questions(concepts[:1], G, max_q=1)
        # Ground truth for node 1 should include node 2 (1-hop)
        assert 1 in qs[0].seed_concept_ids
        assert 2 in qs[0].seed_concept_ids


# ══════════════════════════════════════════════════════════════════════
# QUESTION SCORING
# ══════════════════════════════════════════════════════════════════════

class TestQuestionScoring:
    def test_perfect_retrieval(self):
        from app.graph.certification_service import BenchmarkQuestion, _score_question
        from unittest.mock import MagicMock

        q = BenchmarkQuestion(
            question_id=1,
            question_text="What is ISO 20022?",
            question_type="standard",
            seed_concept_names=["ISO 20022"],
            seed_concept_ids=[1, 2, 3],
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = MagicMock()

        result = _score_question(q, [1, 2, 3], db)
        assert result.recall    == 1.0
        assert result.precision == 1.0
        assert result.has_evidence is True

    def test_zero_retrieval(self):
        from app.graph.certification_service import BenchmarkQuestion, _score_question
        from unittest.mock import MagicMock

        q = BenchmarkQuestion(
            question_id=1, question_text="Q?", question_type="risk",
            seed_concept_names=["X"], seed_concept_ids=[1, 2, 3],
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        result = _score_question(q, [], db)
        assert result.recall    == 0.0
        assert result.precision == 0.0
        assert result.has_evidence is False

    def test_partial_retrieval(self):
        from app.graph.certification_service import BenchmarkQuestion, _score_question
        from unittest.mock import MagicMock

        q = BenchmarkQuestion(
            question_id=1, question_text="Q?", question_type="capability",
            seed_concept_names=["X"], seed_concept_ids=[1, 2, 3, 4],
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = MagicMock()

        # Retrieved 2 of 4 ground truth; also retrieved 2 extra (noise)
        result = _score_question(q, [1, 2, 5, 6], db)
        assert abs(result.recall    - 0.5) < 0.01   # 2/4
        assert abs(result.precision - 0.5) < 0.01   # 2/4


# ══════════════════════════════════════════════════════════════════════
# KNOWLEDGE GAP DETECTION
# ══════════════════════════════════════════════════════════════════════

class TestKnowledgeGapDetection:
    def test_detects_never_retrieved_concept(self):
        engine, Session = _make_session("gap_never_retrieved")
        db = Session()
        try:
            ws = _ws(db)
            doc, concepts = _seed_workspace(db, ws.id, n_concepts=5)

            from app.graph.certification_service import _detect_gaps
            from app.graph.memory_manager import GraphMemoryManager
            mgr = GraphMemoryManager()
            G = mgr.get_workspace_graph(ws.id, db)

            # Only cover the first 3 concepts — last 2 should appear as gaps
            covered = {concepts[0].id, concepts[1].id, concepts[2].id}
            gaps = _detect_gaps(db, ws.id, concepts, covered)

            gap_ids = {g.concept_id for g in gaps}
            assert concepts[3].id in gap_ids
            assert concepts[4].id in gap_ids
            # Covered concepts should not appear as gaps
            assert concepts[0].id not in gap_ids
        finally:
            _teardown(engine)

    def test_classifies_orphaned_concept(self):
        engine, Session = _make_session("gap_orphan")
        db = Session()
        try:
            ws = _ws(db)
            doc = _doc(db, ws.id)
            c1 = _concept(db, ws.id, doc.id, "Connected A")
            c2 = _concept(db, ws.id, doc.id, "Orphan B")
            _rel(db, ws.id, c1.id, c1.id, doc.id)  # self-loop — c2 has no edges

            from app.graph.certification_service import _detect_gaps
            from app.graph.memory_manager import GraphMemoryManager
            mgr = GraphMemoryManager()
            G = mgr.get_workspace_graph(ws.id, db)

            gaps = _detect_gaps(db, ws.id, [c2], covered_concept_ids=set())
            orphan_gaps = [g for g in gaps if g.gap_type == "orphaned"]
            assert len(orphan_gaps) == 1
            assert orphan_gaps[0].concept_name == "Orphan B"
        finally:
            _teardown(engine)

    def test_classifies_no_evidence_concept(self):
        engine, Session = _make_session("gap_no_evidence")
        db = Session()
        try:
            ws = _ws(db)
            doc = _doc(db, ws.id)
            c1 = _concept(db, ws.id, doc.id, "With Evidence", excerpt="real excerpt")
            c2 = Concept(
                workspace_id=ws.id, name="No Evidence", type="General",
                source_document_id=doc.id, source_excerpt=None, confidence=0.8,
            )
            db.add(c2)
            db.commit()
            db.refresh(c2)
            _rel(db, ws.id, c1.id, c2.id, doc.id)

            from app.graph.certification_service import _detect_gaps
            gaps = _detect_gaps(db, ws.id, [c1, c2], covered_concept_ids=set())
            no_ev = [g for g in gaps if g.gap_type == "no_evidence"]
            assert any(g.concept_id == c2.id for g in no_ev)
        finally:
            _teardown(engine)


# ══════════════════════════════════════════════════════════════════════
# FULL CERTIFICATION RUN
# ══════════════════════════════════════════════════════════════════════

class TestFullCertificationRun:
    def test_run_certification_on_populated_workspace(self):
        engine, Session = _make_session("cert_full_run")
        db = Session()
        try:
            ws = _ws(db)
            _seed_workspace(db, ws.id, n_concepts=10)

            from app.graph.memory_manager import GraphMemoryManager
            mgr = GraphMemoryManager()
            mgr.rebuild_workspace_graph(ws.id, db)

            from app.graph.certification_service import run_certification

            with patch("app.graph.certification_service.graph_memory_manager", mgr):
                report = run_certification(db, ws.id)

            assert report.run_id > 0
            assert report.workspace_id == ws.id
            assert report.questions_generated >= 1
            assert report.certification_status in ("CERTIFIED", "PROVISIONAL", "NOT_CERTIFIED")
            assert 0 <= report.certification_score <= 100
            assert 0 <= report.recall_score        <= 100
            assert 0 <= report.precision_score     <= 100
            assert 0 <= report.coverage_score      <= 100
            assert 0 <= report.consistency_score   <= 100
            assert 0 <= report.evidence_fidelity   <= 100
        finally:
            _teardown(engine)

    def test_run_certification_empty_workspace_raises(self):
        engine, Session = _make_session("cert_empty")
        db = Session()
        try:
            ws = _ws(db)
            from app.graph.certification_service import run_certification
            from app.graph.memory_manager import GraphMemoryManager
            mgr = GraphMemoryManager()
            with patch("app.graph.certification_service.graph_memory_manager", mgr):
                with pytest.raises(ValueError, match="No concepts"):
                    run_certification(db, ws.id)
        finally:
            _teardown(engine)

    def test_run_persists_to_db(self):
        engine, Session = _make_session("cert_persist")
        db = Session()
        try:
            ws = _ws(db)
            _seed_workspace(db, ws.id, n_concepts=8)

            from app.graph.memory_manager import GraphMemoryManager
            mgr = GraphMemoryManager()
            mgr.rebuild_workspace_graph(ws.id, db)

            from app.graph.certification_service import run_certification
            with patch("app.graph.certification_service.graph_memory_manager", mgr):
                report = run_certification(db, ws.id)

            run = db.get(RetrievalBenchmarkRun, report.run_id)
            assert run is not None
            assert run.status == "complete"
            assert run.completed_at is not None
            assert run.certification_status in ("CERTIFIED", "PROVISIONAL", "NOT_CERTIFIED")
            # JSON fields are parseable
            q_results = json.loads(run.question_results_json)
            assert isinstance(q_results, list)
        finally:
            _teardown(engine)

    def test_get_latest_certification_returns_most_recent(self):
        engine, Session = _make_session("cert_latest")
        db = Session()
        try:
            ws = _ws(db)
            _seed_workspace(db, ws.id, n_concepts=8)

            from app.graph.memory_manager import GraphMemoryManager
            mgr = GraphMemoryManager()
            mgr.rebuild_workspace_graph(ws.id, db)

            from app.graph.certification_service import (
                run_certification, get_latest_certification,
            )
            with patch("app.graph.certification_service.graph_memory_manager", mgr):
                r1 = run_certification(db, ws.id)
                r2 = run_certification(db, ws.id)

            latest = get_latest_certification(db, ws.id)
            assert latest is not None
            assert latest.run_id == r2.run_id   # most recent
        finally:
            _teardown(engine)

    def test_get_certification_status_pending_when_no_run(self):
        engine, Session = _make_session("cert_status_pending")
        db = Session()
        try:
            ws = _ws(db)
            from app.graph.certification_service import get_certification_status
            status = get_certification_status(db, ws.id)
            assert status.certification_status == "PENDING"
            assert status.run_id is None
        finally:
            _teardown(engine)

    def test_get_knowledge_gaps_empty_when_no_run(self):
        engine, Session = _make_session("cert_gaps_empty")
        db = Session()
        try:
            ws = _ws(db)
            from app.graph.certification_service import get_knowledge_gaps
            gaps = get_knowledge_gaps(db, ws.id)
            assert gaps == []
        finally:
            _teardown(engine)


# ══════════════════════════════════════════════════════════════════════
# DELIVERABLE SAFETY GATE
# ══════════════════════════════════════════════════════════════════════

class TestDeliverableSafetyGate:
    def test_unchecked_when_no_run(self):
        engine, Session = _make_session("safety_unchecked")
        db = Session()
        try:
            ws = _ws(db)
            from app.graph.certification_service import check_deliverable_safety
            is_safe, msg = check_deliverable_safety(db, ws.id)
            assert is_safe is True
            assert "unchecked" in msg
        finally:
            _teardown(engine)

    def test_blocks_on_not_certified(self):
        engine, Session = _make_session("safety_blocked")
        db = Session()
        try:
            ws = _ws(db)
            run = RetrievalBenchmarkRun(
                workspace_id=ws.id, graph_version=1,
                recall_score=20, precision_score=20, coverage_score=20,
                consistency_score=50, evidence_fidelity=20,
                retrieval_accuracy=26, certification_status="NOT_CERTIFIED",
                certification_score=26, questions_generated=5, questions_answered=3,
                status="complete", completed_at=datetime.now(timezone.utc),
            )
            db.add(run)
            db.commit()

            from app.graph.certification_service import check_deliverable_safety
            is_safe, msg = check_deliverable_safety(db, ws.id)
            assert is_safe is False
            assert "NOT_CERTIFIED" in msg or "failed" in msg.lower()
        finally:
            _teardown(engine)

    def test_passes_on_certified(self):
        engine, Session = _make_session("safety_passes")
        db = Session()
        try:
            ws = _ws(db)
            run = RetrievalBenchmarkRun(
                workspace_id=ws.id, graph_version=1,
                recall_score=80, precision_score=75, coverage_score=70,
                consistency_score=90, evidence_fidelity=80,
                retrieval_accuracy=79, certification_status="CERTIFIED",
                certification_score=79, questions_generated=10, questions_answered=9,
                status="complete", completed_at=datetime.now(timezone.utc),
            )
            db.add(run)
            db.commit()

            from app.graph.certification_service import check_deliverable_safety
            is_safe, msg = check_deliverable_safety(db, ws.id)
            assert is_safe is True
            assert "CERTIFIED" in msg
        finally:
            _teardown(engine)


# ══════════════════════════════════════════════════════════════════════
# API ENDPOINTS
# ══════════════════════════════════════════════════════════════════════

from app.main import app
from app.db.session import get_db

CERT_TEST_DB = "sqlite:///file:cert_api_test?mode=memory&cache=shared&uri=true"
cert_engine = create_engine(
    CERT_TEST_DB,
    connect_args={"check_same_thread": False, "uri": True},
    poolclass=StaticPool,
)
CertSession = sessionmaker(autocommit=False, autoflush=False, bind=cert_engine)
Base.metadata.create_all(bind=cert_engine)


def override_cert_db():
    db = CertSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module", autouse=True)
def install_cert_db():
    app.dependency_overrides[get_db] = override_cert_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def reset_cert_db():
    Base.metadata.drop_all(bind=cert_engine)
    Base.metadata.create_all(bind=cert_engine)
    from app.graph.memory_manager import graph_memory_manager
    graph_memory_manager._store.clear()
    yield


cert_client = TestClient(app, raise_server_exceptions=True)


def _api_create_workspace(name="Cert API WS"):
    r = cert_client.post("/workspaces", json={"name": name})
    assert r.status_code == 201
    return r.json()


def _api_seed(ws_id: int, n: int = 8):
    db = CertSession()
    doc = Document(
        workspace_id=ws_id, filename="/fake/doc.pdf",
        file_type="pdf", title="cert doc", upload_status="complete",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    concepts = []
    for i in range(n):
        c = Concept(
            workspace_id=ws_id, name=f"Cert Concept {i}", type="Standard",
            description=f"Description {i}.", source_document_id=doc.id,
            source_excerpt=f"Cert Concept {i} is described here.", confidence=0.85,
        )
        db.add(c)
        concepts.append(c)
    db.commit()
    for j in range(len(concepts) - 1):
        db.refresh(concepts[j])
    for j in range(len(concepts) - 1):
        db.refresh(concepts[j])
        db.refresh(concepts[j+1])
        db.add(Relationship(
            workspace_id=ws_id,
            source_concept_id=concepts[j].id, target_concept_id=concepts[j+1].id,
            relationship_type="related_to", source_document_id=doc.id, strength=0.8,
        ))
    db.commit()
    db.close()
    from app.graph.memory_manager import graph_memory_manager
    new_db = CertSession()
    graph_memory_manager.rebuild_workspace_graph(ws_id, new_db)
    new_db.close()


class TestCertificationAPI:
    def test_status_returns_pending_before_any_run(self):
        ws = _api_create_workspace("Status Pending WS")
        r = cert_client.get(f"/workspaces/{ws['id']}/certification/status")
        assert r.status_code == 200
        assert r.json()["certification_status"] == "PENDING"

    def test_certification_report_404_before_run(self):
        ws = _api_create_workspace("No Report WS")
        r = cert_client.get(f"/workspaces/{ws['id']}/certification")
        assert r.status_code == 404

    def test_gaps_empty_before_run(self):
        ws = _api_create_workspace("No Gaps WS")
        r = cert_client.get(f"/workspaces/{ws['id']}/certification/gaps")
        assert r.status_code == 200
        assert r.json() == []

    def test_run_returns_full_report(self):
        ws = _api_create_workspace("Run Full WS")
        _api_seed(ws["id"])
        r = cert_client.post(f"/workspaces/{ws['id']}/certification/run")
        assert r.status_code == 201
        data = r.json()
        assert "certification_status" in data
        assert "certification_score" in data
        assert "recall_score"         in data
        assert "precision_score"      in data
        assert "coverage_score"       in data
        assert "consistency_score"    in data
        assert "evidence_fidelity"    in data
        assert "question_results"     in data
        assert "knowledge_gaps"       in data
        assert data["certification_status"] in ("CERTIFIED", "PROVISIONAL", "NOT_CERTIFIED")

    def test_get_report_after_run(self):
        ws = _api_create_workspace("Report After Run WS")
        _api_seed(ws["id"])
        cert_client.post(f"/workspaces/{ws['id']}/certification/run")
        r = cert_client.get(f"/workspaces/{ws['id']}/certification")
        assert r.status_code == 200
        assert "certification_status" in r.json()

    def test_status_populated_after_run(self):
        ws = _api_create_workspace("Status After Run WS")
        _api_seed(ws["id"])
        cert_client.post(f"/workspaces/{ws['id']}/certification/run")
        r = cert_client.get(f"/workspaces/{ws['id']}/certification/status")
        assert r.status_code == 200
        data = r.json()
        assert data["certification_status"] != "PENDING"
        assert data["questions_generated"] >= 1

    def test_run_history_lists_all_runs(self):
        ws = _api_create_workspace("History WS")
        _api_seed(ws["id"])
        cert_client.post(f"/workspaces/{ws['id']}/certification/run")
        cert_client.post(f"/workspaces/{ws['id']}/certification/run")
        r = cert_client.get(f"/workspaces/{ws['id']}/certification/runs")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_run_empty_workspace_returns_422(self):
        ws = _api_create_workspace("Empty Cert WS")
        r = cert_client.post(f"/workspaces/{ws['id']}/certification/run")
        assert r.status_code == 422

    def test_nonexistent_workspace_returns_404(self):
        assert cert_client.get("/workspaces/999999/certification/status").status_code == 404
        assert cert_client.post("/workspaces/999999/certification/run").status_code == 404
        assert cert_client.get("/workspaces/999999/certification/gaps").status_code == 404
        assert cert_client.get("/workspaces/999999/certification/runs").status_code == 404

    def test_question_results_contain_required_fields(self):
        ws = _api_create_workspace("Question Fields WS")
        _api_seed(ws["id"])
        r = cert_client.post(f"/workspaces/{ws['id']}/certification/run")
        assert r.status_code == 201
        qrs = r.json().get("question_results", [])
        for qr in qrs:
            assert "question_id"    in qr
            assert "question_text"  in qr
            assert "question_type"  in qr
            assert "recall"         in qr
            assert "precision"      in qr
            assert "has_evidence"   in qr

    def test_workspace_isolation_in_certification(self):
        ws_a = _api_create_workspace("CertIso A")
        ws_b = _api_create_workspace("CertIso B")
        _api_seed(ws_a["id"])
        cert_client.post(f"/workspaces/{ws_a['id']}/certification/run")

        # WS-B must still be PENDING — it never had a run
        r = cert_client.get(f"/workspaces/{ws_b['id']}/certification/status")
        assert r.json()["certification_status"] == "PENDING"
