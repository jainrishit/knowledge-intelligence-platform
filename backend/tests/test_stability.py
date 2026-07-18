"""
Integration tests — platform stability.

Tests every critical workflow end-to-end against an in-memory SQLite DB,
with all LLM calls mocked.  Every workflow that previously could produce
an Internal Server Error must now return either a success response or a
well-formed 4xx / descriptive 5xx — never a raw traceback.

Workflows covered:
  1.  Document upload
  2.  Document collection (list)
  3.  Knowledge Graph Explorer (get graph)
  4.  Clicking Graph Nodes (node neighbourhood)
  5.  Assistant Chat (ask question)
  6.  Assistant Chat — empty workspace (graceful degradation)
  7.  Presentation Plan Generation
  8.  Plan Revision
  9.  Plan Approval (PPTX download)
  10. Deliverable Generation (direct)
  11. Knowledge Health audit
  12. Trust & Reliability certification status

Error paths also tested:
  - Node neighbourhood for missing node → 404
  - Plan generation for empty workspace → 422
  - Assistant on missing workspace → 404
  - Graph endpoint on missing workspace → 404
"""
from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, StaticPool
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.db.models import (
    Base, Concept, Document, PresentationPlan, Relationship, Workspace,
)
from app.db.session import get_db

# ── In-memory test DB ─────────────────────────────────────────────────────────

_INTEGRATION_DB = "sqlite:///file:integration_test_db?mode=memory&cache=shared&uri=true"
_engine = create_engine(
    _INTEGRATION_DB,
    connect_args={"check_same_thread": False, "uri": True},
    poolclass=StaticPool,
)
_Session = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
Base.metadata.create_all(bind=_engine)


def _override_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module", autouse=True)
def _install_db():
    app.dependency_overrides[get_db] = _override_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _reset_db():
    Base.metadata.drop_all(bind=_engine)
    Base.metadata.create_all(bind=_engine)
    from app.graph.memory_manager import graph_memory_manager
    graph_memory_manager._store.clear()
    yield


client = TestClient(app, raise_server_exceptions=False)

# ── Shared mock responses ─────────────────────────────────────────────────────

_BLUEPRINT_JSON = json.dumps({
    "deliverable_type": "client_101",
    "title": "Payments Modernisation",
    "governing_messages": ["ISO 20022 adoption is accelerating."],
    "storyline_summary": "From legacy to real-time.",
    "slides": [
        {
            "slide_number": 1,
            "section": "Context",
            "purpose": "Set the scene.",
            "title": "Payment Rails Are Being Rebuilt.",
            "layout": "title_content",
            "bullets": [
                "ISO 20022 adoption is accelerating globally.",
                "Real-time settlement is now baseline.",
                "Legacy infrastructure blocks modernisation.",
            ],
        },
    ],
    "metadata": {
        "total_slides": 1,
        "source_documents": ["doc.pdf"],
        "open_items": [],
        "generation_notes": "Test deck.",
    },
})

_REVIEW_JSON = json.dumps({
    "quality_score": 8,
    "content_slide_count": 1,
    "deck_size_verdict": "appropriate",
    "visual_layout_pct_non_title_content": 0,
    "strengths": ["Focused."],
    "issues_found": [],
    "fixes_applied": [],
    "refined_blueprint": json.loads(_BLUEPRINT_JSON),
})

_REVISION_JSON = json.dumps({
    "revised_blueprint": json.loads(_BLUEPRINT_JSON),
    "changes_summary": ["Added architecture section."],
})


# ── Helpers ───────────────────────────────────────────────────────────────────

def _create_workspace(name: str = "Test WS") -> dict:
    r = client.post("/workspaces", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _seed_knowledge(ws_id: int, n: int = 4) -> None:
    """Insert concepts + relationships into the DB and build the graph."""
    db = _Session()
    doc = Document(
        workspace_id=ws_id,
        filename="/tmp/test.pdf",
        file_type="pdf",
        title="Test Doc",
        upload_status="complete",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    concepts = []
    for i in range(n):
        c = Concept(
            workspace_id=ws_id,
            name=f"Concept {i}",
            type="General",
            description=f"Description of concept {i}.",
            source_document_id=doc.id,
            source_excerpt=f"Excerpt {i} from source document.",
            confidence=0.85,
        )
        db.add(c)
        concepts.append(c)
    db.commit()
    for j in range(n - 1):
        db.refresh(concepts[j])
        db.refresh(concepts[j + 1])
        db.add(Relationship(
            workspace_id=ws_id,
            source_concept_id=concepts[j].id,
            target_concept_id=concepts[j + 1].id,
            relationship_type="related_to",
            source_document_id=doc.id,
            strength=0.8,
        ))
    db.commit()
    db.close()

    from app.graph.memory_manager import graph_memory_manager
    db2 = _Session()
    graph_memory_manager.rebuild_workspace_graph(ws_id, db2)
    db2.close()


# ══════════════════════════════════════════════════════════════════════════════
# 1. Document Upload
# ══════════════════════════════════════════════════════════════════════════════

def test_document_upload_accepts_pdf():
    """
    Uploading a PDF must return 202 and persist a document record.
    The ingestion pipeline runs in the background and is not awaited here.
    """
    ws = _create_workspace("Upload WS")
    fake_pdf = b"%PDF-1.4 fake content"
    r = client.post(
        f"/workspaces/{ws['id']}/documents",
        files=[("files", ("test.pdf", BytesIO(fake_pdf), "application/pdf"))],
    )
    assert r.status_code == 202, r.text
    docs = r.json()
    assert len(docs) == 1
    assert docs[0]["filename"].endswith("test.pdf")
    assert docs[0]["upload_status"] == "pending"


def test_document_upload_unknown_workspace_returns_404():
    fake_pdf = b"%PDF-1.4 fake"
    r = client.post(
        "/workspaces/999999/documents",
        files=[("files", ("test.pdf", BytesIO(fake_pdf), "application/pdf"))],
    )
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# 2. Document Collection Page
# ══════════════════════════════════════════════════════════════════════════════

def test_document_list_returns_all_documents():
    """GET /workspaces/{id}/documents must return the correct document list."""
    ws = _create_workspace("List Docs WS")
    _seed_knowledge(ws["id"])

    r = client.get(f"/workspaces/{ws['id']}/documents")
    assert r.status_code == 200
    docs = r.json()
    assert len(docs) >= 1


def test_document_list_empty_workspace_returns_empty_list():
    ws = _create_workspace("Empty Docs WS")
    r = client.get(f"/workspaces/{ws['id']}/documents")
    assert r.status_code == 200
    assert r.json() == []


def test_document_list_unknown_workspace_returns_404():
    assert client.get("/workspaces/999999/documents").status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# 3. Knowledge Graph Explorer
# ══════════════════════════════════════════════════════════════════════════════

def test_get_graph_with_knowledge_returns_nodes_and_edges():
    ws = _create_workspace("Graph WS")
    _seed_knowledge(ws["id"])

    r = client.get(f"/workspaces/{ws['id']}/graph")
    assert r.status_code == 200
    data = r.json()
    assert "nodes" in data
    assert "edges" in data
    assert len(data["nodes"]) >= 1


def test_get_graph_empty_workspace_returns_empty_graph():
    """An empty workspace must return {nodes:[], edges:[]} not a 500."""
    ws = _create_workspace("Empty Graph WS")
    r = client.get(f"/workspaces/{ws['id']}/graph")
    assert r.status_code == 200
    data = r.json()
    assert data["nodes"] == []
    assert data["edges"] == []


def test_get_graph_unknown_workspace_returns_404():
    assert client.get("/workspaces/999999/graph").status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# 4. Clicking Graph Nodes
# ══════════════════════════════════════════════════════════════════════════════

def test_node_neighbourhood_returns_concept_and_neighbours():
    ws = _create_workspace("Node WS")
    _seed_knowledge(ws["id"])

    # Get a real node ID from the concept list
    concepts_r = client.get(f"/workspaces/{ws['id']}/concepts")
    assert concepts_r.status_code == 200
    concepts = concepts_r.json()
    assert len(concepts) >= 1
    node_id = concepts[0]["id"]

    r = client.get(f"/workspaces/{ws['id']}/graph/node/{node_id}")
    assert r.status_code == 200
    data = r.json()
    assert "node" in data
    assert data["node"]["id"] == node_id
    assert "neighbours" in data
    assert "edges" in data


def test_node_neighbourhood_nonexistent_node_returns_404():
    """A missing node must return 404, never 500."""
    ws = _create_workspace("Node404 WS")
    r = client.get(f"/workspaces/{ws['id']}/graph/node/999999")
    assert r.status_code == 404


def test_node_neighbourhood_wrong_workspace_returns_404():
    """A node from workspace A must be 404 when queried via workspace B."""
    ws_a = _create_workspace("NodeA WS")
    ws_b = _create_workspace("NodeB WS")
    _seed_knowledge(ws_a["id"])

    concepts_r = client.get(f"/workspaces/{ws_a['id']}/concepts")
    node_id = concepts_r.json()[0]["id"]

    r = client.get(f"/workspaces/{ws_b['id']}/graph/node/{node_id}")
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# 5. Assistant Chat — with knowledge
# ══════════════════════════════════════════════════════════════════════════════

@patch("app.retrieval.qa_service.chat")
def test_assistant_ask_returns_answer(mock_chat):
    mock_chat.return_value = (
        "ISO 20022 is a global standard. [Source: Test Doc]"
    )
    ws = _create_workspace("Chat WS")
    _seed_knowledge(ws["id"])

    r = client.post(
        f"/workspaces/{ws['id']}/ask",
        json={"question": "What is Concept 0?"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "answer" in data
    assert len(data["answer"]) > 0


# ══════════════════════════════════════════════════════════════════════════════
# 6. Assistant Chat — empty workspace (graceful degradation)
# ══════════════════════════════════════════════════════════════════════════════

def test_assistant_empty_workspace_returns_not_found_answer():
    """Empty workspace must return a user-friendly answer, never 500."""
    ws = _create_workspace("Empty Chat WS")
    r = client.post(
        f"/workspaces/{ws['id']}/ask",
        json={"question": "What is ISO 20022?"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "answer" in data
    # Must contain the "could not find" sentinel — not a traceback
    assert "could not find" in data["answer"].lower() or "error" in data["answer"].lower()


def test_assistant_unknown_workspace_returns_404():
    r = client.post("/workspaces/999999/ask", json={"question": "hello"})
    assert r.status_code == 404


def test_assistant_chat_history_returns_list():
    ws = _create_workspace("History WS")
    r = client.get(f"/workspaces/{ws['id']}/chat-history")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ══════════════════════════════════════════════════════════════════════════════
# 7. Presentation Plan Generation
# ══════════════════════════════════════════════════════════════════════════════

@patch("app.generation.plan_service.chat")
def test_plan_generation_returns_slides(mock_chat):
    mock_chat.side_effect = [_BLUEPRINT_JSON]
    ws = _create_workspace("PlanGen WS")
    _seed_knowledge(ws["id"])

    r = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    assert r.status_code == 201
    data = r.json()
    assert data["status"] == "draft"
    assert len(data["slides"]) >= 1
    # Every slide must have a title and number
    for s in data["slides"]:
        assert "title" in s
        assert "slide_number" in s


def test_plan_generation_empty_workspace_returns_422():
    """No knowledge → 422, never 500."""
    ws = _create_workspace("EmptyPlan WS")
    r = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    assert r.status_code == 422
    assert "knowledge" in r.json()["detail"].lower()


def test_plan_generation_invalid_type_returns_422():
    ws = _create_workspace("BadType WS")
    r = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "press_release"},
    )
    assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# 8. Plan Revision
# ══════════════════════════════════════════════════════════════════════════════

@patch("app.generation.plan_service.chat")
def test_plan_revision_updates_history(mock_chat):
    mock_chat.side_effect = [_BLUEPRINT_JSON, _REVISION_JSON]
    ws = _create_workspace("Revise WS")
    _seed_knowledge(ws["id"])

    create_r = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    plan_id = create_r.json()["id"]

    r = client.patch(
        f"/presentation-plans/{plan_id}/revise",
        json={"instruction": "Add more architecture content."},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["revision_history"]) == 1
    assert data["status"] == "draft"


def test_plan_revision_empty_instruction_returns_422():
    ws = _create_workspace("BadRevise WS")
    db = _Session()
    plan = PresentationPlan(
        workspace_id=ws["id"],
        deliverable_type="client_101",
        blueprint_json=_BLUEPRINT_JSON,
        revision_history=[],
        status="draft",
    )
    db.add(plan)
    db.commit()
    plan_id = plan.id
    db.close()

    r = client.patch(
        f"/presentation-plans/{plan_id}/revise",
        json={"instruction": ""},
    )
    assert r.status_code == 422


def test_plan_revision_approved_plan_returns_422():
    ws = _create_workspace("ApprovedRevise WS")
    db = _Session()
    plan = PresentationPlan(
        workspace_id=ws["id"],
        deliverable_type="client_101",
        blueprint_json=_BLUEPRINT_JSON,
        revision_history=[],
        status="approved",
    )
    db.add(plan)
    db.commit()
    plan_id = plan.id
    db.close()

    r = client.patch(
        f"/presentation-plans/{plan_id}/revise",
        json={"instruction": "Add more content."},
    )
    assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# 9. Plan Approval → PPTX download
# ══════════════════════════════════════════════════════════════════════════════

@patch("app.generation.plan_service.chat")
@patch("app.generation.plan_service._validate_deck_spec")
def test_plan_approval_generates_pptx(mock_validate, mock_chat):
    """Approving a plan must return a PPTX (binary) response with correct headers."""
    mock_chat.side_effect = [_BLUEPRINT_JSON]

    # Fake PPTX bytes — real generator not available in tests
    fake_pptx = b"PK\x03\x04fake pptx content"

    def passthrough(spec, *args, **kwargs):
        return spec

    mock_validate.side_effect = passthrough

    ws = _create_workspace("Approve WS")
    _seed_knowledge(ws["id"])

    create_r = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    assert create_r.status_code == 201
    plan_id = create_r.json()["id"]

    # Patch the PPTX generator at the module level where it's used
    with patch(
        "app.generation.plan_service.approve_and_generate",
    ) as mock_approve:
        from app.schemas import DeliverableOut, DeliverablePptxResponse, SourceRef
        from app.db.models import Deliverable as _Deliv
        import datetime

        fake_deliverable = MagicMock(spec=_Deliv)
        fake_deliverable.id = 1
        fake_deliverable.workspace_id = ws["id"]
        fake_deliverable.type = "client_101"
        fake_deliverable.title = "Test Deck"
        fake_deliverable.content_markdown = None
        fake_deliverable.source_concept_ids = []
        fake_deliverable.source_document_ids = []
        fake_deliverable.created_at = datetime.datetime.utcnow()

        mock_approve.return_value = DeliverablePptxResponse(
            deliverable=DeliverableOut.model_validate(fake_deliverable),
            sources=[],
            pptx_bytes=fake_pptx,
            filename="test.pptx",
        )

        r = client.post(f"/presentation-plans/{plan_id}/generate")

    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    )
    assert len(r.content) > 0


# ══════════════════════════════════════════════════════════════════════════════
# 10. Deliverable Generation (direct — bypassing plan workflow)
# ══════════════════════════════════════════════════════════════════════════════

def test_deliverable_generation_empty_workspace_returns_422():
    """Empty workspace must return 422, never 500."""
    ws = _create_workspace("EmptyDeliv WS")
    r = client.post(
        f"/workspaces/{ws['id']}/deliverables",
        json={"type": "client_101"},
    )
    assert r.status_code == 422


def test_deliverable_list_returns_empty_for_new_workspace():
    ws = _create_workspace("EmptyList WS")
    r = client.get(f"/workspaces/{ws['id']}/deliverables")
    assert r.status_code == 200
    assert r.json() == []


def test_deliverable_nonexistent_returns_404():
    r = client.get("/deliverables/999999")
    assert r.status_code == 404


def test_deliverable_export_nonexistent_returns_404():
    r = client.get("/deliverables/999999/export")
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# 11. Knowledge Health audit
# ══════════════════════════════════════════════════════════════════════════════

def test_knowledge_health_audit_returns_report():
    """GET /workspaces/{id}/audit must succeed and return all expected fields."""
    ws = _create_workspace("Audit WS")
    _seed_knowledge(ws["id"])

    r = client.get(f"/workspaces/{ws['id']}/audit")
    assert r.status_code == 200
    data = r.json()
    # Required top-level fields
    for field in ("workspace_id", "memory_confidence_score", "total_concepts", "integrity"):
        assert field in data, f"missing field: {field}"
    assert data["workspace_id"] == ws["id"]


def test_knowledge_health_audit_empty_workspace_returns_report():
    """Empty workspace audit must succeed (zeros are valid)."""
    ws = _create_workspace("Empty Audit WS")
    r = client.get(f"/workspaces/{ws['id']}/audit")
    assert r.status_code == 200
    data = r.json()
    assert data["total_concepts"] == 0
    assert data["memory_confidence_score"] == 0.0


def test_knowledge_health_audit_unknown_workspace_returns_404():
    assert client.get("/workspaces/999999/audit").status_code == 404


def test_knowledge_health_audit_score_endpoint():
    """Lightweight score endpoint must return the memory_confidence_score."""
    ws = _create_workspace("Score WS")
    r = client.get(f"/workspaces/{ws['id']}/audit/score")
    assert r.status_code == 200
    assert "memory_confidence_score" in r.json()


# ══════════════════════════════════════════════════════════════════════════════
# 12. Trust & Reliability certification
# ══════════════════════════════════════════════════════════════════════════════

def test_certification_status_pending_for_new_workspace():
    """Before any run, status must be PENDING — not an error."""
    ws = _create_workspace("Cert WS")
    r = client.get(f"/workspaces/{ws['id']}/certification/status")
    assert r.status_code == 200
    data = r.json()
    assert data["certification_status"] == "PENDING"


def test_certification_status_unknown_workspace_returns_404():
    assert client.get("/workspaces/999999/certification/status").status_code == 404


def test_certification_gaps_returns_empty_when_no_run():
    ws = _create_workspace("Gaps WS")
    r = client.get(f"/workspaces/{ws['id']}/certification/gaps")
    assert r.status_code == 200
    assert r.json() == []


def test_certification_report_404_when_no_run():
    ws = _create_workspace("NoRun WS")
    r = client.get(f"/workspaces/{ws['id']}/certification")
    assert r.status_code == 404


def test_certification_run_no_concepts_returns_422():
    """Running certification on empty workspace must return 422."""
    ws = _create_workspace("EmptyCert WS")
    r = client.post(f"/workspaces/{ws['id']}/certification/run")
    assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════════════
# Global error handling — no raw 500 tracebacks
# ══════════════════════════════════════════════════════════════════════════════

def test_global_handler_converts_unexpected_errors_to_clean_500():
    """
    Even if an endpoint raises an unexpected exception, the response must be
    a JSON object with a 'detail' key — never a raw traceback.
    """
    with patch("app.api.graph.graph_memory_manager") as mock_mgr:
        # Force an unexpected RuntimeError from the graph manager
        mock_mgr.get_workspace_graph.side_effect = RuntimeError("disk failure")

        ws = _create_workspace("Crash WS")
        r = client.get(f"/workspaces/{ws['id']}/graph")

    # Must be 200 (graph returns empty) OR 500 with a clean JSON body
    assert r.status_code in (200, 500)
    if r.status_code == 500:
        data = r.json()
        assert "detail" in data
        # Must NOT contain a raw Python traceback
        assert "Traceback" not in data["detail"]
        assert "File \"" not in data["detail"]


def test_workspace_endpoints_never_expose_stack_traces():
    """Malformed workspace ID in URL must return a clean error, not a traceback."""
    r = client.get("/workspaces/not-an-integer/documents")
    # FastAPI validation → 422 Unprocessable Entity
    assert r.status_code == 422
    body = r.text
    assert "Traceback" not in body
    assert "File \"" not in body
