"""Graph API router — full graph, node neighbourhood, concepts/relationships/patterns."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models import Concept, Relationship, ConsultingPattern, Document, Workspace
from app.graph.builder import graph_to_react_flow
from app.graph.memory_manager import graph_memory_manager
from app.graph.traversal import get_neighbourhood
from app.schemas import (
    ConceptOut, RelationshipOut, ConsultingPatternOut,
    GraphOut, NodeNeighbourhood, DocumentOut
)

router = APIRouter(tags=["knowledge"])


@router.get("/workspaces/{workspace_id}/concepts", response_model=list[ConceptOut])
def list_concepts(workspace_id: int, db: Session = Depends(get_db)):
    _require_workspace(workspace_id, db)
    concepts = db.query(Concept).filter(Concept.workspace_id == workspace_id).all()
    return [ConceptOut.model_validate(c) for c in concepts]


@router.get("/workspaces/{workspace_id}/relationships", response_model=list[RelationshipOut])
def list_relationships(workspace_id: int, db: Session = Depends(get_db)):
    _require_workspace(workspace_id, db)
    rels = db.query(Relationship).filter(Relationship.workspace_id == workspace_id).all()
    return [RelationshipOut.model_validate(r) for r in rels]


@router.get("/workspaces/{workspace_id}/patterns", response_model=list[ConsultingPatternOut])
def list_patterns(workspace_id: int, db: Session = Depends(get_db)):
    _require_workspace(workspace_id, db)
    patterns = db.query(ConsultingPattern).filter(ConsultingPattern.workspace_id == workspace_id).all()
    return [ConsultingPatternOut.model_validate(p) for p in patterns]


@router.get("/workspaces/{workspace_id}/graph", response_model=GraphOut)
def get_graph(workspace_id: int, db: Session = Depends(get_db)):
    _require_workspace(workspace_id, db)
    G = graph_memory_manager.get_workspace_graph(workspace_id, db)
    data = graph_to_react_flow(G)
    return GraphOut(**data)


@router.get("/workspaces/{workspace_id}/graph/node/{node_id}", response_model=NodeNeighbourhood)
def get_node_neighbourhood(workspace_id: int, node_id: int, hops: int = 2, db: Session = Depends(get_db)):
    _require_workspace(workspace_id, db)
    concept = db.get(Concept, node_id)
    if not concept or concept.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Node not found in this workspace.")

    G = graph_memory_manager.get_workspace_graph(workspace_id, db)
    nbr_node_ids, nbr_edges = get_neighbourhood(G, node_id, hops=hops)

    neighbours = [
        ConceptOut.model_validate(c)
        for c in db.query(Concept).filter(Concept.id.in_(nbr_node_ids), Concept.id != node_id).all()
    ]

    edges = [
        RelationshipOut.model_validate(r)
        for r in db.query(Relationship).filter(
            Relationship.workspace_id == workspace_id,
            Relationship.source_concept_id.in_(nbr_node_ids),
            Relationship.target_concept_id.in_(nbr_node_ids),
        ).all()
    ]

    source_doc = None
    if concept.source_document_id:
        doc = db.get(Document, concept.source_document_id)
        if doc:
            source_doc = DocumentOut.model_validate(doc)

    return NodeNeighbourhood(
        node=ConceptOut.model_validate(concept),
        neighbours=neighbours,
        edges=edges,
        source_document=source_doc,
    )


def _require_workspace(workspace_id: int, db: Session):
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
