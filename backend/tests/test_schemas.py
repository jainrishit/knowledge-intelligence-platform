"""
Schema validation tests — every Pydantic model, SourcedResponseMixin edge cases,
grounding rule enforcement, and field constraints.
"""
import pytest
from datetime import datetime
from pydantic import ValidationError

from app.schemas import (
    AskResponse, SourceRef, DeliverableCreate, DeliverableResponse,
    DeliverableOut, ConceptOut, RelationshipOut, WorkspaceCreate,
    WorkspaceOut, DocumentOut, AskRequest, ChatMessageOut,
    ConsultingPatternOut, GraphNode, GraphEdge, GraphOut,
    NodeNeighbourhood,
)


# ══════════════════════════════════════════════════════════════════════
# SourcedResponseMixin — grounding rule
# ══════════════════════════════════════════════════════════════════════

def test_ask_requires_sources_for_real_answer():
    with pytest.raises(ValidationError):
        AskResponse(answer="ISO 20022 is a standard.", sources=[])


def test_ask_allows_no_sources_for_not_found_variant_1():
    r = AskResponse(answer="I could not find this in the uploaded knowledge.", sources=[])
    assert r.sources == []


def test_ask_allows_no_sources_for_not_found_variant_2():
    r = AskResponse(answer="cannot find any information about this topic.", sources=[])
    assert r.sources == []


def test_ask_allows_no_sources_for_not_found_variant_3():
    r = AskResponse(answer="The question may fall outside the documents in this workspace.", sources=[])
    assert r.sources == []


def test_ask_allows_no_sources_for_not_found_variant_4():
    r = AskResponse(answer="I could not find this in the uploaded knowledge — outside the documents.", sources=[])
    assert r.sources == []


def test_ask_valid_with_sources():
    src = SourceRef(document_id=1, document_name="ISO POV.pdf", excerpt="ISO 20022 is...")
    r = AskResponse(answer="ISO 20022 enables richer data.", sources=[src])
    assert len(r.sources) == 1
    assert r.sources[0].document_id == 1


def test_ask_multiple_sources():
    sources = [
        SourceRef(document_id=i, document_name=f"doc{i}.pdf", excerpt=f"excerpt {i}")
        for i in range(1, 4)
    ]
    r = AskResponse(answer="Answer with three sources.", sources=sources)
    assert len(r.sources) == 3


# ══════════════════════════════════════════════════════════════════════
# SourceRef
# ══════════════════════════════════════════════════════════════════════

def test_source_ref_excerpt_optional():
    s = SourceRef(document_id=1, document_name="file.pdf")
    assert s.excerpt == ""


def test_source_ref_requires_name():
    with pytest.raises(ValidationError):
        SourceRef(document_id=1)


# ══════════════════════════════════════════════════════════════════════
# WorkspaceCreate
# ══════════════════════════════════════════════════════════════════════

def test_workspace_create_valid():
    w = WorkspaceCreate(name="Payments")
    assert w.name == "Payments"
    assert w.description is None


def test_workspace_create_name_too_short():
    with pytest.raises(ValidationError):
        WorkspaceCreate(name="")


def test_workspace_create_name_too_long():
    with pytest.raises(ValidationError):
        WorkspaceCreate(name="x" * 201)


def test_workspace_create_with_description():
    w = WorkspaceCreate(name="Digital", description="DeFi workspace")
    assert w.description == "DeFi workspace"


# ══════════════════════════════════════════════════════════════════════
# WorkspaceOut
# ══════════════════════════════════════════════════════════════════════

def test_workspace_out_defaults_counts():
    w = WorkspaceOut(id=1, name="WS", description=None, created_at=datetime.utcnow())
    assert w.document_count == 0
    assert w.concept_count == 0


# ══════════════════════════════════════════════════════════════════════
# AskRequest
# ══════════════════════════════════════════════════════════════════════

def test_ask_request_valid():
    r = AskRequest(question="What is ISO 20022?")
    assert r.question == "What is ISO 20022?"


def test_ask_request_empty_rejected():
    with pytest.raises(ValidationError):
        AskRequest(question="")


# ══════════════════════════════════════════════════════════════════════
# DeliverableCreate
# ══════════════════════════════════════════════════════════════════════

def test_deliverable_create_all_valid_types():
    for t in ["POV", "executive_summary", "roadmap"]:
        d = DeliverableCreate(type=t)
        assert d.type == t


def test_deliverable_create_rejects_invalid_type():
    with pytest.raises(ValidationError):
        DeliverableCreate(type="press_release")


def test_deliverable_create_optional_fields():
    d = DeliverableCreate(type="POV")
    assert d.topic is None
    assert d.audience is None


# ══════════════════════════════════════════════════════════════════════
# ConceptOut
# ══════════════════════════════════════════════════════════════════════

def test_concept_out_full():
    c = ConceptOut(
        id=1, workspace_id=2, name="CBDC", type="Technology",
        description="Central bank digital currency",
        source_document_id=3, source_excerpt="CBDCs are digital forms...",
        created_at=datetime.utcnow(),
    )
    assert c.name == "CBDC"
    assert c.source_document_id == 3


def test_concept_out_nullable_fields():
    c = ConceptOut(
        id=1, workspace_id=1, name="Generic",
        type=None, description=None,
        source_document_id=None, source_excerpt=None,
        created_at=datetime.utcnow(),
    )
    assert c.type is None
    assert c.source_document_id is None


# ══════════════════════════════════════════════════════════════════════
# RelationshipOut
# ══════════════════════════════════════════════════════════════════════

def test_relationship_out_valid():
    r = RelationshipOut(
        id=1, workspace_id=1,
        source_concept_id=10, target_concept_id=11,
        relationship_type="implements",
        source_document_id=5,
        created_at=datetime.utcnow(),
    )
    assert r.relationship_type == "implements"


def test_relationship_out_nullable_doc():
    r = RelationshipOut(
        id=1, workspace_id=1,
        source_concept_id=1, target_concept_id=2,
        relationship_type="related_to",
        source_document_id=None,
        created_at=datetime.utcnow(),
    )
    assert r.source_document_id is None


# ══════════════════════════════════════════════════════════════════════
# ConsultingPatternOut
# ══════════════════════════════════════════════════════════════════════

def test_pattern_out_valid():
    p = ConsultingPatternOut(
        id=1, workspace_id=1, name="Migration Pattern",
        problem_statement="Legacy systems.",
        ibm_approach=["Step 1", "Step 2"],
        related_concept_ids=[1, 2],
        source_document_ids=[3],
        created_at=datetime.utcnow(),
    )
    assert p.name == "Migration Pattern"
    assert len(p.ibm_approach) == 2


def test_pattern_out_defaults_empty_lists():
    p = ConsultingPatternOut(
        id=1, workspace_id=1, name="Pattern",
        problem_statement=None,
        created_at=datetime.utcnow(),
    )
    assert p.ibm_approach == []
    assert p.related_concept_ids == []
    assert p.source_document_ids == []


# ══════════════════════════════════════════════════════════════════════
# Graph schemas
# ══════════════════════════════════════════════════════════════════════

def test_graph_node_schema():
    n = GraphNode(
        id="42",
        data={"label": "ISO 20022", "type": "Standard"},
        position={"x": 100.0, "y": 200.0},
    )
    assert n.id == "42"
    assert n.type == "default"


def test_graph_edge_schema():
    e = GraphEdge(
        id="e1-2", source="1", target="2", label="related_to",
        data={"relationship_type": "related_to", "source_document_id": 1, "strength": 0.8, "strokeWidth": 3},
    )
    assert e.source == "1"
    assert e.data["strength"] == 0.8


def test_graph_out_empty():
    g = GraphOut(nodes=[], edges=[])
    assert g.nodes == []
    assert g.edges == []


# ══════════════════════════════════════════════════════════════════════
# ChatMessageOut
# ══════════════════════════════════════════════════════════════════════

def test_chat_message_out_user():
    m = ChatMessageOut(
        id=1, workspace_id=1, role="user", content="Hello?",
        source_document_ids=[], created_at=datetime.utcnow(),
    )
    assert m.role == "user"


def test_chat_message_out_assistant():
    m = ChatMessageOut(
        id=2, workspace_id=1, role="assistant",
        content="ISO 20022 is a standard.",
        source_document_ids=[1, 2],
        created_at=datetime.utcnow(),
    )
    assert m.source_document_ids == [1, 2]


# ══════════════════════════════════════════════════════════════════════
# DeliverableOut / DeliverableResponse
# ══════════════════════════════════════════════════════════════════════

def test_deliverable_out_valid():
    d = DeliverableOut(
        id=1, workspace_id=1, type="POV", title="ISO 20022 POV",
        content_markdown="## Summary\n\nContent.",
        source_concept_ids=[1, 2], source_document_ids=[3],
        created_at=datetime.utcnow(),
    )
    assert d.type == "POV"
    assert d.source_concept_ids == [1, 2]


def test_deliverable_response_with_sources():
    d = DeliverableOut(
        id=1, workspace_id=1, type="roadmap", title="Roadmap",
        content_markdown="# Roadmap",
        source_concept_ids=[], source_document_ids=[],
        created_at=datetime.utcnow(),
    )
    src = SourceRef(document_id=1, document_name="doc.pdf", excerpt="excerpt")
    resp = DeliverableResponse(deliverable=d, sources=[src])
    assert len(resp.sources) == 1
