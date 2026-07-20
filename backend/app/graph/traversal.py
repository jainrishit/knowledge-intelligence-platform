"""
Graph traversal — semantic node matching and strength-filtered BFS.

find_nodes_semantic()    — multi-signal, cumulative scoring across all
                           keywords: exact name match > substring > type >
                           description. Confidence-weighted so high-confidence
                           nodes rank above equally-matching low-confidence ones.
get_neighbourhood()      — strength-gated BFS; weak edges are skipped
                           below the configured threshold.
shortest_weighted_path() — uses edge weight (1/strength) to surface
                           the most confident concept chain between two nodes.
"""
from __future__ import annotations
import networkx as nx

from app.db.models import Concept


def find_nodes_semantic(
    G: nx.DiGraph,
    keywords: list[str],
    top_k: int = 20,
) -> list[tuple[int, float]]:
    """
    Score every graph node against the keyword list and return the
    top-k (node_id, relevance) pairs sorted highest-first.

    Scoring is cumulative across all keywords rather than winner-takes-all,
    so a node that matches many keywords outranks a node that only matches
    one keyword perfectly. Scores are also weighted by the node's own
    confidence value so high-confidence concepts are preferred when two
    nodes have equal keyword relevance.

    Per-keyword contribution tiers:
      1.0  — exact name match (case-insensitive)
      0.85 — keyword is a substring of the concept name
      0.70 — concept name is a substring of the keyword
      0.55 — keyword matches the concept type
      0.30 — keyword appears in the concept description
      0.0  — no match (excluded from that keyword's contribution)

    Final score = (sum of per-keyword contributions / n_keywords) * confidence_boost
    where confidence_boost = 0.9 + 0.1 * node_confidence  (range 0.9–1.0).
    """
    if not keywords:
        return []

    kw_lower = [k.lower() for k in keywords]
    n_kw = len(kw_lower)
    scored: list[tuple[int, float]] = []

    for node_id, data in G.nodes(data=True):
        name_lower = data.get("name", "").lower()
        type_lower = data.get("type", "").lower()
        desc_lower = data.get("description", "").lower()
        confidence = float(data.get("confidence", 0.8))

        total = 0.0
        for kw in kw_lower:
            if kw == name_lower:
                total += 1.0
            elif kw in name_lower:
                total += 0.85
            elif name_lower in kw:
                total += 0.70
            elif kw in type_lower:
                total += 0.55
            elif kw in desc_lower:
                total += 0.30

        if total == 0.0:
            continue

        # Normalise to [0, 1] then apply a small confidence boost (±10%)
        normalised = total / n_kw
        confidence_boost = 0.9 + 0.1 * min(confidence, 1.0)
        scored.append((node_id, round(normalised * confidence_boost, 6)))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def find_nodes_by_keywords(G: nx.DiGraph, keywords: list[str]) -> list[int]:
    """Return node IDs matching any keyword (no scoring). Wraps find_nodes_semantic."""
    return [nid for nid, _ in find_nodes_semantic(G, keywords, top_k=len(G.nodes) + 1)]


def get_neighbourhood(
    G: nx.DiGraph,
    node_id: int,
    hops: int = 2,
    strength_threshold: float = 0.0,
    max_nodes: int = 60,
) -> tuple[list[int], list[tuple[int, int, dict]]]:
    """
    Return the N-hop neighbourhood of node_id using BFS.

    Parameters
    ----------
    strength_threshold : only follow edges with strength >= this value.
                         Default 0.0 keeps backward compatibility (all edges).
    max_nodes          : cap on neighbourhood size to avoid context explosion.
    """
    if not G.has_node(node_id):
        return [], []

    undirected = G.to_undirected(as_view=True)
    subgraph_nodes: set[int] = {node_id}
    frontier: set[int] = {node_id}

    for _ in range(hops):
        if len(subgraph_nodes) >= max_nodes:
            break
        next_frontier: set[int] = set()
        for n in frontier:
            for neighbour in undirected.neighbors(n):
                if neighbour in subgraph_nodes:
                    continue
                edge_data = (
                    G.get_edge_data(n, neighbour)
                    or G.get_edge_data(neighbour, n)
                    or {}
                )
                edge_strength = edge_data.get("strength", 0.7)
                if edge_strength >= strength_threshold:
                    next_frontier.add(neighbour)
                    subgraph_nodes.add(neighbour)
                    if len(subgraph_nodes) >= max_nodes:
                        break
            if len(subgraph_nodes) >= max_nodes:
                break
        frontier = next_frontier

    subgraph_edges: list[tuple[int, int, dict]] = [
        (src, tgt, data)
        for src, tgt, data in G.edges(data=True)
        if src in subgraph_nodes and tgt in subgraph_nodes
    ]

    return list(subgraph_nodes), subgraph_edges


def shortest_weighted_path(
    G: nx.DiGraph,
    source_id: int,
    target_id: int,
) -> list[int]:
    """
    Return the shortest path from source to target weighted by edge strength
    (weight = 1/strength, so stronger edges are 'shorter').
    Returns an empty list if no path exists.
    """
    try:
        return nx.shortest_path(G, source=source_id, target=target_id, weight="weight")
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []
