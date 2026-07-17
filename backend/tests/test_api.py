"""
FastAPI integration tests — every route, every error path, workspace isolation.
All LLM calls are mocked. Uses shared-cache in-memory SQLite for test isolation.
"""
import io
import pytest
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.db.models import Base, Workspace, Document, Concept, Relationship, ConsultingPattern, Deliverable, ChatMessage
from app.db.session import get_db

# ── Test DB setup ─────────────────────────────────────────────────────

TEST_DATABASE_URL = "sqlite:///file::memory:?cache=shared&uri=true"
test_engine = create_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False, "uri": True},
)
TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
Base.metadata.create_all(bind=test_engine)


def override_get_db():
    db = TestSessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module", autouse=True)
def install_api_db_override():
    """Install and clean up the test DB override for this module."""
    app.dependency_overrides[get_db] = override_get_db
    yield
    app.dependency_overrides.pop(get_db, None)


client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def reset_db(install_api_db_override):
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)
    # Clear the graph memory singleton so each test starts with a clean cache.
    from app.graph.memory_manager import graph_memory_manager
    graph_memory_manager._store.clear()
    yield


# ── Helpers ───────────────────────────────────────────────────────────

def make_pdf_bytes(text: str = "ISO 20022 is a payment messaging standard.") -> bytes:
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 72), text)
    return doc.tobytes()


def make_docx_bytes(paragraphs: list) -> bytes:
    import io as _io
    from docx import Document as DDoc
    doc = DDoc()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = _io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _create_workspace(name="Test WS", description=None):
    resp = client.post("/workspaces", json={"name": name, "description": description})
    assert resp.status_code == 201
    return resp.json()


def _seed_concept(ws_id, name="ISO 20022", excerpt="ISO 20022 is a standard.", confidence=0.9):
    db = TestSessionLocal()
    doc = Document(workspace_id=ws_id, filename=f"/uploads/{name}.pdf",
                   file_type="pdf", title=f"{name}.pdf", upload_status="complete")
    db.add(doc)
    db.commit()
    db.refresh(doc)
    concept = Concept(
        workspace_id=ws_id, name=name, type="Payment Standard",
        description=f"Description of {name}",
        source_document_id=doc.id,
        source_excerpt=excerpt,
        confidence=confidence,
    )
    db.add(concept)
    db.commit()
    db.refresh(concept)
    # Capture scalar IDs before expunge so callers can access them after the session closes.
    doc.id, concept.id  # trigger attribute load while session is still open
    db.expunge_all()
    db.close()
    return doc, concept


# ══════════════════════════════════════════════════════════════════════
# HEALTH
# ══════════════════════════════════════════════════════════════════════

def test_health_endpoint():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ══════════════════════════════════════════════════════════════════════
# WORKSPACE CRUD
# ══════════════════════════════════════════════════════════════════════

def test_create_workspace_minimal():
    resp = client.post("/workspaces", json={"name": "Payments"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Payments"
    assert data["id"] > 0
    assert data["document_count"] == 0
    assert data["concept_count"] == 0


def test_create_workspace_with_description():
    resp = client.post("/workspaces", json={"name": "Digital Assets", "description": "Tokenisation workspace"})
    assert resp.status_code == 201
    assert resp.json()["description"] == "Tokenisation workspace"


def test_create_workspace_name_required():
    resp = client.post("/workspaces", json={"description": "no name"})
    assert resp.status_code == 422


def test_create_workspace_name_empty_string():
    resp = client.post("/workspaces", json={"name": ""})
    assert resp.status_code == 422


def test_list_workspaces_empty():
    resp = client.get("/workspaces")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_workspaces_multiple():
    for name in ["WS-A", "WS-B", "WS-C"]:
        client.post("/workspaces", json={"name": name})
    resp = client.get("/workspaces")
    assert resp.status_code == 200
    names = [w["name"] for w in resp.json()]
    assert "WS-A" in names
    assert "WS-B" in names
    assert "WS-C" in names


def test_get_workspace_detail():
    ws = _create_workspace("Detail WS")
    resp = client.get(f"/workspaces/{ws['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == ws["id"]
    assert resp.json()["name"] == "Detail WS"


def test_get_workspace_counts_update():
    """document_count and concept_count reflect DB state."""
    ws = _create_workspace("Count WS")
    doc, concept = _seed_concept(ws["id"])
    resp = client.get(f"/workspaces/{ws['id']}")
    assert resp.json()["document_count"] == 1
    assert resp.json()["concept_count"] == 1


def test_get_workspace_not_found():
    resp = client.get("/workspaces/999999")
    assert resp.status_code == 404


def test_delete_workspace_cascades():
    """Deleting workspace removes its concepts and documents."""
    ws = _create_workspace("Cascade WS")
    doc, concept = _seed_concept(ws["id"])
    resp = client.delete(f"/workspaces/{ws['id']}")
    assert resp.status_code == 204
    # workspace gone
    assert client.get(f"/workspaces/{ws['id']}").status_code == 404


def test_delete_nonexistent_workspace():
    assert client.delete("/workspaces/999999").status_code == 404


# ══════════════════════════════════════════════════════════════════════
# DOCUMENTS
# ══════════════════════════════════════════════════════════════════════

@patch("app.api.documents.run_ingestion_pipeline")
def test_upload_pdf(mock_pipeline):
    mock_pipeline.return_value = None
    ws = _create_workspace()
    files = [("files", ("report.pdf", io.BytesIO(make_pdf_bytes()), "application/pdf"))]
    resp = client.post(f"/workspaces/{ws['id']}/documents", files=files)
    assert resp.status_code == 202
    doc = resp.json()[0]
    assert doc["file_type"] == "pdf"
    assert doc["upload_status"] == "pending"
    assert mock_pipeline.called


@patch("app.api.documents.run_ingestion_pipeline")
def test_upload_docx(mock_pipeline):
    mock_pipeline.return_value = None
    ws = _create_workspace()
    docx = make_docx_bytes(["SWIFT is the global messaging network."])
    files = [("files", ("spec.docx", io.BytesIO(docx), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))]
    resp = client.post(f"/workspaces/{ws['id']}/documents", files=files)
    assert resp.status_code == 202
    assert resp.json()[0]["file_type"] == "docx"


@patch("app.api.documents.run_ingestion_pipeline")
def test_upload_multiple_files(mock_pipeline):
    mock_pipeline.return_value = None
    ws = _create_workspace()
    files = [
        ("files", ("a.pdf", io.BytesIO(make_pdf_bytes("A")), "application/pdf")),
        ("files", ("b.pdf", io.BytesIO(make_pdf_bytes("B")), "application/pdf")),
    ]
    resp = client.post(f"/workspaces/{ws['id']}/documents", files=files)
    assert resp.status_code == 202
    assert len(resp.json()) == 2


def test_upload_unsupported_type():
    ws = _create_workspace()
    files = [("files", ("image.png", io.BytesIO(b"fake"), "image/png"))]
    assert client.post(f"/workspaces/{ws['id']}/documents", files=files).status_code == 422


def test_upload_to_nonexistent_workspace():
    files = [("files", ("x.pdf", io.BytesIO(make_pdf_bytes()), "application/pdf"))]
    assert client.post("/workspaces/999999/documents", files=files).status_code == 404


def test_list_documents():
    ws = _create_workspace()
    doc, _ = _seed_concept(ws["id"])
    resp = client.get(f"/workspaces/{ws['id']}/documents")
    assert resp.status_code == 200
    assert len(resp.json()) == 1


def test_get_document_detail():
    ws = _create_workspace()
    doc, _ = _seed_concept(ws["id"])
    resp = client.get(f"/documents/{doc.id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == doc.id
    assert resp.json()["file_type"] == "pdf"


def test_get_document_not_found():
    assert client.get("/documents/999999").status_code == 404


@patch("app.api.documents.run_ingestion_pipeline")
def test_delete_document(mock_pipeline):
    mock_pipeline.return_value = None
    ws = _create_workspace()
    files = [("files", ("del.pdf", io.BytesIO(make_pdf_bytes()), "application/pdf"))]
    doc_id = client.post(f"/workspaces/{ws['id']}/documents", files=files).json()[0]["id"]
    assert client.delete(f"/documents/{doc_id}").status_code == 204
    assert client.get(f"/documents/{doc_id}").status_code == 404


def test_delete_document_not_found():
    assert client.delete("/documents/999999").status_code == 404


# ══════════════════════════════════════════════════════════════════════
# KNOWLEDGE GRAPH
# ══════════════════════════════════════════════════════════════════════

def test_concepts_empty():
    ws = _create_workspace()
    resp = client.get(f"/workspaces/{ws['id']}/concepts")
    assert resp.status_code == 200
    assert resp.json() == []


def test_concepts_populated():
    ws = _create_workspace()
    _seed_concept(ws["id"], "ISO 20022")
    _seed_concept(ws["id"], "SWIFT")
    resp = client.get(f"/workspaces/{ws['id']}/concepts")
    assert resp.status_code == 200
    names = [c["name"] for c in resp.json()]
    assert "ISO 20022" in names
    assert "SWIFT" in names


def test_relationships_empty():
    ws = _create_workspace()
    resp = client.get(f"/workspaces/{ws['id']}/relationships")
    assert resp.status_code == 200
    assert resp.json() == []


def test_relationships_populated():
    ws = _create_workspace()
    doc, c1 = _seed_concept(ws["id"], "ISO 20022")
    _, c2 = _seed_concept(ws["id"], "SWIFT")
    db = TestSessionLocal()
    rel = Relationship(
        workspace_id=ws["id"],
        source_concept_id=c1.id, target_concept_id=c2.id,
        relationship_type="related_to", source_document_id=doc.id,
        strength=0.8,
    )
    db.add(rel)
    db.commit()
    db.close()
    resp = client.get(f"/workspaces/{ws['id']}/relationships")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["relationship_type"] == "related_to"


def test_patterns_empty():
    ws = _create_workspace()
    resp = client.get(f"/workspaces/{ws['id']}/patterns")
    assert resp.status_code == 200
    assert resp.json() == []


def test_patterns_populated():
    ws = _create_workspace()
    db = TestSessionLocal()
    pattern = ConsultingPattern(
        workspace_id=ws["id"],
        name="Migration Pattern",
        problem_statement="Legacy payment systems need upgrading.",
        ibm_approach=["Assess current state", "Design target architecture", "Migrate"],
        related_concept_ids=[],
        source_document_ids=[],
    )
    db.add(pattern)
    db.commit()
    db.close()
    resp = client.get(f"/workspaces/{ws['id']}/patterns")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
    assert resp.json()[0]["name"] == "Migration Pattern"


def test_graph_empty():
    ws = _create_workspace()
    resp = client.get(f"/workspaces/{ws['id']}/graph")
    assert resp.status_code == 200
    assert resp.json()["nodes"] == []
    assert resp.json()["edges"] == []


def test_graph_populated():
    ws = _create_workspace()
    doc, c1 = _seed_concept(ws["id"], "ISO 20022")
    _, c2 = _seed_concept(ws["id"], "SWIFT")
    db = TestSessionLocal()
    db.add(Relationship(
        workspace_id=ws["id"],
        source_concept_id=c1.id, target_concept_id=c2.id,
        relationship_type="related_to", source_document_id=doc.id, strength=0.8,
    ))
    db.commit()
    db.close()
    resp = client.get(f"/workspaces/{ws['id']}/graph")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["nodes"]) == 2
    assert len(data["edges"]) == 1
    # nodes have string IDs (React Flow requirement)
    assert all(isinstance(n["id"], str) for n in data["nodes"])
    # edges carry strength data
    assert data["edges"][0]["data"]["strength"] == 0.8


def test_graph_node_neighbourhood():
    ws = _create_workspace()
    doc, c1 = _seed_concept(ws["id"], "ISO 20022")
    _, c2 = _seed_concept(ws["id"], "SWIFT")
    db = TestSessionLocal()
    db.add(Relationship(
        workspace_id=ws["id"],
        source_concept_id=c1.id, target_concept_id=c2.id,
        relationship_type="related_to", source_document_id=doc.id, strength=0.8,
    ))
    db.commit()
    db.close()
    resp = client.get(f"/workspaces/{ws['id']}/graph/node/{c1.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["node"]["id"] == c1.id
    assert data["node"]["name"] == "ISO 20022"
    assert any(n["name"] == "SWIFT" for n in data["neighbours"])
    assert data["source_document"]["id"] == doc.id


def test_graph_node_not_found():
    ws = _create_workspace()
    assert client.get(f"/workspaces/{ws['id']}/graph/node/999999").status_code == 404


def test_graph_node_wrong_workspace():
    """Node from workspace A must not be accessible via workspace B's URL."""
    ws_a = _create_workspace("WS-A")
    ws_b = _create_workspace("WS-B")
    _, c = _seed_concept(ws_a["id"], "ISO 20022")
    assert client.get(f"/workspaces/{ws_b['id']}/graph/node/{c.id}").status_code == 404


# ══════════════════════════════════════════════════════════════════════
# WORKSPACE ISOLATION
# ══════════════════════════════════════════════════════════════════════

def test_concepts_isolated_between_workspaces():
    """Concepts in WS-A must not appear in WS-B."""
    ws_a = _create_workspace("Isolation-A")
    ws_b = _create_workspace("Isolation-B")
    _seed_concept(ws_a["id"], "ISO 20022")
    resp = client.get(f"/workspaces/{ws_b['id']}/concepts")
    assert resp.status_code == 200
    assert resp.json() == []


def test_graph_isolated_between_workspaces():
    ws_a = _create_workspace("Graph-A")
    ws_b = _create_workspace("Graph-B")
    _seed_concept(ws_a["id"], "SWIFT")
    resp_b = client.get(f"/workspaces/{ws_b['id']}/graph")
    assert resp_b.json()["nodes"] == []


@patch("app.retrieval.qa_service._extract_keywords_llm")
def test_ask_isolated_to_workspace(mock_kw):
    """Ask on WS-B cannot see concepts only in WS-A."""
    mock_kw.return_value = ["ISO 20022"]
    ws_a = _create_workspace("Ask-A")
    ws_b = _create_workspace("Ask-B")
    _seed_concept(ws_a["id"], "ISO 20022")
    resp = client.post(f"/workspaces/{ws_b['id']}/ask", json={"question": "What is ISO 20022?"})
    assert resp.status_code == 200
    data = resp.json()
    # WS-B has no concepts — must return not-found, not a leaked answer
    assert "could not find" in data["answer"].lower() or "not found" in data["answer"].lower()
    assert data["sources"] == []


# ══════════════════════════════════════════════════════════════════════
# ASSISTANT
# ══════════════════════════════════════════════════════════════════════

@patch("app.retrieval.qa_service._extract_keywords_llm")
def test_ask_empty_workspace_returns_not_found(mock_kw):
    mock_kw.return_value = ["payments"]
    ws = _create_workspace()
    resp = client.post(f"/workspaces/{ws['id']}/ask", json={"question": "What is ISO 20022?"})
    assert resp.status_code == 200
    data = resp.json()
    assert "could not find" in data["answer"].lower() or "not found" in data["answer"].lower()
    assert data["sources"] == []


@patch("app.retrieval.qa_service.chat", return_value="ISO 20022 is a standard. [Source: doc.pdf]")
@patch("app.retrieval.qa_service._extract_keywords_llm", return_value=["ISO 20022"])
def test_ask_with_concepts_returns_sources(mock_kw, mock_chat):
    ws = _create_workspace()
    _seed_concept(ws["id"], "ISO 20022")
    resp = client.post(f"/workspaces/{ws['id']}/ask", json={"question": "What is ISO 20022?"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["sources"]) > 0
    assert data["sources"][0]["document_id"] > 0
    assert data["sources"][0]["document_name"] != ""


@patch("app.retrieval.qa_service.chat", return_value="ISO 20022 is a standard. [Source: doc.pdf]")
@patch("app.retrieval.qa_service._extract_keywords_llm", return_value=["ISO 20022"])
def test_ask_stores_chat_history(mock_kw, mock_chat):
    ws = _create_workspace()
    _seed_concept(ws["id"], "ISO 20022")
    client.post(f"/workspaces/{ws['id']}/ask", json={"question": "Tell me about ISO 20022?"})
    hist = client.get(f"/workspaces/{ws['id']}/chat-history").json()
    roles = [m["role"] for m in hist]
    assert "user" in roles
    assert "assistant" in roles


def test_ask_empty_question_rejected():
    ws = _create_workspace()
    resp = client.post(f"/workspaces/{ws['id']}/ask", json={"question": ""})
    assert resp.status_code == 422


def test_ask_nonexistent_workspace():
    resp = client.post("/workspaces/999999/ask", json={"question": "anything"})
    assert resp.status_code == 404


def test_chat_history_empty():
    ws = _create_workspace()
    resp = client.get(f"/workspaces/{ws['id']}/chat-history")
    assert resp.status_code == 200
    assert resp.json() == []


def test_chat_history_ordered_asc():
    """History is returned oldest-first."""
    ws = _create_workspace()
    db = TestSessionLocal()
    from datetime import datetime, timedelta
    t0 = datetime(2024, 1, 1, 10, 0, 0)
    db.add(ChatMessage(workspace_id=ws["id"], role="user", content="first", created_at=t0))
    db.add(ChatMessage(workspace_id=ws["id"], role="assistant", content="second", created_at=t0 + timedelta(seconds=1)))
    db.commit()
    db.close()
    hist = client.get(f"/workspaces/{ws['id']}/chat-history").json()
    assert hist[0]["content"] == "first"
    assert hist[1]["content"] == "second"


# ══════════════════════════════════════════════════════════════════════
# DELIVERABLES
# ══════════════════════════════════════════════════════════════════════

# ── Shared PPTX mock factory ──────────────────────────────────────────
#
# The deliverable API now returns raw PPTX bytes (not JSON).
# We mock `generate_client_material` at the service layer so tests never
# hit the LLM or the IBM template file.
# The mock returns a minimal DeliverablePptxResponse built from a real
# DB record so list / get routes still work.

def _make_pptx_response(db_session, workspace_id: int, dtype: str, focus_area=None):
    """Insert a Deliverable row and return a DeliverablePptxResponse wrapping it."""
    from app.db.models import Deliverable as DeliverableModel
    from app.schemas import DeliverableOut, DeliverablePptxResponse, SourceRef
    from datetime import datetime, timezone

    # Minimal valid PPTX-like bytes (just needs to be non-empty)
    fake_bytes = b"PK\x03\x04" + b"\x00" * 256

    d = DeliverableModel(
        workspace_id=workspace_id,
        type=dtype,
        title=f"Mock {dtype}",
        content_markdown=None,
        source_concept_ids=[],
        source_document_ids=[],
    )
    db_session.add(d)
    db_session.commit()
    db_session.refresh(d)

    return DeliverablePptxResponse(
        deliverable=DeliverableOut.model_validate(d),
        sources=[SourceRef(document_id=1, document_name="mock_doc.pdf", excerpt="mock")],
        pptx_bytes=fake_bytes,
        filename=f"{dtype}_{workspace_id}_{d.id}.pptx",
    )


@patch("app.api.deliverables.generate_client_material")
def test_create_deliverable_client_101(mock_gen):
    ws = _create_workspace()
    db = TestSessionLocal()
    mock_gen.return_value = _make_pptx_response(db, ws["id"], "client_101")
    db.close()

    resp = client.post(f"/workspaces/{ws['id']}/deliverables",
                       json={"type": "client_101"})
    assert resp.status_code == 201
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert "X-Deliverable-Id" in resp.headers
    assert "X-Deliverable-Title" in resp.headers
    assert len(resp.content) > 0


@patch("app.api.deliverables.generate_client_material")
def test_create_deliverable_all_types(mock_gen):
    ws = _create_workspace()
    for dtype in ["client_101", "client_201", "executive_summary"]:
        db = TestSessionLocal()
        mock_gen.return_value = _make_pptx_response(db, ws["id"], dtype)
        db.close()
        resp = client.post(f"/workspaces/{ws['id']}/deliverables",
                           json={"type": dtype})
        assert resp.status_code == 201, f"{dtype} failed: {resp.text}"


def test_create_deliverable_invalid_type():
    ws = _create_workspace()
    resp = client.post(f"/workspaces/{ws['id']}/deliverables", json={"type": "blog_post"})
    assert resp.status_code == 422


@patch("app.api.deliverables.generate_client_material", side_effect=ValueError("No knowledge found"))
def test_create_deliverable_empty_workspace_fails(mock_gen):
    ws = _create_workspace()
    resp = client.post(f"/workspaces/{ws['id']}/deliverables",
                       json={"type": "client_101"})
    assert resp.status_code == 422


def test_list_deliverables_empty():
    ws = _create_workspace()
    assert client.get(f"/workspaces/{ws['id']}/deliverables").json() == []


@patch("app.api.deliverables.generate_client_material")
def test_list_deliverables_populated(mock_gen):
    ws = _create_workspace()
    db = TestSessionLocal()
    mock_gen.return_value = _make_pptx_response(db, ws["id"], "client_101")
    db.close()
    client.post(f"/workspaces/{ws['id']}/deliverables", json={"type": "client_101"})
    resp = client.get(f"/workspaces/{ws['id']}/deliverables")
    assert len(resp.json()) >= 1


def test_get_deliverable_by_id():
    """Seed a Deliverable directly and verify the GET route returns it."""
    ws = _create_workspace()
    db = TestSessionLocal()
    from app.db.models import Deliverable as DeliverableModel
    d = DeliverableModel(
        workspace_id=ws["id"], type="client_101", title="Seeded",
        content_markdown=None, source_concept_ids=[], source_document_ids=[],
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    deliv_id = d.id
    db.close()

    resp = client.get(f"/deliverables/{deliv_id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == deliv_id


def test_get_deliverable_not_found():
    assert client.get("/deliverables/999999").status_code == 404


@patch("app.api.deliverables.generate_client_material")
def test_export_deliverable_pptx(mock_gen):
    """Export re-runs generation and returns PPTX bytes."""
    ws = _create_workspace()
    db = TestSessionLocal()
    from app.db.models import Deliverable as DeliverableModel
    d = DeliverableModel(
        workspace_id=ws["id"], type="client_101", title="Export Test",
        content_markdown=None, source_concept_ids=[], source_document_ids=[],
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    deliv_id = d.id

    mock_gen.return_value = _make_pptx_response(db, ws["id"], "client_101")
    db.close()

    resp = client.get(f"/deliverables/{deliv_id}/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert len(resp.content) > 0


def test_delete_deliverable():
    """DELETE returns 204 and the record is gone."""
    ws = _create_workspace()
    db = TestSessionLocal()
    from app.db.models import Deliverable as DeliverableModel
    d = DeliverableModel(
        workspace_id=ws["id"], type="client_201", title="To Delete",
        content_markdown=None, source_concept_ids=[], source_document_ids=[],
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    deliv_id = d.id
    db.close()

    assert client.delete(f"/deliverables/{deliv_id}").status_code == 204
    assert client.get(f"/deliverables/{deliv_id}").status_code == 404


# ══════════════════════════════════════════════════════════════════════
# GROUNDING RULE
# ══════════════════════════════════════════════════════════════════════

@patch("app.retrieval.qa_service._extract_keywords_llm", return_value=["blockchain"])
def test_ask_unrelated_question_returns_not_found(mock_kw):
    """Question about a topic absent from the corpus must return not-found, never a fabricated answer."""
    ws = _create_workspace()
    _seed_concept(ws["id"], "ISO 20022")
    resp = client.post(f"/workspaces/{ws['id']}/ask", json={"question": "How does photosynthesis work?"})
    data = resp.json()
    assert data["sources"] == [] or (
        "could not find" in data["answer"].lower() or "not found" in data["answer"].lower()
    )


@patch("app.retrieval.qa_service.chat", return_value="ISO 20022 is a standard. [Source: doc.pdf]")
@patch("app.retrieval.qa_service._extract_keywords_llm", return_value=["ISO 20022"])
def test_ask_response_never_missing_sources_when_answered(mock_kw, mock_chat):
    """Any non-not-found answer must carry at least one source."""
    ws = _create_workspace()
    _seed_concept(ws["id"], "ISO 20022")
    data = client.post(f"/workspaces/{ws['id']}/ask", json={"question": "What is ISO 20022?"}).json()
    if "could not find" not in data["answer"].lower() and "not found" not in data["answer"].lower():
        assert len(data["sources"]) > 0
