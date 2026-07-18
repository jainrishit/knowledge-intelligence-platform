"""
Stress tests — concurrent API requests, bulk upload simulation, large workspace
operations, graph traversal under load. All LLM calls are mocked.

Uses a file-based SQLite database in WAL mode to support concurrent readers
and a single writer safely (in-memory SQLite is not safe for multi-threaded use).
"""
from __future__ import annotations
import io
import os
import threading
import tempfile
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.db.models import Base, Concept, Relationship, Document
from app.db.session import get_db

_STRESS_DB_FILE = os.path.join(tempfile.gettempdir(), "ckip_stress_test.db")
STRESS_DB_URL = f"sqlite:///{_STRESS_DB_FILE}"

stress_engine = create_engine(
    STRESS_DB_URL,
    connect_args={"check_same_thread": False},
)


@event.listens_for(stress_engine, "connect")
def _set_wal_mode(dbapi_conn, _):
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA synchronous=NORMAL")


StressSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=stress_engine)
Base.metadata.create_all(bind=stress_engine)


def override_stress_db():
    db = StressSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module", autouse=True)
def install_stress_db_override():
    """Activate stress DB override for the entire module, then restore."""
    from app.db.session import get_db as _get_db
    app.dependency_overrides[_get_db] = override_stress_db
    yield
    app.dependency_overrides.pop(_get_db, None)
    # Clean up DB file after all stress tests finish
    stress_engine.dispose()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(_STRESS_DB_FILE + suffix)
        except FileNotFoundError:
            pass


stress_client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def reset_stress_db():
    Base.metadata.drop_all(bind=stress_engine)
    Base.metadata.create_all(bind=stress_engine)
    # Clear the graph memory singleton so each test starts with a clean cache.
    from app.graph.memory_manager import graph_memory_manager
    graph_memory_manager._store.clear()
    yield


def make_pdf_bytes(text="test") -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 72), text)
    return doc.tobytes()


def _seed_workspace_with_concepts(n_concepts: int) -> int:
    """Create a workspace with n_concepts concepts and return workspace ID."""
    resp = stress_client.post("/workspaces", json={"name": f"StressWS-{n_concepts}"})
    ws_id = resp.json()["id"]
    db = StressSessionLocal()
    doc = Document(workspace_id=ws_id, filename="bulk.pdf", file_type="pdf",
                   title="Bulk", upload_status="complete")
    db.add(doc)
    db.commit()
    db.refresh(doc)
    for i in range(n_concepts):
        db.add(Concept(
            workspace_id=ws_id,
            name=f"Concept {i}",
            type="Standard" if i % 3 == 0 else "Technology",
            description=f"Description of concept number {i} in the domain of payments.",
            source_document_id=doc.id,
            source_excerpt=f"Concept {i} is mentioned in the document at position {i}.",
            confidence=0.7 + (i % 3) * 0.1,
        ))
    db.commit()
    # Add relationships between adjacent concepts
    concepts = db.query(Concept).filter(Concept.workspace_id == ws_id).all()
    for j in range(len(concepts) - 1):
        db.add(Relationship(
            workspace_id=ws_id,
            source_concept_id=concepts[j].id,
            target_concept_id=concepts[j+1].id,
            relationship_type="related_to",
            source_document_id=doc.id,
            strength=0.6 + (j % 4) * 0.1,
        ))
    db.commit()
    db.close()
    return ws_id


# ══════════════════════════════════════════════════════════════════════
# CONCURRENT REQUESTS
# ══════════════════════════════════════════════════════════════════════

def test_concurrent_workspace_creation():
    """20 threads each create a workspace simultaneously — no deadlocks, all succeed."""
    results = []
    errors = []

    def create_ws(i):
        try:
            resp = stress_client.post("/workspaces", json={"name": f"Concurrent-{i}"})
            results.append(resp.status_code)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=create_ws, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Thread errors: {errors}"
    assert all(s == 201 for s in results), f"Non-201 responses: {[s for s in results if s != 201]}"
    resp = stress_client.get("/workspaces")
    assert len(resp.json()) == 20


def test_concurrent_reads_on_populated_workspace():
    """10 threads simultaneously query graph on the same workspace — no corruption."""
    ws_id = _seed_workspace_with_concepts(30)
    results = []
    errors = []

    def read_graph():
        try:
            resp = stress_client.get(f"/workspaces/{ws_id}/graph")
            results.append((resp.status_code, len(resp.json()["nodes"])))
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=read_graph) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Thread errors: {errors}"
    assert all(s == 200 for s, _ in results)
    # All threads should see the same 30 nodes
    node_counts = [n for _, n in results]
    assert all(n == 30 for n in node_counts), f"Inconsistent node counts: {node_counts}"


@patch("app.retrieval.qa_service.chat", return_value="Concept 5 is a standard. [Source: bulk.pdf]")
@patch("app.retrieval.qa_service._extract_keywords_llm", return_value=["Concept 5"])
def test_concurrent_ask_requests(mock_kw, mock_chat):
    """5 simultaneous ask requests on the same workspace — all return valid responses."""
    ws_id = _seed_workspace_with_concepts(20)
    results = []
    errors = []

    def ask():
        try:
            resp = stress_client.post(f"/workspaces/{ws_id}/ask",
                                      json={"question": "What is Concept 5?"})
            results.append(resp.status_code)
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=ask) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"Thread errors: {errors}"
    assert all(s == 200 for s in results)


# ══════════════════════════════════════════════════════════════════════
# BULK UPLOAD SIMULATION
# ══════════════════════════════════════════════════════════════════════

@patch("app.api.documents.run_ingestion_pipeline")
def test_bulk_upload_10_files(mock_pipeline):
    """Upload 10 files at once — all appear in document list."""
    mock_pipeline.return_value = None
    resp = stress_client.post("/workspaces", json={"name": "Bulk Upload WS"})
    ws_id = resp.json()["id"]

    files = [
        ("files", (f"doc_{i}.pdf", io.BytesIO(make_pdf_bytes(f"Document {i}")), "application/pdf"))
        for i in range(10)
    ]
    resp = stress_client.post(f"/workspaces/{ws_id}/documents", files=files)
    assert resp.status_code == 202
    assert len(resp.json()) == 10

    # All docs in listing
    listed = stress_client.get(f"/workspaces/{ws_id}/documents").json()
    assert len(listed) == 10
    assert all(d["upload_status"] == "pending" for d in listed)


# ══════════════════════════════════════════════════════════════════════
# LARGE WORKSPACE OPERATIONS
# ══════════════════════════════════════════════════════════════════════

def test_graph_endpoint_with_100_concepts():
    """Graph endpoint must handle 100 nodes and return them all correctly."""
    ws_id = _seed_workspace_with_concepts(100)
    resp = stress_client.get(f"/workspaces/{ws_id}/graph")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["nodes"]) == 100
    assert len(data["edges"]) == 99  # n-1 edges for linear chain
    assert all(isinstance(n["id"], str) for n in data["nodes"])


def test_concepts_endpoint_with_100_concepts():
    ws_id = _seed_workspace_with_concepts(100)
    resp = stress_client.get(f"/workspaces/{ws_id}/concepts")
    assert resp.status_code == 200
    assert len(resp.json()) == 100


def test_workspace_counts_accurate_at_scale():
    ws_id = _seed_workspace_with_concepts(50)
    resp = stress_client.get(f"/workspaces/{ws_id}")
    assert resp.json()["concept_count"] == 50
    assert resp.json()["document_count"] == 1


@patch("app.retrieval.qa_service.chat", return_value="Concept 50 relates to payments. [Source: bulk.pdf]")
@patch("app.retrieval.qa_service._extract_keywords_llm", return_value=["Concept 50"])
def test_ask_on_large_workspace(mock_kw, mock_chat):
    """Q&A pipeline handles workspace with 80 concepts without timeout."""
    ws_id = _seed_workspace_with_concepts(80)
    resp = stress_client.post(f"/workspaces/{ws_id}/ask",
                              json={"question": "Tell me about Concept 50"})
    assert resp.status_code == 200
    data = resp.json()
    # Must return either a sourced answer or a not-found — never crash
    assert "answer" in data
    assert "sources" in data


# ══════════════════════════════════════════════════════════════════════
# ISOLATION UNDER LOAD
# ══════════════════════════════════════════════════════════════════════

def test_multiple_workspaces_independent():
    """Three workspaces with different sizes must all be independently correct."""
    ws_ids = [_seed_workspace_with_concepts(n) for n in [10, 25, 50]]
    for ws_id, expected in zip(ws_ids, [10, 25, 50]):
        resp = stress_client.get(f"/workspaces/{ws_id}/concepts")
        assert len(resp.json()) == expected, f"ws_id={ws_id} expected {expected} concepts"


def test_delete_one_workspace_does_not_affect_others():
    ws_a = _seed_workspace_with_concepts(15)
    ws_b = _seed_workspace_with_concepts(15)
    stress_client.delete(f"/workspaces/{ws_a}")
    # WS-B must be unaffected
    resp = stress_client.get(f"/workspaces/{ws_b}/concepts")
    assert resp.status_code == 200
    assert len(resp.json()) == 15


# ══════════════════════════════════════════════════════════════════════
# DELIVERABLE UNDER LOAD
# ══════════════════════════════════════════════════════════════════════

@patch("app.api.deliverables.generate_client_material")
def test_generate_multiple_deliverables_same_workspace(mock_gen):
    """Three deliverables on the same workspace — all stored independently."""
    from app.db.models import Deliverable as DeliverableModel
    from app.schemas import DeliverableOut, DeliverablePptxResponse, SourceRef
    from datetime import datetime, timezone

    ws_id = _seed_workspace_with_concepts(20)
    fake_bytes = b"PK\x03\x04" + b"\x00" * 256

    ids = set()
    for dtype in ["client_101", "client_201", "executive_summary"]:
        db = StressSessionLocal()
        d = DeliverableModel(
            workspace_id=ws_id, type=dtype, title=f"Mock {dtype}",
            content_markdown=None, source_concept_ids=[], source_document_ids=[],
        )
        db.add(d)
        db.commit()
        db.refresh(d)
        mock_gen.return_value = DeliverablePptxResponse(
            deliverable=DeliverableOut.model_validate(d),
            sources=[SourceRef(document_id=1, document_name="bulk.pdf", excerpt="mock")],
            pptx_bytes=fake_bytes,
            filename=f"{dtype}.pptx",
        )
        db.close()

        resp = stress_client.post(f"/workspaces/{ws_id}/deliverables", json={"type": dtype})
        assert resp.status_code == 201, f"{dtype} failed: {resp.text}"
        ids.add(int(resp.headers.get("X-Deliverable-Id", "0")))

    assert len(ids) == 3  # three distinct deliverables

    listed = stress_client.get(f"/workspaces/{ws_id}/deliverables").json()
    assert len(listed) == 3
