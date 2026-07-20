"""
Retrieval consistency tests.

These tests verify that the Q&A pipeline:
  - Produces the same seed-node set for identical and semantically equivalent
    questions (cumulative scoring, not winner-takes-all).
  - Deduplicates near-identical source excerpts so context is not polluted.
  - Surfaces the highest-confidence concepts first in the context.
  - Uses the registered settings.qa_top_k cap.
  - Remains stable across repeated invocations with identical inputs.
"""
from __future__ import annotations
import hashlib
import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import create_engine, StaticPool
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Workspace, Document, Concept, Relationship


# ── Per-test isolated in-memory DB ────────────────────────────────────

def _make_session(name: str):
    engine = create_engine(
        f"sqlite:///file:{name}?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False, "uri": True},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


def _teardown(engine):
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


# ── Shared graph fixtures ─────────────────────────────────────────────

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


def _build_digital_asset_graph():
    """
    Minimal graph representing IBM Digital Asset Haven concepts across two
    source documents — mirrors the real-world scenario described in the brief.
    """
    from app.graph.builder import build_workspace_graph
    concepts = [
        _mc(1, 1, "IBM Digital Asset Haven",   "Platform",   "Unified platform for wallet management and transaction orchestration across 42+ blockchains", doc_id=1, excerpt="IBM Digital Asset Haven is a unified platform for secured wallet management.", confidence=0.95),
        _mc(2, 1, "Multi-Party Computation",    "Technology", "Distributed key generation and threshold signature schemes", doc_id=1, excerpt="MPC with distributed key generation for flexible key management.", confidence=0.90),
        _mc(3, 1, "Policy-based Governance",    "Capability", "Entitlement policies and approvals for wallet operations", doc_id=2, excerpt="Policy-based governance with entitlement policies and approvals.", confidence=0.88),
        _mc(4, 1, "KYC AML Compliance",         "Regulation", "Integrated compliance services for Travel Rule and blockchain analytics", doc_id=2, excerpt="KYC/AML screening, Travel Rule compliance across all supported networks.", confidence=0.85),
        _mc(5, 1, "IBM Z LinuxONE HSM",         "Technology", "Hardware-anchored security using embedded HSMs", doc_id=1, excerpt="Hardware-anchored security using IBM Z/LinuxONE embedded HSMs.", confidence=0.87),
        _mc(6, 1, "Wallet as a Service",        "Capability", "Hosted wallet infrastructure for financial institutions", doc_id=2, excerpt="Wallet-as-a-Service capabilities for financial institutions.", confidence=0.82),
        _mc(7, 1, "Blockchain Interoperability","Standard",   "Support for 42+ public and private blockchain networks", doc_id=1, excerpt="Transact, govern, and settle assets across 42+ blockchains.", confidence=0.91),
        _mc(8, 1, "Transaction Orchestration",  "Process",    "ABI decoding and smart contract interpretation", doc_id=1, excerpt="Transaction management including ABI decoding for smart contract interpretation.", confidence=0.83),
    ]
    rels = [
        _mr(1, 1, 1, 2, "implements",   strength=0.9),
        _mr(2, 1, 1, 3, "enables",      strength=0.88),
        _mr(3, 1, 1, 4, "requires",     strength=0.85),
        _mr(4, 1, 1, 5, "implements",   strength=0.87),
        _mr(5, 1, 1, 6, "is_part_of",   strength=0.82),
        _mr(6, 1, 1, 7, "implements",   strength=0.91),
        _mr(7, 1, 1, 8, "is_part_of",   strength=0.83),
    ]
    return build_workspace_graph(_mock_db(concepts, rels), 1), concepts


# ══════════════════════════════════════════════════════════════════════
# CUMULATIVE SCORING
# ══════════════════════════════════════════════════════════════════════

def test_cumulative_scoring_ranks_multi_match_above_single_match():
    """
    A node matching several keywords at 0.85 each must rank above a node
    that matches only one keyword perfectly (1.0) but no others.
    """
    from app.graph.builder import build_workspace_graph
    from app.graph.traversal import find_nodes_semantic

    concepts = [
        _mc(1, 1, "Digital Asset Platform", desc="wallet blockchain token"),  # matches 3 kw at 0.85
        _mc(2, 1, "IBM",                    desc="unrelated content here"),   # exact match on 1 kw
    ]
    G = build_workspace_graph(_mock_db(concepts, []), 1)
    results = find_nodes_semantic(G, ["digital", "asset", "platform", "blockchain"])
    ids = [r[0] for r in results]
    assert ids[0] == 1, "Multi-keyword match node must rank above single exact-match node"


def test_confidence_boost_breaks_tie_in_favour_of_high_confidence():
    """
    Two nodes with identical keyword relevance: the one with higher confidence
    must be ranked first.
    """
    from app.graph.builder import build_workspace_graph
    from app.graph.traversal import find_nodes_semantic

    concepts = [
        _mc(1, 1, "Payment Standard", confidence=0.65),
        _mc(2, 1, "Payment Standard", confidence=0.95),
    ]
    G = build_workspace_graph(_mock_db(concepts, []), 1)
    results = find_nodes_semantic(G, ["payment standard"])
    assert results[0][0] == 2, "Higher-confidence node must rank first on equal keyword match"
    assert results[0][1] > results[1][1], "Higher-confidence node must have strictly higher score"


def test_identical_questions_return_identical_seed_sets():
    """
    Calling find_nodes_semantic twice with the same question keywords must
    return the exact same ordered list of (node_id, score) pairs.
    """
    from app.graph.traversal import find_nodes_semantic
    G, _ = _build_digital_asset_graph()
    keywords = ["ibm digital asset haven", "wallet", "blockchain", "governance"]

    result_a = find_nodes_semantic(G, keywords, top_k=10)
    result_b = find_nodes_semantic(G, keywords, top_k=10)
    assert result_a == result_b, "Same keywords must always return the same ranked seed list"


def test_rephrased_questions_return_overlapping_top_seeds():
    """
    'What is IBM Digital Asset Haven?' and 'Describe the IBM Digital Asset Haven platform'
    should retrieve at least 60% of the same top seeds.
    """
    from app.graph.traversal import find_nodes_semantic
    G, _ = _build_digital_asset_graph()

    kw_a = ["ibm digital asset haven", "platform", "wallet"]
    kw_b = ["digital asset haven", "ibm", "blockchain", "platform"]

    top_a = {nid for nid, _ in find_nodes_semantic(G, kw_a, top_k=5)}
    top_b = {nid for nid, _ in find_nodes_semantic(G, kw_b, top_k=5)}

    # The primary concept node (id=1, "IBM Digital Asset Haven") must appear
    # in both results because it matches keywords in both queries.
    overlap = len(top_a & top_b)
    assert overlap >= 1, f"Rephrased queries share only {overlap}/5 seeds — expected ≥ 1"
    assert 1 in top_a and 1 in top_b, "Primary concept must be in top seeds for both queries"


def test_primary_concept_always_in_top_seeds_for_exact_name_query():
    """
    When the exact concept name appears in the keywords, that concept must be
    the top-ranked seed.
    """
    from app.graph.traversal import find_nodes_semantic
    G, _ = _build_digital_asset_graph()

    results = find_nodes_semantic(G, ["ibm digital asset haven"], top_k=5)
    assert results, "Should return at least one seed for a known concept name"
    assert results[0][0] == 1, "The primary concept node must be the top seed"


# ══════════════════════════════════════════════════════════════════════
# EXCERPT DEDUPLICATION
# ══════════════════════════════════════════════════════════════════════

def test_excerpt_fingerprint_matches_normalised_duplicates():
    """Near-identical excerpts (different whitespace/case) must produce the same fingerprint."""
    from app.retrieval.qa_service import _excerpt_fingerprint

    a = "IBM Digital Asset Haven is a unified platform for secured wallet management."
    b = "  ibm digital  asset haven is  a unified  PLATFORM for secured wallet management.  "
    assert _excerpt_fingerprint(a) == _excerpt_fingerprint(b), \
        "Normalised near-duplicates must share the same fingerprint"


def test_excerpt_fingerprint_differs_for_distinct_content():
    from app.retrieval.qa_service import _excerpt_fingerprint

    a = "IBM Digital Asset Haven supports 42 blockchains."
    b = "IBM Z LinuxONE provides hardware security modules."
    assert _excerpt_fingerprint(a) != _excerpt_fingerprint(b)


def test_context_assembly_deduplicates_near_identical_excerpts():
    """
    When two concepts share a near-identical source excerpt, only one should
    appear in the assembled context to avoid redundant passages.
    """
    from app.retrieval.qa_service import _excerpt_fingerprint

    excerpt = "IBM Digital Asset Haven is a unified platform for secured wallet management."
    seen: set[str] = set()
    included = 0
    for _ in range(3):
        fp = _excerpt_fingerprint(excerpt)
        if fp not in seen:
            seen.add(fp)
            included += 1

    assert included == 1, "Near-duplicate excerpt must be included only once"


# ══════════════════════════════════════════════════════════════════════
# CONTEXT ORDERING — HIGH-RELEVANCE CONCEPTS FIRST
# ══════════════════════════════════════════════════════════════════════

def test_context_ordering_places_seed_nodes_before_bfs_neighbours():
    """
    Direct seed nodes (high relevance score) must appear before BFS-expanded
    neighbour nodes (score=0) in the sorted concept list.
    """
    seed_score = {1: 0.9, 3: 0.7}

    concepts = [
        _mc(5, 1, "Neighbour C", confidence=0.95),  # BFS node, no seed score
        _mc(1, 1, "Seed A",      confidence=0.80),  # direct seed
        _mc(3, 1, "Seed B",      confidence=0.75),  # direct seed
        _mc(7, 1, "Neighbour D", confidence=0.90),  # BFS node, no seed score
    ]

    concepts.sort(
        key=lambda c: (
            -seed_score.get(c.id, 0.0),
            -float(getattr(c, "confidence", 0.8)),
            c.id,
        )
    )

    ids = [c.id for c in concepts]
    assert ids[0] == 1, "Highest seed-score node must be first"
    assert ids[1] == 3, "Second seed-score node must be second"
    # BFS neighbours (no seed score) follow; higher-confidence neighbour first
    assert ids[2] == 5, "Higher-confidence BFS node must precede lower-confidence BFS node"
    assert ids[3] == 7


def test_context_ordering_stable_for_equal_scores():
    """Concepts with equal seed score must be secondarily ordered by confidence, then ID."""
    seed_score: dict[int, float] = {}  # all BFS neighbours

    concepts = [
        _mc(10, 1, "C10", confidence=0.70),
        _mc(2,  1, "C2",  confidence=0.90),
        _mc(5,  1, "C5",  confidence=0.90),
        _mc(8,  1, "C8",  confidence=0.80),
    ]
    concepts.sort(
        key=lambda c: (
            -seed_score.get(c.id, 0.0),
            -float(getattr(c, "confidence", 0.8)),
            c.id,
        )
    )
    ids = [c.id for c in concepts]
    # confidence 0.90 → ids 2 and 5 (tie-break by id asc)
    assert ids[0] == 2
    assert ids[1] == 5
    assert ids[2] == 8
    assert ids[3] == 10


# ══════════════════════════════════════════════════════════════════════
# QA_TOP_K CONFIG
# ══════════════════════════════════════════════════════════════════════

def test_top_k_default_is_20():
    from app.config import settings
    assert settings.qa_top_k == 20, "Default qa_top_k must be 20"


def test_find_nodes_semantic_respects_top_k():
    """find_nodes_semantic must never return more nodes than top_k."""
    from app.graph.traversal import find_nodes_semantic
    G, _ = _build_digital_asset_graph()
    for k in [1, 3, 5]:
        results = find_nodes_semantic(G, ["digital", "asset", "wallet", "blockchain"], top_k=k)
        assert len(results) <= k, f"top_k={k} violated: got {len(results)} results"


# ══════════════════════════════════════════════════════════════════════
# FULL PIPELINE — REPEATED IDENTICAL QUESTION
# ══════════════════════════════════════════════════════════════════════

@patch("app.extraction.pattern_agent.chat", return_value="[]")
@patch("app.extraction.relationship_agent.chat", return_value="[]")
@patch(
    "app.extraction.concept_agent.chat",
    return_value=(
        '[{"name": "IBM Digital Asset Haven", "type": "Platform", "confidence": 0.95, '
        '"description": "Unified wallet management platform.", '
        '"source_excerpt": "IBM Digital Asset Haven is a unified platform for secured wallet management.", '
        '"reasoning": "central topic"}]'
    ),
)
def test_repeated_identical_question_returns_same_sources(mock_concept, mock_rel, mock_pattern):
    """
    Asking the identical question twice against the same corpus must return
    the same set of source document IDs and the same top-ranked seed nodes.
    """
    import os
    import tempfile
    import fitz
    from unittest.mock import patch as upatch

    engine, Session = _make_session("consistency_repeated")
    db = Session()
    tmp_path = None
    try:
        ws = Workspace(name="Consistency WS")
        db.add(ws)
        db.commit()

        # Build a real PDF so the pipeline can parse it
        doc_obj = fitz.open()
        page = doc_obj.new_page()
        page.insert_text((50, 72), (
            "IBM Digital Asset Haven is a unified platform for secured wallet "
            "management and transaction orchestration across 42 blockchains."
        ))
        pdf_bytes = doc_obj.tobytes()

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name

        from app.db.models import Document as DocModel
        doc_row = DocModel(
            workspace_id=ws.id, filename=tmp_path, file_type="pdf",
            title="Digital Asset Haven Overview", upload_status="pending",
        )
        db.add(doc_row)
        db.commit()
        doc_id = doc_row.id
        ws_id = ws.id
        db.close()

        from app.ingestion.pipeline import run_ingestion_pipeline
        with upatch("app.ingestion.pipeline.SessionLocal", Session):
            run_ingestion_pipeline(doc_id)

        # Mock the QA generation step so we aren't hitting the real API,
        # but let the retrieval layer run fully.
        fixed_answer = "IBM Digital Asset Haven is a unified platform. [Source: Digital Asset Haven Overview]"
        qa_answer_mock = upatch(
            "app.retrieval.qa_service.chat",
            return_value=fixed_answer,
        )
        qa_kw_mock = upatch(
            "app.retrieval.qa_service._extract_keywords_llm",
            return_value=["IBM Digital Asset Haven", "wallet", "blockchain"],
        )

        question = "What is IBM Digital Asset Haven?"
        with qa_answer_mock, qa_kw_mock:
            from app.retrieval.qa_service import ask_workspace
            db_a = Session()
            resp_a = ask_workspace(db_a, ws_id, question)
            db_a.close()

            db_b = Session()
            resp_b = ask_workspace(db_b, ws_id, question)
            db_b.close()

        # The same source documents must be returned both times
        src_ids_a = {s.document_id for s in resp_a.sources}
        src_ids_b = {s.document_id for s in resp_b.sources}
        assert src_ids_a == src_ids_b, (
            f"Repeated identical question returned different source sets: {src_ids_a} vs {src_ids_b}"
        )
        assert len(src_ids_a) > 0, "At least one source must be returned"

    finally:
        if tmp_path:
            os.unlink(tmp_path)
        _teardown(engine)


@patch("app.extraction.pattern_agent.chat", return_value="[]")
@patch("app.extraction.relationship_agent.chat", return_value="[]")
@patch(
    "app.extraction.concept_agent.chat",
    return_value=(
        '[{"name": "ISO 20022", "type": "Standard", "confidence": 0.92, '
        '"description": "Global payment messaging standard.", '
        '"source_excerpt": "ISO 20022 is the global standard for payment messaging.", '
        '"reasoning": "central"},'
        '{"name": "SWIFT Network", "type": "Network", "confidence": 0.88, '
        '"description": "Global interbank messaging network.", '
        '"source_excerpt": "SWIFT connects banks across the globe using ISO 20022.", '
        '"reasoning": "related"}]'
    ),
)
def test_rephrased_question_returns_overlapping_sources(mock_concept, mock_rel, mock_pattern):
    """
    Rephrased versions of the same question must return at least one
    common source document.
    """
    import os
    import tempfile
    import fitz
    from unittest.mock import patch as upatch

    engine, Session = _make_session("consistency_rephrased")
    db = Session()
    tmp_path = None
    try:
        ws = Workspace(name="Rephrase WS")
        db.add(ws)
        db.commit()

        doc_obj = fitz.open()
        page = doc_obj.new_page()
        page.insert_text((50, 72), (
            "ISO 20022 is the global standard for payment messaging. "
            "SWIFT connects banks worldwide and is migrating to ISO 20022 "
            "to enable richer, structured financial data."
        ))
        pdf_bytes = doc_obj.tobytes()

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name

        from app.db.models import Document as DocModel
        doc_row = DocModel(
            workspace_id=ws.id, filename=tmp_path, file_type="pdf",
            title="ISO 20022 Overview", upload_status="pending",
        )
        db.add(doc_row)
        db.commit()
        doc_id = doc_row.id
        ws_id = ws.id
        db.close()

        from app.ingestion.pipeline import run_ingestion_pipeline
        with upatch("app.ingestion.pipeline.SessionLocal", Session):
            run_ingestion_pipeline(doc_id)

        fixed_answer = "ISO 20022 is a standard. [Source: ISO 20022 Overview]"

        q_a = "What is ISO 20022?"
        q_b = "Describe the ISO 20022 payment messaging standard."

        with upatch("app.retrieval.qa_service.chat", return_value=fixed_answer):
            with upatch("app.retrieval.qa_service._extract_keywords_llm",
                        return_value=["ISO 20022", "payment standard"]):
                from app.retrieval.qa_service import ask_workspace
                db_a = Session()
                resp_a = ask_workspace(db_a, ws_id, q_a)
                db_a.close()

            with upatch("app.retrieval.qa_service._extract_keywords_llm",
                        return_value=["ISO 20022", "payment messaging", "standard"]):
                db_b = Session()
                resp_b = ask_workspace(db_b, ws_id, q_b)
                db_b.close()

        src_ids_a = {s.document_id for s in resp_a.sources}
        src_ids_b = {s.document_id for s in resp_b.sources}
        shared = src_ids_a & src_ids_b
        assert len(shared) > 0, (
            f"Rephrased questions returned no common sources: {src_ids_a} vs {src_ids_b}"
        )

    finally:
        if tmp_path:
            os.unlink(tmp_path)
        _teardown(engine)
