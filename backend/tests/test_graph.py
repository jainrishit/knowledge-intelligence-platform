"""
Graph builder and traversal unit tests — weighted edges, semantic scoring,
strength-filtered BFS, weighted shortest path, large graph stress.
"""
import pytest
import networkx as nx
from unittest.mock import MagicMock


def _mc(id, ws_id, name, type_="General", desc="", doc_id=None, excerpt="", confidence=0.8):
    c = MagicMock()
    c.id = id
    c.workspace_id = ws_id
    c.name = name
    c.type = type_
    c.description = desc
    c.source_document_id = doc_id
    c.source_excerpt = excerpt
    c.confidence = float(confidence)
    return c


def _mr(id, ws_id, src, tgt, rel="related_to", doc_id=None, strength=0.7):
    r = MagicMock()
    r.id = id
    r.workspace_id = ws_id
    r.source_concept_id = src
    r.target_concept_id = tgt
    r.relationship_type = rel
    r.source_document_id = doc_id
    r.strength = float(strength)
    return r


def _mock_db(concepts, relationships):
    db = MagicMock()
    def _query(model):
        from app.db.models import Concept, Relationship
        q = MagicMock()
        if model == Concept:
            q.filter.return_value.all.return_value = concepts
        elif model == Relationship:
            q.filter.return_value.all.return_value = relationships
        return q
    db.query.side_effect = _query
    return db


# ══════════════════════════════════════════════════════════════════════
# GRAPH BUILDER
# ══════════════════════════════════════════════════════════════════════

def test_empty_graph():
    from app.graph.builder import build_workspace_graph
    G = build_workspace_graph(_mock_db([], []), 1)
    assert G.number_of_nodes() == 0
    assert G.number_of_edges() == 0


def test_nodes_added_with_attributes():
    from app.graph.builder import build_workspace_graph
    concepts = [_mc(1, 1, "ISO 20022", "Standard", "A payment standard", confidence=0.95)]
    G = build_workspace_graph(_mock_db(concepts, []), 1)
    assert G.has_node(1)
    assert G.nodes[1]["name"] == "ISO 20022"
    assert G.nodes[1]["type"] == "Standard"
    assert G.nodes[1]["confidence"] == 0.95


def test_edges_added_with_weight():
    """weight must equal 1/strength so stronger edges are shorter."""
    from app.graph.builder import build_workspace_graph
    concepts = [_mc(1, 1, "A"), _mc(2, 1, "B")]
    rels = [_mr(1, 1, 1, 2, "implements", strength=0.8)]
    G = build_workspace_graph(_mock_db(concepts, rels), 1)
    assert G.has_edge(1, 2)
    assert G[1][2]["strength"] == 0.8
    assert abs(G[1][2]["weight"] - 1.0 / 0.8) < 1e-6


def test_orphan_edge_skipped():
    """Edge referencing non-existent node is silently skipped."""
    from app.graph.builder import build_workspace_graph
    concepts = [_mc(1, 1, "A")]
    rels = [_mr(1, 1, 1, 99, "related_to")]  # node 99 not in concepts
    G = build_workspace_graph(_mock_db(concepts, rels), 1)
    assert G.number_of_edges() == 0


def test_near_zero_strength_clamped():
    """Zero strength must not cause division by zero in weight."""
    from app.graph.builder import build_workspace_graph
    concepts = [_mc(1, 1, "A"), _mc(2, 1, "B")]
    rels = [_mr(1, 1, 1, 2, strength=0.0)]
    G = build_workspace_graph(_mock_db(concepts, rels), 1)
    assert G.has_edge(1, 2)
    assert G[1][2]["weight"] < 1000  # clamped to max(0.01)


def test_react_flow_format_structure():
    from app.graph.builder import build_workspace_graph, graph_to_react_flow
    concepts = [_mc(10, 1, "CBDC", "Technology"), _mc(11, 1, "DLT", "Pattern")]
    rels = [_mr(1, 1, 10, 11, "implements", strength=0.9)]
    G = build_workspace_graph(_mock_db(concepts, rels), 1)
    out = graph_to_react_flow(G)

    assert "nodes" in out and "edges" in out
    assert len(out["nodes"]) == 2
    assert len(out["edges"]) == 1

    node_ids = {n["id"] for n in out["nodes"]}
    assert node_ids == {"10", "11"}  # React Flow requires string IDs

    edge = out["edges"][0]
    assert edge["source"] == "10"
    assert edge["target"] == "11"
    assert edge["data"]["strength"] == 0.9
    assert edge["data"]["strokeWidth"] >= 1  # stroke derived from strength


def test_react_flow_empty_graph():
    from app.graph.builder import graph_to_react_flow
    G = nx.DiGraph()
    out = graph_to_react_flow(G)
    assert out == {"nodes": [], "edges": []}


# ══════════════════════════════════════════════════════════════════════
# TRAVERSAL — SEMANTIC NODE SEARCH
# ══════════════════════════════════════════════════════════════════════

def _build_G():
    from app.graph.builder import build_workspace_graph
    concepts = [
        _mc(1, 1, "ISO 20022",           "Standard",  "Payment messaging standard",  confidence=0.9),
        _mc(2, 1, "SWIFT Messaging",      "Network",   "Global financial messaging",  confidence=0.85),
        _mc(3, 1, "CBDC Architecture",    "Technology","Central bank digital currency", confidence=0.8),
        _mc(4, 1, "Tokenized Deposits",   "Concept",   "Deposits as digital tokens",  confidence=0.75),
        _mc(5, 1, "Payment Settlement",   "Process",   "Settlement of payments",      confidence=0.7),
    ]
    rels = [
        _mr(1, 1, 1, 2, "related_to",   strength=0.9),
        _mr(2, 1, 2, 3, "related_to",   strength=0.6),
        _mr(3, 1, 3, 4, "implements",   strength=0.4),  # weak edge
        _mr(4, 1, 1, 5, "enables",      strength=0.85),
    ]
    return build_workspace_graph(_mock_db(concepts, rels), 1)


def test_semantic_exact_match_scores_highest():
    from app.graph.traversal import find_nodes_semantic
    G = _build_G()
    results = find_nodes_semantic(G, ["ISO 20022"])
    # Node 1 is exact match — must be first with score close to 1.0.
    # Score is normalised: (1.0 / n_kw) * confidence_boost (≤ 1.0).
    assert results[0][0] == 1
    assert results[0][1] > 0.95


def test_semantic_substring_match():
    from app.graph.traversal import find_nodes_semantic
    G = _build_G()
    results = find_nodes_semantic(G, ["cbdc"])
    node_ids = [r[0] for r in results]
    assert 3 in node_ids  # "CBDC Architecture" contains "cbdc"


def test_semantic_type_match():
    from app.graph.traversal import find_nodes_semantic
    G = _build_G()
    results = find_nodes_semantic(G, ["Process"])
    node_ids = [r[0] for r in results]
    assert 5 in node_ids  # Payment Settlement has type "Process"


def test_semantic_description_match():
    from app.graph.traversal import find_nodes_semantic
    G = _build_G()
    results = find_nodes_semantic(G, ["digital tokens"])
    node_ids = [r[0] for r in results]
    assert 4 in node_ids  # description contains "digital tokens"


def test_semantic_empty_keywords():
    from app.graph.traversal import find_nodes_semantic
    G = _build_G()
    assert find_nodes_semantic(G, []) == []


def test_semantic_top_k_respected():
    from app.graph.traversal import find_nodes_semantic
    G = _build_G()
    results = find_nodes_semantic(G, ["payment", "messaging", "digital"], top_k=2)
    assert len(results) <= 2


def test_backward_compat_find_nodes_by_keywords():
    from app.graph.traversal import find_nodes_by_keywords
    G = _build_G()
    matched = find_nodes_by_keywords(G, ["iso", "cbdc"])
    assert 1 in matched
    assert 3 in matched
    assert 2 not in matched  # "SWIFT Messaging" does not contain "iso" or "cbdc"


# ══════════════════════════════════════════════════════════════════════
# TRAVERSAL — BFS NEIGHBOURHOOD
# ══════════════════════════════════════════════════════════════════════

def test_neighbourhood_1_hop():
    from app.graph.traversal import get_neighbourhood
    G = _build_G()
    nodes, _ = get_neighbourhood(G, node_id=1, hops=1)
    assert 1 in nodes
    assert 2 in nodes   # 1-hop
    assert 3 not in nodes  # 2-hop


def test_neighbourhood_2_hop():
    from app.graph.traversal import get_neighbourhood
    G = _build_G()
    nodes, _ = get_neighbourhood(G, node_id=1, hops=2)
    assert 1 in nodes
    assert 2 in nodes
    assert 3 in nodes   # 2-hop via node 2


def test_neighbourhood_strength_threshold_filters():
    """With threshold=0.65, edge 2→3 (strength=0.6) must be blocked."""
    from app.graph.traversal import get_neighbourhood
    G = _build_G()
    nodes, _ = get_neighbourhood(G, node_id=2, hops=2, strength_threshold=0.65)
    assert 2 in nodes
    assert 3 not in nodes  # edge 2→3 has strength 0.6 < threshold


def test_neighbourhood_no_threshold_includes_weak_edges():
    from app.graph.traversal import get_neighbourhood
    G = _build_G()
    nodes, _ = get_neighbourhood(G, node_id=2, hops=2, strength_threshold=0.0)
    assert 3 in nodes  # weak edge followed when threshold is 0


def test_neighbourhood_max_nodes_cap():
    """Large graph: max_nodes must cap the expansion."""
    from app.graph.builder import build_workspace_graph
    from app.graph.traversal import get_neighbourhood
    concepts = [_mc(i, 1, f"C{i}") for i in range(1, 51)]
    rels = [_mr(i, 1, i, i+1) for i in range(1, 50)]
    G = build_workspace_graph(_mock_db(concepts, rels), 1)
    nodes, _ = get_neighbourhood(G, node_id=1, hops=50, max_nodes=10)
    assert len(nodes) <= 10


def test_neighbourhood_nonexistent_node():
    from app.graph.traversal import get_neighbourhood
    G = _build_G()
    nodes, edges = get_neighbourhood(G, node_id=999, hops=2)
    assert nodes == []
    assert edges == []


def test_neighbourhood_edges_all_within_subgraph():
    """Every returned edge must have both endpoints in the returned node set."""
    from app.graph.traversal import get_neighbourhood
    G = _build_G()
    nodes, edges = get_neighbourhood(G, node_id=1, hops=3)
    node_set = set(nodes)
    for src, tgt, _ in edges:
        assert src in node_set
        assert tgt in node_set


# ══════════════════════════════════════════════════════════════════════
# TRAVERSAL — WEIGHTED SHORTEST PATH
# ══════════════════════════════════════════════════════════════════════

def test_shortest_weighted_path_exists():
    from app.graph.traversal import shortest_weighted_path
    G = _build_G()
    path = shortest_weighted_path(G, source_id=1, target_id=3)
    assert path[0] == 1
    assert path[-1] == 3
    assert len(path) >= 2


def test_shortest_weighted_path_no_path():
    from app.graph.builder import build_workspace_graph
    from app.graph.traversal import shortest_weighted_path
    # Disconnected graph
    concepts = [_mc(1, 1, "A"), _mc(2, 1, "B")]
    G = build_workspace_graph(_mock_db(concepts, []), 1)
    path = shortest_weighted_path(G, 1, 2)
    assert path == []


def test_shortest_path_prefers_strong_edges():
    """
    Two paths A→C:
      A→B (strength 0.9) → B→C (strength 0.9)   total weight = 2*(1/0.9) ≈ 2.22
      A→D (strength 0.1) → D→C (strength 0.1)   total weight = 2*(1/0.1) = 20
    Shortest weighted path must choose the strong path.
    """
    from app.graph.builder import build_workspace_graph
    from app.graph.traversal import shortest_weighted_path
    concepts = [_mc(i, 1, c) for i, c in [(1,"A"),(2,"B"),(3,"C"),(4,"D")]]
    rels = [
        _mr(1, 1, 1, 2, strength=0.9),  # A→B strong
        _mr(2, 1, 2, 3, strength=0.9),  # B→C strong
        _mr(3, 1, 1, 4, strength=0.1),  # A→D weak
        _mr(4, 1, 4, 3, strength=0.1),  # D→C weak
    ]
    G = build_workspace_graph(_mock_db(concepts, rels), 1)
    path = shortest_weighted_path(G, 1, 3)
    assert 2 in path  # must go through B (strong path)
    assert 4 not in path  # must avoid D (weak path)


# ══════════════════════════════════════════════════════════════════════
# STRESS — LARGE GRAPH
# ══════════════════════════════════════════════════════════════════════

def test_large_graph_builds_correctly():
    """Build a 200-node, 400-edge graph and verify node/edge counts."""
    from app.graph.builder import build_workspace_graph
    N = 200
    concepts = [_mc(i, 1, f"Concept{i}") for i in range(1, N+1)]
    rels = [_mr(i, 1, i, (i % N) + 1) for i in range(1, N+1)]  # circular
    G = build_workspace_graph(_mock_db(concepts, rels), 1)
    assert G.number_of_nodes() == N
    assert G.number_of_edges() == N


def test_large_graph_react_flow_export():
    """200-node graph must export to React Flow format without error."""
    from app.graph.builder import build_workspace_graph, graph_to_react_flow
    N = 200
    concepts = [_mc(i, 1, f"Concept{i}") for i in range(1, N+1)]
    rels = [_mr(i, 1, i, (i % N) + 1) for i in range(1, N+1)]
    G = build_workspace_graph(_mock_db(concepts, rels), 1)
    out = graph_to_react_flow(G)
    assert len(out["nodes"]) == N
    assert len(out["edges"]) == N
    assert all(isinstance(n["id"], str) for n in out["nodes"])


def test_semantic_search_on_large_graph():
    """Semantic search across 200 nodes returns top_k results, not all 200."""
    from app.graph.builder import build_workspace_graph
    from app.graph.traversal import find_nodes_semantic
    N = 200
    concepts = [_mc(i, 1, f"PaymentConcept{i}" if i % 2 == 0 else f"TechConcept{i}") for i in range(1, N+1)]
    G = build_workspace_graph(_mock_db(concepts, []), 1)
    results = find_nodes_semantic(G, ["payment"], top_k=10)
    assert len(results) <= 10
    # All returned nodes should match "payment"
    for node_id, score in results:
        assert score > 0
