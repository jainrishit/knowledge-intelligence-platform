"""
NetworkX graph builder — weighted edges based on relationship strength.

Edges carry a `weight` = 1/strength so that NetworkX shortest-path
algorithms naturally prefer high-confidence relationship chains.
The SQL tables are canonical; the graph is a derived view rebuilt on demand.
"""
from __future__ import annotations
import networkx as nx
from sqlalchemy.orm import Session

from app.db.models import Concept, Relationship


def build_workspace_graph(db: Session, workspace_id: int) -> nx.DiGraph:
    """
    Build a directed, weighted NetworkX graph for the given workspace.

    Node attributes : name, type, description, source_document_id,
                      source_excerpt, confidence
    Edge attributes : relationship_type, source_document_id,
                      strength, weight (= 1/strength for path algorithms)
    """
    G = nx.DiGraph()

    concepts = db.query(Concept).filter(Concept.workspace_id == workspace_id).all()
    for c in concepts:
        G.add_node(
            c.id,
            name=c.name,
            type=c.type or "General",
            description=c.description or "",
            source_document_id=c.source_document_id,
            source_excerpt=c.source_excerpt or "",
            confidence=getattr(c, "confidence", 0.8),
        )

    relationships = db.query(Relationship).filter(Relationship.workspace_id == workspace_id).all()
    for r in relationships:
        if not (G.has_node(r.source_concept_id) and G.has_node(r.target_concept_id)):
            continue
        strength = getattr(r, "strength", 0.7) or 0.7
        G.add_edge(
            r.source_concept_id,
            r.target_concept_id,
            relationship_id=r.id,
            relationship_type=r.relationship_type,
            source_document_id=r.source_document_id,
            strength=strength,
            weight=round(1.0 / max(strength, 0.01), 4),
        )

    return G


def graph_to_react_flow(G: nx.DiGraph) -> dict:
    """
    Convert NetworkX graph to React Flow–compatible node/edge format.
    Edge thickness hint (strokeWidth) is derived from strength so the UI
    can render stronger relationships as visually heavier lines.
    """
    if len(G.nodes) == 0:
        return {"nodes": [], "edges": []}

    pos = nx.spring_layout(G, seed=42, k=2.5)

    nodes = []
    for node_id, data in G.nodes(data=True):
        x, y = pos.get(node_id, (0.0, 0.0))
        nodes.append({
            "id": str(node_id),
            "data": {
                "label": data.get("name", str(node_id)),
                "type": data.get("type", "General"),
                "description": data.get("description", ""),
                "source_document_id": data.get("source_document_id"),
                "source_excerpt": data.get("source_excerpt", ""),
                "confidence": data.get("confidence", 0.8),
            },
            "position": {"x": float(x) * 500, "y": float(y) * 500},
            "type": "default",
        })

    edges = []
    for src, tgt, data in G.edges(data=True):
        strength = data.get("strength", 0.7)
        edges.append({
            "id": f"e{src}-{tgt}",
            "source": str(src),
            "target": str(tgt),
            "label": data.get("relationship_type", ""),
            "data": {
                "relationship_type": data.get("relationship_type", ""),
                "source_document_id": data.get("source_document_id"),
                "strength": strength,
                "strokeWidth": max(1, round(strength * 4)),
            },
        })

    return {"nodes": nodes, "edges": edges}
