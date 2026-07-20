"""
Permanent workspace-isolation regression test.

Locks in the guarantee verified by the isolation audit: a deck built for one
workspace can NEVER contain another workspace's concepts. Two workspaces are
seeded with unique sentinel concepts; the graph + intelligence digest for each
must contain only its own concepts. This must run forever in CI — cross-workspace
contamination is invisible without a deliberate test like this.
"""
from __future__ import annotations

from sqlalchemy import create_engine, StaticPool
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Workspace, Document, Concept, Relationship
from app.graph.memory_manager import graph_memory_manager
from app.generation.deliverable_service import _build_graph_intelligence, _retrieve_relevant_nodes

RIPPLE = "Ripple RLUSD Payments"          # unique to workspace A (Payments)
SENTINEL = "CBDC_SENTINEL_ZQ7X"           # unique to workspace B (CBDC)


def _session():
    engine = create_engine(
        "sqlite:///file:iso_test?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False, "uri": True},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine)()


def _seed_workspace(db, name, concept_names):
    ws = Workspace(name=name); db.add(ws); db.commit(); db.refresh(ws)
    doc = Document(workspace_id=ws.id, filename=f"/fake/{name}.pdf", file_type="pdf",
                   title=name, raw_text=" ".join(concept_names), upload_status="complete")
    db.add(doc); db.commit(); db.refresh(doc)
    ids = []
    for nm in concept_names:
        c = Concept(workspace_id=ws.id, name=nm, type="Concept",
                    description=f"{nm} description.", source_document_id=doc.id,
                    source_excerpt=f"{nm} quote.", confidence=0.95)
        db.add(c); db.commit(); db.refresh(c); ids.append(c.id)
    # one intra-workspace relationship so the graph has an edge
    if len(ids) >= 2:
        db.add(Relationship(workspace_id=ws.id, source_concept_id=ids[0],
                            target_concept_id=ids[1], relationship_type="related_to",
                            source_document_id=doc.id, strength=0.9))
        db.commit()
    return ws.id


def test_no_cross_workspace_concept_contamination():
    engine, db = _session()
    try:
        ws_a = _seed_workspace(db, "Payments", [RIPPLE, "Cross-Border Settlement"])
        ws_b = _seed_workspace(db, "CBDC", [SENTINEL, "Retail CBDC Wallet"])

        # Force fresh builds from THIS db (evict any cached graphs first).
        g_a = graph_memory_manager.rebuild_workspace_graph(ws_a, db)
        g_b = graph_memory_manager.rebuild_workspace_graph(ws_b, db)

        def names(g):
            rows = db.query(Concept).filter(Concept.id.in_(list(g.nodes()))).all()
            return {c.name for c in rows}, {c.workspace_id for c in rows}

        names_a, wsids_a = names(g_a)
        names_b, wsids_b = names(g_b)

        # Graph-level isolation: every node belongs to its own workspace.
        assert wsids_a == {ws_a}, f"workspace A graph leaked nodes from {wsids_a - {ws_a}}"
        assert wsids_b == {ws_b}, f"workspace B graph leaked nodes from {wsids_b - {ws_b}}"
        assert SENTINEL not in names_a
        assert RIPPLE not in names_b

        # Digest-level isolation: the intelligence brief each deck is built from
        # contains only its own workspace's concepts.
        digest_a, *_ = _build_graph_intelligence(
            g_a, db, ws_a, _retrieve_relevant_nodes(g_a, None), None, "client_201")
        digest_b, *_ = _build_graph_intelligence(
            g_b, db, ws_b, _retrieve_relevant_nodes(g_b, None), None, "client_201")

        assert SENTINEL not in digest_a          # CBDC never bleeds into Payments
        assert "Ripple" not in digest_b          # Payments never bleeds into CBDC
        assert SENTINEL in digest_b              # sanity: B really does use its own concept
    finally:
        graph_memory_manager.invalidate(ws_a)
        graph_memory_manager.invalidate(ws_b)
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
