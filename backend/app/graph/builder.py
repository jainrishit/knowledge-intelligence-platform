"""
NetworkX graph builder — weighted edges based on relationship strength.

Edges carry a `weight` = 1/strength so that NetworkX shortest-path
algorithms naturally prefer high-confidence relationship chains.
The SQL tables are canonical; the graph is a derived view rebuilt on demand.

Performance notes
-----------------
spring_layout is O(N²) and dominates response time for large graphs.
graph_to_react_flow() caches layout positions keyed by (workspace_id, graph_hash)
so that repeated requests for the same unchanged graph do not recompute layout.

Node cap: the browser React Flow canvas renders poorly above ~300 nodes.
For workspaces with >300 concepts we return the top-300 by degree centrality
(most-connected = most important) and include all edges between them.
"""
from __future__ import annotations
import hashlib
import json
import logging
import threading
from typing import Optional

import networkx as nx
from sqlalchemy.orm import Session

from app.db.models import Concept, Relationship

logger = logging.getLogger(__name__)

# Maximum nodes to return to the browser. Above this we rank by degree and
# keep only the top-N most connected nodes to stay within React Flow limits.
_REACT_FLOW_NODE_CAP = 300

# Thread-safe layout position cache: {cache_key: {node_id: (x, y)}}
_layout_cache: dict[str, dict[int, tuple[float, float]]] = {}
_layout_cache_lock = threading.Lock()
_MAX_LAYOUT_CACHE_ENTRIES = 20  # evict oldest when exceeded


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
            reasoning=getattr(r, "reasoning", None),
            weight=round(1.0 / max(strength, 0.01), 4),
        )

    return G


def _graph_hash(G: nx.DiGraph) -> str:
    """Cheap structural fingerprint of the graph for cache keying."""
    sig = f"n={G.number_of_nodes()}|e={G.number_of_edges()}"
    node_ids = sorted(G.nodes())[:50]
    sig += "|" + ",".join(str(n) for n in node_ids)
    return hashlib.md5(sig.encode()).hexdigest()[:16]


def _get_cached_layout(cache_key: str) -> Optional[dict[int, tuple[float, float]]]:
    with _layout_cache_lock:
        return _layout_cache.get(cache_key)


def _store_layout(cache_key: str, positions: dict[int, tuple[float, float]]) -> None:
    with _layout_cache_lock:
        if len(_layout_cache) >= _MAX_LAYOUT_CACHE_ENTRIES:
            # Evict the oldest entry
            oldest = next(iter(_layout_cache))
            del _layout_cache[oldest]
        _layout_cache[cache_key] = positions


def graph_to_react_flow(G: nx.DiGraph, workspace_id: int = 0) -> dict:
    """
    Convert NetworkX graph to React Flow–compatible node/edge format.

    Edge thickness hint (strokeWidth) is derived from strength so the UI
    can render stronger relationships as visually heavier lines.

    For graphs with >_REACT_FLOW_NODE_CAP nodes, returns only the top-N
    by degree centrality (most-connected nodes are most important).

    Layout positions are cached by graph fingerprint to avoid recomputing
    spring_layout on every request for an unchanged graph.
    """
    if len(G.nodes) == 0:
        return {"nodes": [], "edges": []}

    # ── Node cap: keep top-N by total degree for large workspaces ─────────────
    if len(G.nodes) > _REACT_FLOW_NODE_CAP:
        degree = {n: G.in_degree(n) + G.out_degree(n) for n in G.nodes()}
        top_nodes = set(
            sorted(degree, key=lambda n: degree[n], reverse=True)[:_REACT_FLOW_NODE_CAP]
        )
        sub = G.subgraph(top_nodes)
        logger.info(
            "[graph builder ws=%d] capped %d → %d nodes for React Flow",
            workspace_id, len(G.nodes), len(top_nodes),
        )
    else:
        sub = G

    # ── Cached spring_layout ──────────────────────────────────────────────────
    cache_key = f"{workspace_id}:{_graph_hash(sub)}"
    cached_pos = _get_cached_layout(cache_key)
    if cached_pos is not None:
        pos = cached_pos
        logger.debug("[graph builder ws=%d] layout cache hit (%s)", workspace_id, cache_key)
    else:
        pos = nx.spring_layout(sub, seed=42, k=2.5)
        pos_serialisable = {node_id: (float(xy[0]), float(xy[1])) for node_id, xy in pos.items()}
        _store_layout(cache_key, pos_serialisable)
        pos = pos_serialisable
        logger.debug("[graph builder ws=%d] layout computed and cached (%s)", workspace_id, cache_key)

    nodes = []
    for node_id, data in sub.nodes(data=True):
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
    for src, tgt, data in sub.edges(data=True):
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
                "reasoning": data.get("reasoning"),
                "strokeWidth": max(1, round(strength * 4)),
            },
        })

    return {"nodes": nodes, "edges": edges}
