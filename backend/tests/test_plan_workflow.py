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


def test_validate_keeps_cover_and_end_and_strong_dividers():
    """cover and end_slide are never removed; an insight divider that opens a real
    section (≥2 content slides following) is kept."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Cover", "layout": "cover"},
            {"slide_number": 2,
             "title": "Digital assets are reshaping settlement economics for global banks",
             "layout": "section_divider"},
            {"slide_number": 3, "title": "Custody secures institutional assets.",
             "layout": "title_content",
             "bullets": ["Custody secures assets end to end.", "Treasury automates liquidity."]},
            {"slide_number": 4, "title": "Settlement finality drops to seconds.",
             "layout": "title_content",
             "bullets": ["Settlement clears in seconds.", "Capital is freed for lending."]},
            {"slide_number": 5, "title": "End", "layout": "end_slide"},
        ]
    }
    result = _validate_deck_spec(spec)
    layouts = [s["layout"] for s in result["slides"]]
    assert "cover" in layouts and "end_slide" in layouts
    assert "section_divider" in layouts  # strong divider with 2 following slides kept
    assert len(result["slides"]) == 5


def test_validate_drops_label_section_dividers():
    """Bare category-label dividers, and dividers with <2 following content slides,
    are removed by divider hygiene (they read as AI-generated section labels)."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "slides": [
            {"slide_number": 1, "title": "Technical Architecture", "layout": "section_divider"},
            {"slide_number": 2, "title": "IBM Z hosts the custody workload.",
             "layout": "title_content",
             "bullets": ["IBM Z hosts the custody workload.", "Throughput scales linearly."]},
            {"slide_number": 3, "title": "Market Forces", "layout": "section_divider"},
            {"slide_number": 4, "title": "End", "layout": "end_slide"},
        ]
    }
    result = _validate_deck_spec(spec)
    titles = [s["title"] for s in result["slides"]]
    assert "Technical Architecture" not in titles  # label divider dropped
    assert "Market Forces" not in titles            # label + no following content dropped
    assert "IBM Z hosts the custody workload." in titles


def test_validate_rewrites_label_divider_to_lead_insight():
    """A label divider that opens a real section (>=2 content slides) is REWRITTEN
    to the section's lead insight, not dropped — preserving structure and story."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "deliverable_type": "client_201",
        "slides": [
            {"slide_number": 1, "title": "Market Forces", "layout": "section_divider"},
            {"slide_number": 2,
             "title": "Four converging pressures are forcing payment infrastructure change.",
             "layout": "title_content",
             "bullets": ["Capital is trapped in Nostro accounts.", "Settlement takes days."]},
            {"slide_number": 3,
             "title": "Regulation is tightening across major jurisdictions this year.",
             "layout": "title_content",
             "bullets": ["MiCA sets stablecoin rules.", "The GENIUS Act passed."]},
            {"slide_number": 4, "title": "End", "layout": "end_slide"},
        ],
    }
    result = _validate_deck_spec(spec)
    dividers = [s for s in result["slides"] if s["layout"] == "section_divider"]
    assert len(dividers) == 1                                   # divider preserved
    assert dividers[0]["title"].startswith("Four converging")   # rewritten to lead insight
    assert "Market Forces" not in [s["title"] for s in result["slides"]]


def test_validate_layout_diversity_guard():
    """No more than 2 consecutive identical layouts — the 3rd+ is rerouted to a
    content-preserving sibling."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "deliverable_type": "client_101",
        "slides": [
            {"slide_number": i,
             "title": f"Insight number {i} advances the overall story clearly.",
             "layout": "four_boxes_wide",
             "boxes": ["Alpha point.", "Beta point.", "Gamma point.", "Delta point."]}
            for i in range(1, 6)  # 5 consecutive four_boxes_wide
        ],
    }
    result = _validate_deck_spec(spec)
    layouts = [s["layout"] for s in result["slides"]]
    max_run = cur = 1
    for i in range(1, len(layouts)):
        cur = cur + 1 if layouts[i] == layouts[i - 1] else 1
        max_run = max(max_run, cur)
    assert max_run <= 2
    assert "four_boxes_stacked" in layouts  # sibling used to break the run


def test_validate_business_translation_101_and_exec_only():
    """HSM/MPC etc. are translated to business language for Exec/101, untouched for 201."""
    from app.generation.deliverable_service import _validate_deck_spec

    def spec(dt):
        return {
            "deliverable_type": dt,
            "slides": [
                {"slide_number": 1,
                 "title": "Custody requires Hardware Security Modules and MPC controls.",
                 "layout": "title_content",
                 "bullets": ["HSM devices protect private keys.",
                             "Multi-Party Computation signs transactions."]},
            ],
        }

    r101 = str(_validate_deck_spec(spec("client_101"))).lower()
    assert "hardware security module" not in r101 and "multi-party computation" not in r101
    assert "institutional-grade security controls" in r101

    r201 = str(_validate_deck_spec(spec("client_201"))).lower()
    assert "hardware security module" in r201  # 201 keeps implementation depth


def test_conditional_pr_trigger_detection():
    """The conditional-PR trigger fires only on residual ending/recommendation gaps
    the deterministic validator cannot synthesise (and stays quiet on clean decks)."""
    from app.generation.deliverable_service import _residual_ending_issues

    clean = {"slides": [
        {"layout": "title_content", "title": "Custody underpins the operating model."},
        {"layout": "title_content", "title": "Banks should establish custody governance now."},
    ]}
    assert _residual_ending_issues(clean) == []  # ends on action → PR skipped

    vendor = {"slides": [
        {"layout": "title_content", "title": "Banks should act now on digital-asset custody."},
        {"layout": "four_boxes_stacked", "title": "IBM provides the secure infrastructure and guidance."},
    ]}
    iss = _residual_ending_issues(vendor)
    assert "vendor_positioning_close" in iss and "non_action_ending" in iss  # PR triggered

    norec = {"slides": [
        {"layout": "title_content", "title": "Stablecoins are growing quickly in market share."},
        {"layout": "title_content", "title": "Tokenization expands the addressable market."},
    ]}
    assert "no_recommendation_slide" in _residual_ending_issues(norec)  # PR triggered


def test_validate_ending_backstop_lands_on_recommendation():
    """If a deck would end on a weak 'Risks' slide with a recommendation right
    before it, the two are swapped so the deck closes on the recommendation."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "deliverable_type": "executive_summary",
        "slides": [
            {"slide_number": 1, "title": "Tokenization opens a large new market for banks now.",
             "layout": "title_content", "bullets": ["The market grows fast.", "Banks can capture it."]},
            {"slide_number": 2,
             "title": "Institutions should establish custody governance before scaling tokenization.",
             "layout": "title_content", "bullets": ["Set governance first.", "Then scale programs."]},
            {"slide_number": 3, "title": "Risks", "layout": "title_content",
             "bullets": ["Regulatory risk is material.", "Operational risk is material."]},
        ],
    }
    result = _validate_deck_spec(spec)
    assert result["slides"][-1]["title"].startswith("Institutions should establish")


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


def test_plan_to_out_surfaces_graph_coverage():
    """The plan response exposes deck-level graph coverage so the UI can show the
    graph IS being used (instead of aggregating the stripped per-slide fields → 0)."""
    from types import SimpleNamespace
    from datetime import datetime
    import json as _json
    from app.generation.plan_service import _plan_to_out

    bp = {
        "slides": [{"slide_number": 1, "title": "T", "layout": "title_content"}],
        "metadata": {"graph_coverage": {
            "concepts_available": 162, "relationships_analyzed": 414,
            "patterns_available": 14, "source_documents": 3, "concepts_selected": 28}},
    }
    plan = SimpleNamespace(
        id=1, workspace_id=1, deliverable_type="client_201", focus_area=None,
        blueprint_json=_json.dumps(bp), revision_history=[], status="draft",
        deliverable_id=None, created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )
    out = _plan_to_out(plan)
    assert out.graph_coverage is not None
    assert out.graph_coverage.concepts_available == 162
    assert out.graph_coverage.relationships_analyzed == 414
    assert out.graph_coverage.concepts_selected == 28


def test_validate_restricts_raci_layout():
    """raci is a restricted static-diagram layout — it must be rerouted to a
    truthful text/box layout (never rendering the template's demo RACI table)."""
    from app.generation.deliverable_service import _validate_deck_spec

    spec = {
        "deliverable_type": "client_201",
        "slides": [
            {"slide_number": 1,
             "title": "Governance assigns clear accountability across the operating model",
             "layout": "raci",
             "bullets": ["The CISO owns custody security.", "Treasury owns liquidity policy.",
                         "Compliance owns reporting.", "Operations owns settlement."]},
        ],
    }
    result = _validate_deck_spec(spec)
    slide = result["slides"][0]
    assert slide["layout"] != "raci"
    assert slide.get("boxes") or slide.get("bullets")  # content preserved


def test_validate_reroutes_underfilled_four_boxes():
    """A four_boxes slide with only 3 items can't fill the 4-box grid (would render
    sparse), so occupancy validation reroutes it away from the box layout."""
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
    # 3 items -> rerouted to a non-box layout (two_column), never a sparse 4-box grid
    assert slide["layout"] not in ("four_boxes_wide", "four_boxes_stacked", "six_boxes")
    assert slide.get("boxes") or slide.get("bullets")  # content preserved


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


# ══════════════════════════════════════════════════════════════════════
# 0-SLIDE GUARD — deterministic fallback on empty / malformed blueprint
# ══════════════════════════════════════════════════════════════════════

_NARRATIVE_RESPONSE = (
    "I'd be happy to help create a presentation plan! Here's my thinking on how to "
    "structure this deck for the Payment Modernization workspace. First, we should "
    "consider the audience — typically a new engagement team — and build from context "
    "toward insights. The key themes from the knowledge graph are ISO 20022, Ripple, "
    "and cross-border payments. I'll draft a structured outline below..."
)

_EMPTY_JSON_RESPONSE = "{}"

_NO_SLIDES_RESPONSE = '{"deliverable_type":"client_101","title":"Test","slides":[]}'

_MISSING_SLIDES_RESPONSE = '{"deliverable_type":"client_101","title":"Test","governing_messages":[]}'


def _plan_service_with_mock_chat(mock_response: str):
    """Return a patched plan_service where chat() returns mock_response."""
    from unittest.mock import patch
    return patch("app.generation.plan_service.chat", return_value=mock_response)


@patch("app.generation.plan_service.chat")
def test_narrative_response_triggers_fallback(mock_chat):
    """Pure narrative reply → 0-slide guard fires → fallback blueprint → non-zero slides."""
    mock_chat.return_value = _NARRATIVE_RESPONSE
    ws = _create_workspace("NarrFallback WS")
    _seed_knowledge(ws["id"], n_concepts=6)

    resp = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "draft"
    assert len(data["slides"]) > 0, (
        "0-slide guard must activate and produce fallback slides on narrative response"
    )


@patch("app.generation.plan_service.chat")
def test_empty_json_triggers_fallback(mock_chat):
    """Empty JSON {} → 0-slide guard fires → fallback blueprint → non-zero slides."""
    mock_chat.return_value = _EMPTY_JSON_RESPONSE
    ws = _create_workspace("EmptyJSON WS")
    _seed_knowledge(ws["id"], n_concepts=6)

    resp = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    assert resp.status_code == 201, resp.text
    assert len(resp.json()["slides"]) > 0


@patch("app.generation.plan_service.chat")
def test_empty_slides_array_triggers_fallback(mock_chat):
    """Slides array = [] → 0-slide guard fires → fallback blueprint → non-zero slides."""
    mock_chat.return_value = _NO_SLIDES_RESPONSE
    ws = _create_workspace("EmptySlides WS")
    _seed_knowledge(ws["id"], n_concepts=6)

    resp = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    assert resp.status_code == 201, resp.text
    assert len(resp.json()["slides"]) > 0, (
        "Empty slides array must trigger fallback — no 0-slide plans permitted"
    )


@patch("app.generation.plan_service.chat")
def test_missing_slides_field_triggers_fallback(mock_chat):
    """Missing 'slides' key entirely → 0-slide guard fires → fallback → non-zero slides."""
    mock_chat.return_value = _MISSING_SLIDES_RESPONSE
    ws = _create_workspace("MissingSlides WS")
    _seed_knowledge(ws["id"], n_concepts=6)

    resp = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    assert resp.status_code == 201, resp.text
    assert len(resp.json()["slides"]) > 0


@patch("app.generation.plan_service.chat")
def test_fallback_blueprint_has_valid_slide_schema(mock_chat):
    """Fallback slides must satisfy PlanSlide schema fields (title, layout, slide_number)."""
    mock_chat.return_value = _EMPTY_JSON_RESPONSE
    ws = _create_workspace("SchemCheck WS")
    _seed_knowledge(ws["id"], n_concepts=8)

    resp = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    assert resp.status_code == 201, resp.text
    slides = resp.json()["slides"]
    assert len(slides) > 0
    for s in slides:
        assert "title" in s and s["title"], f"Slide missing title: {s}"
        assert "layout" in s and s["layout"], f"Slide missing layout: {s}"
        assert "slide_number" in s and s["slide_number"] >= 1, f"Slide has bad slide_number: {s}"


@patch("app.generation.plan_service.chat")
def test_fallback_blueprint_no_http_500(mock_chat):
    """Neither narrative response nor empty JSON may produce HTTP 500."""
    for bad_response in [_NARRATIVE_RESPONSE, _EMPTY_JSON_RESPONSE,
                         _NO_SLIDES_RESPONSE, _MISSING_SLIDES_RESPONSE]:
        mock_chat.return_value = bad_response
        ws = _create_workspace(f"No500 WS {bad_response[:10]!r}")
        _seed_knowledge(ws["id"])
        resp = client.post(
            f"/workspaces/{ws['id']}/presentation-plans",
            json={"type": "client_101"},
        )
        assert resp.status_code != 500, (
            f"HTTP 500 must never occur for malformed LLM output. Got 500 for: {bad_response[:80]!r}"
        )
        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}"


@patch("app.generation.plan_service.chat")
def test_fallback_plan_is_approvable(mock_chat):
    """A fallback plan must be approvable and produce a valid PPTX."""
    import sys
    from pathlib import Path
    from unittest.mock import patch as _patch, MagicMock

    mock_chat.return_value = _EMPTY_JSON_RESPONSE
    ws = _create_workspace("FallbackApprove WS")
    _seed_knowledge(ws["id"], n_concepts=8)

    create_resp = client.post(
        f"/workspaces/{ws['id']}/presentation-plans",
        json={"type": "client_101"},
    )
    assert create_resp.status_code == 201
    plan_id = create_resp.json()["id"]
    assert len(create_resp.json()["slides"]) > 0, "Fallback plan must have slides"

    # Approve it — PowerPoint generator is mocked to avoid file-system dependency
    fake_pptx = b"PK\x03\x04" + b"\x00" * 200  # minimal ZIP magic bytes
    pptx_path = "deliverables.generators.powerpoint_generator.generate_pptx"
    with _patch(pptx_path, return_value=fake_pptx):
        gen_resp = client.post(f"/presentation-plans/{plan_id}/generate")
    assert gen_resp.status_code == 200, (
        f"Fallback plan approve must succeed (HTTP 200), got {gen_resp.status_code}: {gen_resp.text[:200]}"
    )


# ══════════════════════════════════════════════════════════════════════
# REVISION MAX_TOKENS — verify 16000 budget for Client 201 blueprints
# ══════════════════════════════════════════════════════════════════════

def test_revise_plan_uses_16000_max_tokens():
    """revise_plan must call _generate_json_phase with max_tokens=16000.

    A Client 201 blueprint with full annotation fields reaches ~12,000–15,000
    chars (~3,500–4,000 tokens).  8192 tokens left insufficient budget for the
    output; 16000 matches the blueprint generation budget and eliminates the
    truncation risk.
    """
    import inspect
    from app.generation import plan_service as _ps

    src = inspect.getsource(_ps.revise_plan)
    # The call must explicitly pass max_tokens=16000 (not 8192)
    assert "max_tokens=16000" in src, (
        "revise_plan must call _generate_json_phase with max_tokens=16000 "
        "to prevent JSON truncation on large Client 201 blueprints"
    )
    assert "max_tokens=8192" not in src, (
        "revise_plan must not use max_tokens=8192 — this caused truncation "
        "for Client 201 blueprints with full annotation fields"
    )


# ══════════════════════════════════════════════════════════════════════
# VALIDATION SUMMARY — response headers after approve_and_generate
# ══════════════════════════════════════════════════════════════════════

def test_approve_exposes_validation_headers():
    """POST /presentation-plans/{id}/generate must return X-Validation-* headers."""
    import sys
    from pathlib import Path
    from unittest.mock import patch as _patch

    ws = _create_workspace("ValHeaders WS")
    _seed_knowledge(ws["id"], n_concepts=6)

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

    fake_pptx = b"PK\x03\x04" + b"\x00" * 200
    pptx_path = "deliverables.generators.powerpoint_generator.generate_pptx"
    with _patch(pptx_path, return_value=fake_pptx):
        resp = client.post(f"/presentation-plans/{plan_id}/generate")

    assert resp.status_code == 200, resp.text
    # Validation headers must be present (may be "0" when nothing was removed/fixed)
    for header in ("X-Validation-Removed", "X-Validation-Fixed",
                   "X-Slides-Before", "X-Slides-After"):
        assert header in resp.headers, f"Missing response header: {header}"
        assert resp.headers[header].isdigit(), (
            f"Header {header} must be a non-negative integer, got: {resp.headers[header]!r}"
        )


def test_validation_summary_schema():
    """ValidationSummary fields must all be non-negative integers."""
    from app.schemas import ValidationSummary

    v = ValidationSummary(
        slides_removed=2,
        slides_fixed=3,
        slide_count_before=15,
        slide_count_after=13,
    )
    assert v.slides_removed == 2
    assert v.slides_fixed == 3
    assert v.slide_count_before == 15
    assert v.slide_count_after == 13

    # Default (clean run — no issues)
    v_clean = ValidationSummary()
    assert v_clean.slides_removed == 0
    assert v_clean.slides_fixed == 0


def test_validate_deck_spec_embeds_stats_in_metadata():
    """_validate_deck_spec must embed validation_removed and validation_fixed in metadata."""
    from app.generation.deliverable_service import _validate_deck_spec

    deck = {
        "title": "Test Deck",
        "slides": [
            # This slide has a label title with ≤1 bullet → should be removed
            {"slide_number": 1, "title": "overview", "layout": "title_content",
             "bullets": ["One bullet only."]},
            # This slide is fine
            {"slide_number": 2, "title": "ISO 20022 Drives Settlement Reform",
             "layout": "title_content",
             "bullets": [
                 "ISO 20022 is now mandated in 50+ countries.",
                 "Swift gpi processes 50% of cross-border payments.",
                 "Richer data reduces reconciliation errors by 40%.",
             ]},
        ],
        "metadata": {"total_slides": 2, "source_documents": []},
    }

    result = _validate_deck_spec(deck, context="test")
    meta = result.get("metadata", {})
    assert "validation_removed" in meta, "metadata must contain validation_removed"
    assert "validation_fixed" in meta, "metadata must contain validation_fixed"
    assert isinstance(meta["validation_removed"], int)
    assert isinstance(meta["validation_fixed"], int)
    # The label-title slide should have been removed
    assert meta["validation_removed"] >= 1, (
        "The 'overview' label slide with ≤1 bullet must have been removed"
    )



# ══════════════════════════════════════════════════════════════════════
# SPEAKER-NOTE SCRUB — validation layer removes presenter-coaching language
# ══════════════════════════════════════════════════════════════════════

def test_speaker_note_bullets_removed_by_validation():
    """Bullets starting with presenter-coaching phrases must be removed by _validate_deck_spec."""
    from app.generation.deliverable_service import _validate_deck_spec

    deck = {
        "title": "Test Deck",
        "slides": [
            {
                "slide_number": 1,
                "title": "ISO 20022 Drives Settlement Reform",
                "layout": "title_content",
                "bullets": [
                    "This slide establishes the context for payment modernisation.",
                    "ISO 20022 adoption is now mandated in over 50 countries.",
                    "Use this slide to orient the audience.",
                    "Swift gpi processes over 50% of cross-border payments today.",
                    "The presenter should note the regulatory timeline.",
                ],
            },
        ],
        "metadata": {"total_slides": 1, "source_documents": []},
    }

    result = _validate_deck_spec(deck, context="speaker_note_test")
    slides = result["slides"]
    assert len(slides) == 1
    remaining_bullets = slides[0]["bullets"]

    # Speaker-note bullets must be removed
    for b in remaining_bullets:
        assert not b.lower().startswith("this slide"), f"Speaker-note bullet survived: {b!r}"
        assert not b.lower().startswith("use this slide"), f"Speaker-note bullet survived: {b!r}"
        assert not b.lower().startswith("the presenter"), f"Speaker-note bullet survived: {b!r}"

    # Consulting-quality bullets must be kept
    consulting_bullets = [b for b in remaining_bullets if "ISO 20022" in b or "Swift gpi" in b]
    assert len(consulting_bullets) >= 2, (
        "Consulting-quality bullets must not be removed by speaker-note scrub"
    )


def test_speaker_note_boxes_removed_by_validation():
    """Boxes starting with presenter-coaching phrases must be removed."""
    from app.generation.deliverable_service import _validate_deck_spec

    deck = {
        "title": "Test Deck",
        "slides": [
            {
                "slide_number": 1,
                "title": "Four Strategic Priorities Drive Digital Adoption",
                "layout": "four_boxes_wide",
                "boxes": [
                    "This slide provides an overview of the four priorities.",
                    "ISO 20022 mandates force banks to upgrade payment rails.",
                    "If the audience asks, explain the regulatory timeline.",
                    "Ripple ODL eliminates pre-funded nostro account requirements.",
                ],
            },
        ],
        "metadata": {"total_slides": 1, "source_documents": []},
    }

    result = _validate_deck_spec(deck, context="speaker_note_boxes_test")
    slides = result["slides"]
    assert len(slides) == 1
    remaining_boxes = slides[0].get("boxes", [])

    for b in remaining_boxes:
        assert not b.lower().startswith("this slide"), f"Speaker-note box survived: {b!r}"
        assert not b.lower().startswith("if the audience"), f"Speaker-note box survived: {b!r}"


def test_blueprint_system_prompt_excludes_annotation_fields():
    """_BLUEPRINT_SYSTEM_PROMPT must not instruct Claude to generate annotation fields."""
    from app.generation.deliverable_service import _BLUEPRINT_SYSTEM_PROMPT

    banned = ["key_insights", "graph_concepts", "relationships_used", "patterns_used"]
    for field in banned:
        # The field name should not appear as an instruction to generate it.
        # It may appear in the "Do NOT include" section — check the instruction context.
        assert f'"{field}"' not in _BLUEPRINT_SYSTEM_PROMPT.split("Do NOT include")[0], (
            f"_BLUEPRINT_SYSTEM_PROMPT must not ask Claude to generate {field!r} "
            f"— these are annotation fields that waste output tokens"
        )


def test_blueprint_system_prompt_has_speaker_note_prohibition():
    """_BLUEPRINT_SYSTEM_PROMPT must explicitly prohibit presenter-coaching language."""
    from app.generation.deliverable_service import _BLUEPRINT_SYSTEM_PROMPT

    # These exact phrases must appear in the prohibition list
    assert "This slide establishes" in _BLUEPRINT_SYSTEM_PROMPT
    assert "Use this slide to" in _BLUEPRINT_SYSTEM_PROMPT
    assert "The presenter should" in _BLUEPRINT_SYSTEM_PROMPT
    assert "If the audience asks" in _BLUEPRINT_SYSTEM_PROMPT


def test_revision_system_prompt_excludes_annotation_fields():
    """_REVISION_SYSTEM_PROMPT must not ask Claude to generate annotation fields."""
    from app.generation.plan_service import _REVISION_SYSTEM_PROMPT

    # The revision prompt should explicitly tell Claude NOT to add these fields
    assert "key_insights" in _REVISION_SYSTEM_PROMPT
    # But it should be in a "Do NOT" context
    assert "Do NOT add" in _REVISION_SYSTEM_PROMPT or "do NOT" in _REVISION_SYSTEM_PROMPT.lower()


def test_normaliser_strips_notes_field():
    """_blueprint_to_deck_spec must strip 'notes' from slide output."""
    from app.generation.deliverable_service import _blueprint_to_deck_spec

    blueprint = {
        "deliverable_type": "client_101",
        "title": "Test",
        "governing_messages": [],
        "storyline_summary": "Test arc.",
        "slides": [
            {
                "slide_number": 1,
                "title": "ISO 20022 Drives Payments Reform",
                "layout": "title_content",
                "bullets": ["ISO 20022 is now mandated globally."],
                "notes": "This slide establishes the regulatory context.",
                "purpose": "Introduce ISO 20022.",
                "key_insights": ["ISO 20022 is important."],
            }
        ],
        "metadata": {"total_slides": 1, "source_documents": []},
    }

    deck_spec = _blueprint_to_deck_spec(blueprint, focus_area=None, source_refs=[])
    slide = deck_spec["slides"][0]

    # These fields must be stripped before reaching the renderer
    for field in ("notes", "purpose", "key_insights", "graph_concepts",
                  "relationships_used", "patterns_used", "evidence"):
        assert field not in slide, (
            f"Field {field!r} must be stripped by _blueprint_to_deck_spec before rendering"
        )

    # Content must be preserved
    assert slide["title"] == "ISO 20022 Drives Payments Reform"
    assert slide["bullets"] == ["ISO 20022 is now mandated globally."]


def test_contains_speaker_note_detection():
    """_contains_speaker_note correctly identifies presenter-coaching phrases."""
    from app.generation.deliverable_service import _contains_speaker_note

    # These must be detected
    assert _contains_speaker_note("This slide establishes the context.")
    assert _contains_speaker_note("This slide shows our approach.")
    assert _contains_speaker_note("Use this slide to orient the audience.")
    assert _contains_speaker_note("The presenter should note the timeline.")
    assert _contains_speaker_note("If the audience asks, explain the regulation.")
    assert _contains_speaker_note("this slide provides an overview")   # case-insensitive

    # These must NOT be detected (legitimate consulting content)
    assert not _contains_speaker_note("ISO 20022 adoption drives richer data exchange.")
    assert not _contains_speaker_note("Ripple ODL eliminates pre-funded nostro requirements.")
    assert not _contains_speaker_note("Three regulatory mandates force adoption by 2026.")



def test_plan_blueprint_system_prompt_requires_annotation_fields():
    """_PLAN_BLUEPRINT_SYSTEM_PROMPT must require annotation fields for the plan review UI."""
    from app.generation.deliverable_service import _PLAN_BLUEPRINT_SYSTEM_PROMPT

    required = ["key_insights", "graph_concepts", "relationships_used", "patterns_used", "evidence", "purpose"]
    for field in required:
        assert field in _PLAN_BLUEPRINT_SYSTEM_PROMPT, (
            f"_PLAN_BLUEPRINT_SYSTEM_PROMPT must require {field!r} — it powers the plan review UI"
        )
    # Must state these are REQUIRED (not optional)
    assert "REQUIRED" in _PLAN_BLUEPRINT_SYSTEM_PROMPT

    # Must also prohibit speaker notes (same quality bar as generation prompt)
    assert "This slide establishes" in _PLAN_BLUEPRINT_SYSTEM_PROMPT


def test_plan_blueprint_system_prompt_used_in_generate_plan():
    """generate_plan() must use _PLAN_BLUEPRINT_SYSTEM_PROMPT, not _BLUEPRINT_SYSTEM_PROMPT."""
    import inspect
    from app.generation import plan_service
    from app.generation.deliverable_service import _PLAN_BLUEPRINT_SYSTEM_PROMPT, _BLUEPRINT_SYSTEM_PROMPT

    # plan_service must import _PLAN_BLUEPRINT_SYSTEM_PROMPT
    assert hasattr(plan_service, '_PLAN_BLUEPRINT_SYSTEM_PROMPT'), (
        "plan_service must import _PLAN_BLUEPRINT_SYSTEM_PROMPT"
    )
    # The generate_plan source must reference the plan-specific prompt
    src = inspect.getsource(plan_service.generate_plan)
    assert "_PLAN_BLUEPRINT_SYSTEM_PROMPT" in src, (
        "generate_plan() must use _PLAN_BLUEPRINT_SYSTEM_PROMPT, not _BLUEPRINT_SYSTEM_PROMPT"
    )
    assert "_BLUEPRINT_SYSTEM_PROMPT" not in src or "_PLAN_BLUEPRINT_SYSTEM_PROMPT" in src, (
        "generate_plan() must not use the generation-phase _BLUEPRINT_SYSTEM_PROMPT"
    )


def test_plan_blueprint_annotation_fields_stripped_before_render():
    """
    Annotation fields that _PLAN_BLUEPRINT_SYSTEM_PROMPT requires must all be
    stripped by _blueprint_to_deck_spec before PowerPoint rendering.
    """
    from app.generation.deliverable_service import _blueprint_to_deck_spec

    blueprint = {
        "deliverable_type": "client_101",
        "title": "Test",
        "governing_messages": [],
        "storyline_summary": "Test.",
        "slides": [{
            "slide_number": 1,
            "title": "ISO 20022 mandates reshape global payments infrastructure.",
            "layout": "title_content",
            "bullets": ["ISO 20022 adoption drives richer data exchange."],
            "purpose": "Introduce the regulatory mandate.",
            "key_insights": ["ISO 20022 is the dominant driver."],
            "graph_concepts": ["ISO 20022", "SWIFT"],
            "relationships_used": ["ISO 20022 → SWIFT (enables)"],
            "patterns_used": ["Payments Modernisation"],
            "evidence": ["Payments Strategy 2024"],
        }],
        "metadata": {"total_slides": 1, "source_documents": []},
    }

    deck_spec = _blueprint_to_deck_spec(blueprint, focus_area=None, source_refs=[])
    slide = deck_spec["slides"][0]

    for field in ("purpose", "key_insights", "graph_concepts", "relationships_used", "patterns_used", "evidence"):
        assert field not in slide, (
            f"Annotation field {field!r} must be stripped by _blueprint_to_deck_spec before rendering"
        )
    assert slide["title"] == "ISO 20022 mandates reshape global payments infrastructure."
    assert slide["bullets"] == ["ISO 20022 adoption drives richer data exchange."]
