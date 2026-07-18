"""Graph API router — full graph, node neighbourhood, concepts/relationships/patterns."""
import logging

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

logger = logging.getLogger(__name__)

router = APIRouter(tags=["knowledge"])


@router.get("/workspaces/{workspace_id}/concepts", response_model=list[ConceptOut])
def list_concepts(workspace_id: int, db: Session = Depends(get_db)):
    _require_workspace(workspace_id, db)
    try:
        concepts = db.query(Concept).filter(Concept.workspace_id == workspace_id).all()
        return [ConceptOut.model_validate(c) for c in concepts]
    except Exception as exc:
        logger.error("[graph ws=%d] list_concepts failed: %s: %s", workspace_id, type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail="Failed to retrieve concepts.")


@router.get("/workspaces/{workspace_id}/relationships", response_model=list[RelationshipOut])
def list_relationships(workspace_id: int, db: Session = Depends(get_db)):
    _require_workspace(workspace_id, db)
    try:
        rels = db.query(Relationship).filter(Relationship.workspace_id == workspace_id).all()
        return [RelationshipOut.model_validate(r) for r in rels]
    except Exception as exc:
        logger.error("[graph ws=%d] list_relationships failed: %s: %s", workspace_id, type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail="Failed to retrieve relationships.")


@router.get("/workspaces/{workspace_id}/patterns", response_model=list[ConsultingPatternOut])
def list_patterns(workspace_id: int, db: Session = Depends(get_db)):
    _require_workspace(workspace_id, db)
    try:
        patterns = db.query(ConsultingPattern).filter(ConsultingPattern.workspace_id == workspace_id).all()
        return [ConsultingPatternOut.model_validate(p) for p in patterns]
    except Exception as exc:
        logger.error("[graph ws=%d] list_patterns failed: %s: %s", workspace_id, type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail="Failed to retrieve patterns.")


@router.get("/workspaces/{workspace_id}/graph", response_model=GraphOut)
def get_graph(workspace_id: int, db: Session = Depends(get_db)):
    _require_workspace(workspace_id, db)
    try:
        G = graph_memory_manager.get_workspace_graph(workspace_id, db)
        data = graph_to_react_flow(G, workspace_id=workspace_id)
        return GraphOut(**data)
    except Exception as exc:
        logger.error("[graph ws=%d] get_graph failed: %s: %s", workspace_id, type(exc).__name__, exc)
        # Return empty graph rather than 500 — the UI handles the empty state gracefully
        return GraphOut(nodes=[], edges=[])


@router.get("/workspaces/{workspace_id}/graph/node/{node_id}", response_model=NodeNeighbourhood)
def get_node_neighbourhood(
    workspace_id: int,
    node_id: int,
    hops: int = 2,
    db: Session = Depends(get_db),
):
    _require_workspace(workspace_id, db)

    # Validate that the node belongs to this workspace before any graph work
    concept = db.get(Concept, node_id)
    if not concept or concept.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Node not found in this workspace.")

    try:
        G = graph_memory_manager.get_workspace_graph(workspace_id, db)

        # Guard: hops must be a reasonable value to prevent runaway BFS
        safe_hops = max(1, min(hops, 4))

        nbr_node_ids, nbr_edges = get_neighbourhood(G, node_id, hops=safe_hops)

        neighbours = [
            ConceptOut.model_validate(c)
            for c in db.query(Concept).filter(
                Concept.id.in_(nbr_node_ids),
                Concept.id != node_id,
            ).all()
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

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "[graph ws=%d node=%d] get_node_neighbourhood failed: %s: %s",
            workspace_id, node_id, type(exc).__name__, exc,
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to load node neighbourhood. Please try again.",
        )


def _require_workspace(workspace_id: int, db: Session):
    ws = db.get(Workspace, workspace_id)
    if not ws:
        raise HTTPException(status_code=404, detail="Workspace not found.")
