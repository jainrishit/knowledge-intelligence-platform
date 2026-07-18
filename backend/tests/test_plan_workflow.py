"""
Presentation Plan workflow tests — plan generation, revision, approval,
workspace isolation, error paths, and status transitions.

All LLM calls are mocked. The plan workflow (generate → revise → approve)
is exercised against an in-memory SQLite DB that mirrors production schema.
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, StaticPool
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.db.models import (
    Base, Workspace, Document, Concept, Relationship, PresentationPlan,
)
from app.db.session import get_db

# ── Test DB setup ─────────────────────────────────────────────────────

PLAN_TEST_DB = "sqlite:///file:plan_test_db?mode=memory&cache=shared&uri=true"
plan_engine = create_engine(
    PLAN_TEST_DB,
    connect_args={"check_same_thread": False, "uri": True},
    poolclass=StaticPool,
)
PlanSession = sessionmaker(autocommit=False, autoflush=False, bind=plan_engine)
Base.metadata.create_all(bind=plan_engine)


def override_plan_db():
    db = PlanSession()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(scope="module", autouse=True)
def install_plan_db_override():
    app.dependency_overrides[get_db] = override_plan_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def reset_plan_db():
    Base.metadata.drop_all(bind=plan_engine)
    Base.metadata.create_all(bind=plan_engine)
    from app.graph.memory_manager import graph_memory_manager
    graph_memory_manager._store.clear()
    yield


client = TestClient(app, raise_server_exceptions=True)

# ── Minimal valid blueprint JSON ───────────────────────────────────────

_MINIMAL_BLUEPRINT = json.dumps({
    "deliverable_type": "client_101",
    "title": "Payments Modernisation Strategy",
    "governing_messages": [
        "ISO 20022 adoption is accelerating across major corridors.",
        "Real-time settlement is now the competitive baseline.",
    ],
    "storyline_summary": "From legacy rails to real-time settlement.",
    "slides": [
        {
            "slide_number": 1,
            "section": "Context",
            "purpose": "Set the scene.",
            "title": "Payment Rails Are Being Rebuilt for the Digital Age.",
            "layout": "large_text",
            "bullets": [],
            "notes": "Opening statement.",
            "key_insights": ["Legacy payment rails are structurally incompatible with digital-age requirements."],
            "graph_concepts": ["ISO 20022", "SWIFT"],
            "relationships_used": [],
            "patterns_used": [],
            "evidence": ["Payments Strategy 2024.pdf"],
        },
        {
            "slide_number": 2,
            "section": "Findings",
            "purpose": "Show ISO 20022 traction.",
            "title": "ISO 20022 Adoption Has Passed the Point of No Return.",
            "layout": "title_content",
            "bullets": [
                "ISO 20022 mandates are now active in 50+ countries.",
                "Swift gpi processes over 50% of cross-border payments.",
                "Richer data payloads reduce reconciliation errors by 40%.",
            ],
            "notes": "Evidence from uploaded documents.",
            "key_insights": [
                "ISO 20022 has reached critical mass and is no longer optional.",
                "Swift gpi already carries the majority of cross-border payment volume.",
            ],
            "graph_concepts": ["ISO 20022", "Swift gpi"],
            "relationships_used": ["ISO 20022 -> Swift gpi (enables)"],
            "patterns_used": [],
            "evidence": ["Payments Strategy 2024.pdf"],
        },
        {
            "slide_number": 3,
            "section": "Recommendations",
            "purpose": "Direct next steps.",
            "title": "Three Investments Unlock the Largest Near-Term Opportunity.",
            "layout": "four_boxes_wide",
            "boxes": [
                "Migrate SWIFT infrastructure to ISO 20022 by Q4 2025.",
                "Deploy real-time payment APIs across all client channels.",
                "Integrate Ripple ODL for cross-border corridors.",
                "Establish a payments centre of excellence.",
            ],
            "notes": "Prioritised roadmap.",
            "key_insights": ["Three targeted investments address the most material capability gaps."],
            "graph_concepts": ["SWIFT", "Ripple ODL", "ISO 20022"],
            "relationships_used": ["Ripple ODL -> Cross-Border Payments (enables)"],
            "patterns_used": ["Payments Modernisation Roadmap"],
            "evidence": ["Payments Strategy 2024.pdf"],
        },
    ],
    "metadata": {
        "total_slides": 3,
        "source_documents": ["Payments Strategy 2024.pdf"],
        "open_items": [],
        "generation_notes": "Payments modernisation deck.",
    },
})

_REVIEW_RESULT = json.dumps({
    "quality_score": 8,
    "content_slide_count": 2,
    "deck_size_verdict": "appropriate",
    "visual_layout_pct_non_title_content": 50,
    "strengths": ["Strong takeaway titles."],
    "issues_found": [],
    "fixes_applied": [],
    "refined_blueprint": json.loads(_MINIMAL_BLUEPRINT),
})


# ── Helpers ───────────────────────────────────────────────────────────

def _create_workspace(name="Plan WS") -> dict:
    resp = client.post("/workspaces", json={"name": name})
    assert resp.status_code == 201
    return resp.json()


def _seed_knowledge(ws_id: int, n_concepts: int = 6) -> None:
    """Insert enough concepts into the test DB for plan generation to proceed."""
    db = PlanSession()
    doc = Document(
        workspace_id=ws_id,
        filename="/fake/payments.pdf",
        file_type="pdf",
        title="Payments Strategy",
        upload_status="complete",
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    for i in range(n_concepts):
        c = Concept(
            workspace_id=ws_id,
            name=f"ISO 20022 Component {i}",
            type="Standard",
            description=f"Payment concept {i}.",
            source_document_id=doc.id,
            source_excerpt=f"Concept {i} excerpt from the source document.",
            confidence=0.85,
        )
        db.add(c)
    db.commit()
    for j in range(n_concepts - 1):
        concepts = db.query(Concept).filter(Concept.workspace_id == ws_id).all()
        if j + 1 < len(concepts):
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
    # Load graph so memory manager has it
    from app.graph.memory_manager import graph_memory_manager
    new_db = PlanSession()
    graph_memory_manager.rebuild_workspace_graph(ws_id, new_db)
    new_db.close()


# ══════════════════════════════════════════════════════════════════════
# PLAN GENERATION
# ══════════════════════════════════════════════════════════════════════

@patch("app.generation.plan_service.chat")
def test_create_plan_returns_draft_status(mock_chat):
    mock_chat.side_effect = [_MINIMAL_BLUEPRINT]
    ws = _create_workspace("PlanGen WS")
    _seed_knowledge(ws["id"])

    resp = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "draft"
    assert data["deliverable_type"] == "client_101"
    assert data["id"] > 0
    assert data["workspace_id"] == ws["id"]


@patch("app.generation.plan_service.chat")
def test_create_plan_returns_slides(mock_chat):
    mock_chat.side_effect = [_MINIMAL_BLUEPRINT]
    ws = _create_workspace("PlanSlides WS")
    _seed_knowledge(ws["id"])

    resp = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert len(data["slides"]) >= 1
    # Every slide must have a title and slide_number
    for slide in data["slides"]:
        assert "title" in slide
        assert "slide_number" in slide
        assert slide["slide_number"] >= 1


def test_create_plan_empty_workspace_returns_422():
    """Empty workspace (no knowledge) must return 422 — not 500."""
    ws = _create_workspace("Empty Plan WS")
    resp = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    assert resp.status_code == 422
    assert "knowledge" in resp.json()["detail"].lower()


def test_create_plan_nonexistent_workspace_returns_404():
    resp = client.post(
        "/workspaces/999999/presentation-plans",
        json={"type": "client_101"},
    )
    assert resp.status_code == 404


def test_create_plan_invalid_type_returns_422():
    ws = _create_workspace("BadType WS")
    resp = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "press_release"},
    )
    assert resp.status_code == 422


@patch("app.generation.plan_service.chat")
def test_create_plan_all_deliverable_types(mock_chat):
    """All three deliverable types must be accepted and produce a draft plan."""
    mock_chat.side_effect = [_MINIMAL_BLUEPRINT] * 3
    for dtype in ["client_101", "client_201", "executive_summary"]:
        ws = _create_workspace(f"Plan {dtype}")
        _seed_knowledge(ws["id"])
        resp = client.post(
            f"/workspaces/{ws['id']}/presentation-plans",
            json={"type": dtype},
        )
        assert resp.status_code == 201, f"{dtype} failed: {resp.text}"
        assert resp.json()["deliverable_type"] == dtype


# ══════════════════════════════════════════════════════════════════════
# LIST / GET
# ══════════════════════════════════════════════════════════════════════

@patch("app.generation.plan_service.chat")
def test_list_plans_for_workspace(mock_chat):
    mock_chat.side_effect = [_MINIMAL_BLUEPRINT] * 2
    ws = _create_workspace("List Plans WS")
    _seed_knowledge(ws["id"])

    # Create two plans
    for _ in range(2):
        client.post(f"/workspaces/{ws['id']}/presentation-plans", json={"type": "client_101"})

    resp = client.get(f"/workspaces/{ws['id']}/presentation-plans")
    assert resp.status_code == 200
    plans = resp.json()
    assert len(plans) == 2
    # Ordered newest-first
    assert plans[0]["id"] >= plans[1]["id"]


def test_list_plans_empty_workspace():
    ws = _create_workspace("No Plans WS")
    resp = client.get(f"/workspaces/{ws['id']}/presentation-plans")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_plans_nonexistent_workspace():
    assert client.get("/workspaces/999999/presentation-plans").status_code == 404


@patch("app.generation.plan_service.chat")
def test_get_plan_by_id(mock_chat):
    mock_chat.side_effect = [_MINIMAL_BLUEPRINT]
    ws = _create_workspace("GetPlan WS")
    _seed_knowledge(ws["id"])

    create_resp = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_201"},
    )
    plan_id = create_resp.json()["id"]

    get_resp = client.get(f"/presentation-plans/{plan_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == plan_id


def test_get_plan_not_found():
    assert client.get("/presentation-plans/999999").status_code == 404


# ══════════════════════════════════════════════════════════════════════
# PLAN REVISION
# ══════════════════════════════════════════════════════════════════════

_REVISION_RESULT = json.dumps({
    "revised_blueprint": json.loads(_MINIMAL_BLUEPRINT),
    "changes_summary": ["Added technology landscape section.", "Removed stakeholder slide."],
})


@patch("app.generation.plan_service.chat")
def test_revise_plan_returns_updated_plan(mock_chat):
    """Revision must return the updated plan with revision history populated."""
    mock_chat.side_effect = [_MINIMAL_BLUEPRINT, _REVISION_RESULT]
    ws = _create_workspace("Revise WS")
    _seed_knowledge(ws["id"])

    create_resp = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    plan_id = create_resp.json()["id"]

    revise_resp = client.patch(
        f"/presentation-plans/{plan_id}/revise",
        json={"instruction": "Add more payment modernization content."},
    )
    assert revise_resp.status_code == 200
    data = revise_resp.json()
    assert data["status"] == "draft"
    assert len(data["revision_history"]) == 1
    assert "instruction" in data["revision_history"][0]


@patch("app.generation.plan_service.chat")
def test_revise_plan_multiple_times(mock_chat):
    """Revision history accumulates across multiple revisions."""
    mock_chat.side_effect = [
        _MINIMAL_BLUEPRINT,   # create
        _REVISION_RESULT,     # first revision
        _REVISION_RESULT,     # second revision
    ]
    ws = _create_workspace("MultiRevise WS")
    _seed_knowledge(ws["id"])

    create_resp = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    plan_id = create_resp.json()["id"]

    client.patch(f"/presentation-plans/{plan_id}/revise",
                 json={"instruction": "Expand architecture section."})
    revise2_resp = client.patch(
        f"/presentation-plans/{plan_id}/revise",
        json={"instruction": "Remove stakeholder content."},
    )
    assert revise2_resp.status_code == 200
    assert len(revise2_resp.json()["revision_history"]) == 2


def test_revise_nonexistent_plan():
    assert client.patch(
        "/presentation-plans/999999/revise",
        json={"instruction": "change something"},
    ).status_code == 404


def test_revise_empty_instruction_rejected():
    """Empty instruction must be rejected by Pydantic validation."""
    ws = _create_workspace("BadRevise WS")
    # Seed a plan directly in DB
    db = PlanSession()
    plan = PresentationPlan(
        workspace_id=ws["id"],
        deliverable_type="client_101",
        blueprint_json=_MINIMAL_BLUEPRINT,
        revision_history=[],
        status="draft",
    )
    db.add(plan)
    db.commit()
    plan_id = plan.id
    db.close()

    resp = client.patch(
        f"/presentation-plans/{plan_id}/revise",
        json={"instruction": ""},
    )
    assert resp.status_code == 422


# ══════════════════════════════════════════════════════════════════════
# PLAN DELETE
# ══════════════════════════════════════════════════════════════════════

def test_delete_draft_plan():
    ws = _create_workspace("Delete Plan WS")
    db = PlanSession()
    plan = PresentationPlan(
        workspace_id=ws["id"],
        deliverable_type="client_101",
        blueprint_json=_MINIMAL_BLUEPRINT,
        revision_history=[],
        status="draft",
    )
    db.add(plan)
    db.commit()
    plan_id = plan.id
    db.close()

    assert client.delete(f"/presentation-plans/{plan_id}").status_code == 204
    assert client.get(f"/presentation-plans/{plan_id}").status_code == 404


def test_delete_nonexistent_plan():
    assert client.delete("/presentation-plans/999999").status_code == 404


# ══════════════════════════════════════════════════════════════════════
# WORKSPACE ISOLATION
# ══════════════════════════════════════════════════════════════════════

def test_plans_isolated_between_workspaces():
    """Plans in WS-A must not appear in WS-B plan list."""
    ws_a = _create_workspace("IsoA WS")
    ws_b = _create_workspace("IsoB WS")

    db = PlanSession()
    plan = PresentationPlan(
        workspace_id=ws_a["id"],
        deliverable_type="client_101",
        blueprint_json=_MINIMAL_BLUEPRINT,
        revision_history=[],
        status="draft",
    )
    db.add(plan)
    db.commit()
    db.close()

    resp = client.get(f"/workspaces/{ws_b['id']}/presentation-plans")
    assert resp.json() == [], "WS-B must not see WS-A's plans"


# ══════════════════════════════════════════════════════════════════════
# PLAN STATUS TRANSITIONS
# ══════════════════════════════════════════════════════════════════════

def test_approved_plan_cannot_be_revised():
    """Attempting to revise an approved plan must return 422."""
    ws = _create_workspace("Approved WS")
    db = PlanSession()
    plan = PresentationPlan(
        workspace_id=ws["id"],
        deliverable_type="client_101",
        blueprint_json=_MINIMAL_BLUEPRINT,
        revision_history=[],
        status="approved",
    )
    db.add(plan)
    db.commit()
    plan_id = plan.id
    db.close()

    resp = client.patch(
        f"/presentation-plans/{plan_id}/revise",
        json={"instruction": "add more content"},
    )
    assert resp.status_code == 422
    assert "approved" in resp.json()["detail"].lower()


# ══════════════════════════════════════════════════════════════════════
# PLAN SERVICE — UNIT TESTS
# ══════════════════════════════════════════════════════════════════════

def test_plan_to_out_handles_malformed_blueprint():
    """_plan_to_out must not raise when blueprint has bad/missing slide fields."""
    from app.generation.plan_service import _plan_to_out

    db = PlanSession()
    ws = Workspace(name="MalformedBP WS")
    db.add(ws)
    db.commit()

    # Blueprint with a slide missing required fields
    malformed = json.dumps({
        "deliverable_type": "client_101",
        "title": "Test",
        "slides": [
            {"slide_number": 1, "title": "Good slide", "layout": "title_content"},
            {"slide_number": 2},  # missing title
            {"title": "No number"},  # missing slide_number
        ],
    })
    plan = PresentationPlan(
        workspace_id=ws.id,
        deliverable_type="client_101",
        blueprint_json=malformed,
        revision_history=[],
        status="draft",
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)

    # Should not raise
    result = _plan_to_out(plan)
    assert result is not None
    assert len(result.slides) == 3
    db.close()


def test_blueprint_from_plan_handles_invalid_json():
    """_blueprint_from_plan must return {} on broken JSON, not raise."""
    from app.generation.plan_service import _blueprint_from_plan

    db = PlanSession()
    ws = Workspace(name="BadJSON WS")
    db.add(ws)
    db.commit()

    plan = PresentationPlan(
        workspace_id=ws.id,
        deliverable_type="client_101",
        blueprint_json="{not valid json",
        revision_history=[],
        status="draft",
    )
    db.add(plan)
    db.commit()

    result = _blueprint_from_plan(plan)
    assert result == {}
    db.close()


# ══════════════════════════════════════════════════════════════════════
# VALIDATION LAYER — unit tests
# ══════════════════════════════════════════════════════════════════════

def test_validate_removes_empty_slides():
    """Slides with no content fields at all must be removed."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Good Slide", "layout": "title_content",
             "bullets": ["ISO 20022 adoption is accelerating globally."]},
            {"slide_number": 2, "title": "Empty", "layout": "title_content"},
        ]
    }
    result = _validate_deck_spec(spec)
    assert len(result["slides"]) == 1
    assert result["slides"][0]["title"] == "Good Slide"


def test_validate_removes_label_title_with_one_bullet():
    """A label-only title (e.g. 'Overview') with ≤1 bullet must be removed."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Overview", "layout": "title_content",
             "bullets": ["One thin bullet."]},
            {"slide_number": 2, "title": "ISO 20022 Drives Real-Time Settlement",
             "layout": "title_content",
             "bullets": ["ISO 20022 is now active in 50 countries.",
                         "Swift gpi settlement time is under 2 hours.",
                         "Richer data payloads reduce reconciliation errors."]},
        ]
    }
    result = _validate_deck_spec(spec)
    assert len(result["slides"]) == 1
    assert result["slides"][0]["title"] == "ISO 20022 Drives Real-Time Settlement"


def test_validate_keeps_structural_layouts_unconditionally():
    """section_divider, cover, end_slide are never removed by the validator."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Cover", "layout": "cover"},
            {"slide_number": 2, "title": "Section One", "layout": "section_divider"},
            {"slide_number": 3, "title": "End", "layout": "end_slide"},
        ]
    }
    result = _validate_deck_spec(spec)
    assert len(result["slides"]) == 3


def test_validate_fixes_bullet_termination():
    """Bullets not ending with . ! or ? must have a period appended."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Payments Are Changing",
             "layout": "title_content",
             "bullets": [
                 "ISO 20022 adoption is accelerating",   # no period
                 "Ripple ODL removes pre-funding costs.",  # already correct
             ]},
        ]
    }
    result = _validate_deck_spec(spec)
    bullets = result["slides"][0]["bullets"]
    assert all(b.endswith((".","!","?","…")) for b in bullets)
    assert bullets[0].endswith(".")
    assert bullets[1] == "Ripple ODL removes pre-funding costs."


def test_validate_caps_title_content_bullets_at_7():
    """title_content bullets are capped at 7 to prevent overflow."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Too Many Bullets",
             "layout": "title_content",
             "bullets": [f"Bullet number {i}." for i in range(10)]},
        ]
    }
    result = _validate_deck_spec(spec)
    assert len(result["slides"][0]["bullets"]) == 7


def test_validate_demotes_four_boxes_without_boxes():
    """four_boxes_wide with no boxes field must be demoted to title_content."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Four Priorities",
             "layout": "four_boxes_wide",
             "bullets": ["Priority A.", "Priority B.", "Priority C."]},
        ]
    }
    result = _validate_deck_spec(spec)
    slide = result["slides"][0]
    # bullets → boxes promotion path (≥2 bullets available)
    assert slide["layout"] in ("four_boxes_wide", "title_content")
    # Must have some renderable content
    assert slide.get("boxes") or slide.get("bullets")


def test_validate_promotes_bullets_to_boxes_for_four_boxes():
    """four_boxes_wide with ≥2 bullets but no boxes: bullets should be promoted."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Four Priorities",
             "layout": "four_boxes_wide",
             "bullets": ["Priority A.", "Priority B.", "Priority C.", "Priority D."]},
        ]
    }
    result = _validate_deck_spec(spec)
    slide = result["slides"][0]
    assert slide.get("boxes") is not None
    assert len(slide["boxes"]) >= 2
    assert "bullets" not in slide or not slide["bullets"]


def test_validate_demotes_two_col_dividers_missing_columns():
    """two_col_dividers without columns is demoted to title_content."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Current vs Target",
             "layout": "two_col_dividers",
             "bullets": ["Current state has legacy rails.", "Target state uses ISO 20022."]},
        ]
    }
    result = _validate_deck_spec(spec)
    slide = result["slides"][0]
    # Should either fix with columns or demote; must be renderable
    assert slide.get("columns") or slide.get("bullets")


def test_validate_demotes_data_2_callouts_with_one_stat():
    """data_2_callouts with fewer than 2 stats is demoted to title_content."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Key Metrics",
             "layout": "data_2_callouts",
             "stats": [{"label": "50+", "body": "countries mandating ISO 20022"}]},
        ]
    }
    result = _validate_deck_spec(spec)
    slide = result["slides"][0]
    assert slide["layout"] == "title_content"


def test_validate_fixes_data_2_callouts_from_bullets():
    """data_2_callouts with no stats but ≥2 bullets: build stats from bullets."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Key Metrics",
             "layout": "data_2_callouts",
             "bullets": ["50+ countries mandate ISO 20022.", "$1.5T daily cross-border volume."]},
        ]
    }
    result = _validate_deck_spec(spec)
    slide = result["slides"][0]
    assert slide["layout"] == "data_2_callouts"
    assert len(slide["stats"]) == 2


def test_validate_caps_data_2_callouts_stats_at_2():
    """data_2_callouts with >2 stats must be capped at 2 (overflow guard)."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Metrics",
             "layout": "data_2_callouts",
             "stats": [
                 {"label": "A", "body": "First stat."},
                 {"label": "B", "body": "Second stat."},
                 {"label": "C", "body": "Third stat."},
             ]},
        ]
    }
    result = _validate_deck_spec(spec)
    assert len(result["slides"][0]["stats"]) == 2


def test_validate_caps_four_boxes_at_4():
    """four_boxes_wide with >4 boxes must be capped at 4."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Five Things",
             "layout": "four_boxes_wide",
             "boxes": ["A.", "B.", "C.", "D.", "E.", "F."]},
        ]
    }
    result = _validate_deck_spec(spec)
    assert len(result["slides"][0]["boxes"]) == 4


def test_validate_splits_long_bullet():
    """A bullet >200 chars is split into two shorter ones."""
    from app.generation.deliverable_service import _validate_deck_spec

    long_bullet = (
        "ISO 20022 provides a richer messaging standard for cross-border payments. "
        "It enables real-time gross settlement and reduces reconciliation overhead. "
        "Banks adopting it see measurable reductions in processing cost."
    )
    assert len(long_bullet) > 200

    spec = {
        "slides": [
            {"slide_number": 1, "title": "ISO 20022 Impact",
             "layout": "title_content",
             "bullets": [long_bullet]},
        ]
    }
    result = _validate_deck_spec(spec)
    # The long bullet must have been split into ≥2 shorter ones
    bullets = result["slides"][0]["bullets"]
    assert len(bullets) >= 2
    assert all(len(b) <= 201 for b in bullets)  # tolerance: trailing period


def test_validate_renumbers_after_removal():
    """Slide numbers must be sequential 1..N after removals."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Good", "layout": "title_content",
             "bullets": ["A good slide with content."]},
            {"slide_number": 2, "title": "Empty", "layout": "title_content"},
            {"slide_number": 3, "title": "Also Good", "layout": "title_content",
             "bullets": ["Another good slide."]},
        ]
    }
    result = _validate_deck_spec(spec)
    nums = [s["slide_number"] for s in result["slides"]]
    assert nums == list(range(1, len(nums) + 1))


def test_validate_updates_metadata_total_slides():
    """metadata.total_slides must reflect the post-removal count."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Good", "layout": "title_content",
             "bullets": ["A good slide with content."]},
            {"slide_number": 2, "title": "Empty", "layout": "title_content"},
        ],
        "metadata": {"total_slides": 2, "source_documents": []},
    }
    result = _validate_deck_spec(spec)
    assert result["metadata"]["total_slides"] == 1


def test_split_long_bullet_at_sentence_boundary():
    """_split_long_bullet splits at '. ' when available within max_chars."""
    from app.generation.deliverable_service import _split_long_bullet

    bullet = "First sentence about ISO 20022 adoption. " + "X" * 180
    result = _split_long_bullet(bullet, max_chars=80)
    assert len(result) >= 1
    # First part must end with a period
    assert result[0].endswith(".")


def test_split_long_bullet_falls_back_to_word_boundary():
    """_split_long_bullet truncates at a word boundary when no sentence break."""
    from app.generation.deliverable_service import _split_long_bullet

    bullet = "A" * 5 + " " + "B" * 5 + " " + "C" * 220
    result = _split_long_bullet(bullet, max_chars=20)
    assert len(result) == 1
    assert len(result[0]) <= 25  # word boundary + ellipsis


def test_split_long_bullet_short_input_unchanged():
    """Short bullets (≤200 chars) pass through unchanged."""
    from app.generation.deliverable_service import _split_long_bullet

    bullet = "ISO 20022 adoption is accelerating."
    assert _split_long_bullet(bullet) == [bullet]


# ══════════════════════════════════════════════════════════════════════
# SLIDE EDIT PERSISTENCE — PATCH /presentation-plans/{id}/slides
# ══════════════════════════════════════════════════════════════════════

def _seed_draft_plan(ws_id: int, blueprint: str = _MINIMAL_BLUEPRINT) -> int:
    """Insert a draft PresentationPlan and return its ID."""
    db = PlanSession()
    plan = PresentationPlan(
        workspace_id=ws_id,
        deliverable_type="client_101",
        blueprint_json=blueprint,
        revision_history=[],
        status="draft",
    )
    db.add(plan)
    db.commit()
    plan_id = plan.id
    db.close()
    return plan_id


def _slides_payload(titles: list[str]) -> list[dict]:
    """Build a minimal slides payload for PATCH /slides."""
    return [
        {
            "slide_number": i + 1,
            "title": t,
            "layout": "title_content",
            "bullets": ["ISO 20022 adoption is accelerating globally."],
        }
        for i, t in enumerate(titles)
    ]


def test_update_slides_persists_reorder():
    """Sending slides in a new order must store them renumbered sequentially."""
    ws = _create_workspace("SlideReorder WS")
    plan_id = _seed_draft_plan(ws["id"])

    # Original: slides 1,2,3 — send back in reversed order
    blueprint = json.loads(_MINIMAL_BLUEPRINT)
    reversed_titles = [s["title"] for s in reversed(blueprint["slides"])]
    payload = _slides_payload(reversed_titles)

    resp = client.patch(
        f"/presentation-plans/{plan_id}/slides",
        json={"slides": payload},
    )
    assert resp.status_code == 200
    data = resp.json()
    returned_titles = [s["title"] for s in data["slides"]]
    assert returned_titles == reversed_titles, (
        "Titles must be persisted in the submitted order"
    )
    # Slide numbers must be sequential 1..N
    nums = [s["slide_number"] for s in data["slides"]]
    assert nums == list(range(1, len(nums) + 1))


def test_update_slides_persists_removal():
    """Sending a subset of slides must remove the missing ones from the blueprint."""
    ws = _create_workspace("SlideRemove WS")
    plan_id = _seed_draft_plan(ws["id"])

    blueprint = json.loads(_MINIMAL_BLUEPRINT)
    # Keep only first slide
    kept_title = blueprint["slides"][0]["title"]
    payload = _slides_payload([kept_title])

    resp = client.patch(
        f"/presentation-plans/{plan_id}/slides",
        json={"slides": payload},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["slides"]) == 1
    assert data["slides"][0]["title"] == kept_title


def test_update_slides_renumbers_sequentially():
    """Slide numbers must always be 1..N regardless of input slide_number values."""
    ws = _create_workspace("SlideRenum WS")
    plan_id = _seed_draft_plan(ws["id"])

    # Send slides with arbitrary slide_number values
    payload = [
        {"slide_number": 99, "title": "Slide Alpha", "layout": "title_content",
         "bullets": ["ISO 20022 is accelerating."]},
        {"slide_number": 1,  "title": "Slide Beta",  "layout": "title_content",
         "bullets": ["Ripple ODL removes pre-funding."]},
    ]
    resp = client.patch(
        f"/presentation-plans/{plan_id}/slides",
        json={"slides": payload},
    )
    assert resp.status_code == 200
    nums = [s["slide_number"] for s in resp.json()["slides"]]
    assert nums == [1, 2]


def test_update_slides_preserves_blueprint_metadata():
    """Updating slides must not destroy governing_messages or storyline_summary."""
    ws = _create_workspace("SlideMetaPreserve WS")
    plan_id = _seed_draft_plan(ws["id"])

    payload = _slides_payload(["ISO 20022 Drives Modernisation"])
    resp = client.patch(
        f"/presentation-plans/{plan_id}/slides",
        json={"slides": payload},
    )
    assert resp.status_code == 200
    data = resp.json()
    # governing_messages and storyline_summary must still be present
    assert len(data["governing_messages"]) >= 1
    assert data["storyline_summary"] is not None


def test_update_slides_empty_list_rejected():
    """An empty slides list must be rejected with 422."""
    ws = _create_workspace("EmptySlides WS")
    plan_id = _seed_draft_plan(ws["id"])

    resp = client.patch(
        f"/presentation-plans/{plan_id}/slides",
        json={"slides": []},
    )
    assert resp.status_code == 422


def test_update_slides_nonexistent_plan_returns_404():
    resp = client.patch(
        "/presentation-plans/999999/slides",
        json={"slides": _slides_payload(["Any Title"])},
    )
    assert resp.status_code == 404


def test_update_slides_approved_plan_returns_422():
    """Cannot edit slides on an already-approved plan."""
    ws = _create_workspace("ApprovedSlides WS")
    db = PlanSession()
    plan = PresentationPlan(
        workspace_id=ws["id"],
        deliverable_type="client_101",
        blueprint_json=_MINIMAL_BLUEPRINT,
        revision_history=[],
        status="approved",
    )
    db.add(plan)
    db.commit()
    plan_id = plan.id
    db.close()

    resp = client.patch(
        f"/presentation-plans/{plan_id}/slides",
        json={"slides": _slides_payload(["Any Title"])},
    )
    assert resp.status_code == 422
    assert "approved" in resp.json()["detail"].lower()


def test_update_slides_status_remains_draft():
    """Editing slides on a draft plan must not change its status."""
    ws = _create_workspace("StatusCheck WS")
    plan_id = _seed_draft_plan(ws["id"])

    payload = _slides_payload(["Payment Modernisation Is Accelerating"])
    resp = client.patch(
        f"/presentation-plans/{plan_id}/slides",
        json={"slides": payload},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "draft"


def test_update_slides_revision_then_get_returns_new_order():
    """After a PATCH /slides the new order is readable via GET."""
    ws = _create_workspace("SlideGetAfter WS")
    plan_id = _seed_draft_plan(ws["id"])

    blueprint = json.loads(_MINIMAL_BLUEPRINT)
    new_order = [s["title"] for s in reversed(blueprint["slides"])]
    payload = _slides_payload(new_order)

    client.patch(f"/presentation-plans/{plan_id}/slides", json={"slides": payload})

    get_resp = client.get(f"/presentation-plans/{plan_id}")
    assert get_resp.status_code == 200
    returned_titles = [s["title"] for s in get_resp.json()["slides"]]
    assert returned_titles == new_order


def test_update_plan_slides_service_unit():
    """Unit test: update_plan_slides() directly updates blueprint in DB."""
    from app.generation.plan_service import update_plan_slides
    from app.schemas import PlanSlidesUpdate, PlanSlide

    db = PlanSession()
    ws = Workspace(name="UnitSlide WS")
    db.add(ws)
    db.commit()

    plan = PresentationPlan(
        workspace_id=ws.id,
        deliverable_type="client_101",
        blueprint_json=_MINIMAL_BLUEPRINT,
        revision_history=[],
        status="draft",
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    plan_id = plan.id

    new_slides = [
        PlanSlide(
            slide_number=1,
            title="Only Remaining Slide",
            layout="title_content",
            bullets=["ISO 20022 is now the global standard for payments messaging."],
        )
    ]
    body = PlanSlidesUpdate(slides=new_slides)
    result = update_plan_slides(db=db, plan_id=plan_id, body=body)

    assert len(result.slides) == 1
    assert result.slides[0].title == "Only Remaining Slide"
    assert result.slides[0].slide_number == 1

    # Verify DB was actually written
    db.expire_all()
    updated_plan = db.get(PresentationPlan, plan_id)
    bp = json.loads(updated_plan.blueprint_json)
    assert len(bp["slides"]) == 1
    assert bp["slides"][0]["title"] == "Only Remaining Slide"
    db.close()


# ══════════════════════════════════════════════════════════════════════
# KEY INSIGHTS ANNOTATION FIELD
# ══════════════════════════════════════════════════════════════════════

def test_plan_slide_key_insights_round_trips():
    """key_insights in the blueprint must be returned in the plan API response."""
    from app.generation.plan_service import _plan_to_out

    db = PlanSession()
    ws = Workspace(name="KeyInsights WS")
    db.add(ws)
    db.commit()

    plan = PresentationPlan(
        workspace_id=ws.id,
        deliverable_type="client_101",
        blueprint_json=_MINIMAL_BLUEPRINT,
        revision_history=[],
        status="draft",
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)

    result = _plan_to_out(plan)
    # The second slide has key_insights in the fixture
    slide2 = next(s for s in result.slides if s.slide_number == 2)
    assert len(slide2.key_insights) == 2
    assert "ISO 20022" in slide2.key_insights[0]
    db.close()


@patch("app.generation.plan_service.chat")
def test_create_plan_key_insights_in_response(mock_chat):
    """Plan creation endpoint must expose key_insights for each slide."""
    mock_chat.side_effect = [_MINIMAL_BLUEPRINT]
    ws = _create_workspace("KeyInsightsAPI WS")
    _seed_knowledge(ws["id"])

    resp = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    assert resp.status_code == 201
    data = resp.json()
    # Find any content slide that has key_insights populated
    slides_with_insights = [
        s for s in data["slides"] if s.get("key_insights")
    ]
    assert len(slides_with_insights) >= 1, (
        "At least one slide must carry key_insights in the plan response"
    )


# ══════════════════════════════════════════════════════════════════════
# APPROVE AND GENERATE — Phase 4 skip
# ══════════════════════════════════════════════════════════════════════

def test_approve_and_generate_skips_review_deck_spec():
    """approve_and_generate must NOT import or call _review_deck_spec.

    Phase 4 (LLM presentation review) is intentionally omitted from the
    plan-approval path because the user has already reviewed and approved
    the plan.  Running an LLM pass post-approval would silently change or
    remove slides the user accepted, violating the plan → review → approve contract.
    """
    from app.generation import plan_service as _ps

    # _review_deck_spec must not be in plan_service's namespace at all —
    # it was removed from the import block when Phase 4 was dropped.
    assert not hasattr(_ps, "_review_deck_spec"), (
        "_review_deck_spec must not be importable from plan_service; "
        "approve_and_generate must not call it after the user approves a plan."
    )
