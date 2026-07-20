"""
Permanent regression test for the document-deletion cascade.

Fixes the audited bug where deleting a document removed only the document row and
transiently pruned the in-memory graph, leaving its concepts/relationships/patterns
in the DB — so the deleted document's knowledge reappeared on graph rebuild and kept
influencing deck generation.

Guarantees, locked in CI:
  - deleting a document deletes the concepts/relationships whose sole source was it,
  - orphaned patterns are removed; multi-source patterns are retained (doc dropped),
  - other documents' knowledge is untouched,
  - the rebuilt-from-DB graph reflects the deletion permanently (no resurrection),
  - no orphaned records remain.
"""
from __future__ import annotations

from sqlalchemy import create_engine, StaticPool
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Workspace, Document, Concept, Relationship, ConsultingPattern
from app.graph.memory_manager import graph_memory_manager as gmm
from app.api.documents import delete_document


def _session():
    engine = create_engine(
        "sqlite:///file:doc_del_test?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False, "uri": True},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine)()


def _ws(db, name="DEL"):
    w = Workspace(name=name); db.add(w); db.commit(); db.refresh(w); return w.id


def _doc(db, ws, title):
    d = Document(workspace_id=ws, filename=f"{title}.pdf", file_type="pdf",
                 title=title, raw_text="x", upload_status="complete")
    db.add(d); db.commit(); db.refresh(d); return d.id


def _concept(db, ws, doc, name):
    c = Concept(workspace_id=ws, name=name, type="Concept", description="d",
                source_document_id=doc, confidence=0.9)
    db.add(c); db.commit(); db.refresh(c); return c.id


def _rel(db, ws, s, t, doc):
    db.add(Relationship(workspace_id=ws, source_concept_id=s, target_concept_id=t,
                        relationship_type="related_to", source_document_id=doc, strength=0.8))
    db.commit()


def _orphans(db, ws):
    valid_docs = db.query(Document.id).subquery()
    valid_c = [x for (x,) in db.query(Concept.id).filter_by(workspace_id=ws).all()] or [-1]
    concepts_bad = db.query(Concept).filter(
        Concept.workspace_id == ws, Concept.source_document_id.isnot(None),
        ~Concept.source_document_id.in_(db.query(Document.id))).count()
    rels_bad = db.query(Relationship).filter(
        Relationship.workspace_id == ws,
        (~Relationship.source_concept_id.in_(valid_c)) | (~Relationship.target_concept_id.in_(valid_c))).count()
    return concepts_bad + rels_bad


def test_single_document_deletion_cascades_and_stays_deleted():
    engine, db = _session()
    try:
        ws = _ws(db)
        d = _doc(db, ws, "Doc")
        cs = [_concept(db, ws, d, f"C{i}") for i in range(3)]
        _rel(db, ws, cs[0], cs[1], d); _rel(db, ws, cs[1], cs[2], d)
        db.add(ConsultingPattern(workspace_id=ws, name="P", problem_statement="p",
                                 ibm_approach=["a"], related_concept_ids=[cs[0]],
                                 source_document_ids=[d]))
        db.commit()
        gmm.rebuild_workspace_graph(ws, db)  # warm cache

        delete_document(d, db)

        assert db.query(Document).filter_by(workspace_id=ws).count() == 0
        assert db.query(Concept).filter_by(workspace_id=ws).count() == 0
        assert db.query(Relationship).filter_by(workspace_id=ws).count() == 0
        assert db.query(ConsultingPattern).filter_by(workspace_id=ws).count() == 0
        assert _orphans(db, ws) == 0
        # The knowledge must NOT resurrect when the graph rebuilds from the DB.
        gmm.invalidate(ws)
        g = gmm.rebuild_workspace_graph(ws, db)
        assert g.number_of_nodes() == 0 and g.number_of_edges() == 0
    finally:
        gmm.invalidate(ws)
        Base.metadata.drop_all(bind=engine); engine.dispose()


def test_deleting_one_document_retains_others():
    engine, db = _session()
    try:
        ws = _ws(db)
        a, b = _doc(db, ws, "DocA"), _doc(db, ws, "DocB")
        a1, a2 = _concept(db, ws, a, "A1"), _concept(db, ws, a, "A2")
        b1, b2 = _concept(db, ws, b, "B1"), _concept(db, ws, b, "B2")
        _rel(db, ws, a1, a2, a); _rel(db, ws, b1, b2, b)
        # A-only pattern → should be deleted; A+B pattern → retained with A dropped.
        db.add(ConsultingPattern(workspace_id=ws, name="PA", problem_statement="p",
                                 ibm_approach=["a"], related_concept_ids=[a1], source_document_ids=[a]))
        db.add(ConsultingPattern(workspace_id=ws, name="PAB", problem_statement="p",
                                 ibm_approach=["a"], related_concept_ids=[a1, b1], source_document_ids=[a, b]))
        db.commit()
        gmm.rebuild_workspace_graph(ws, db)

        delete_document(a, db)

        docs = {t for (t,) in db.query(Document.title).filter_by(workspace_id=ws).all()}
        concepts = {n for (n,) in db.query(Concept.name).filter_by(workspace_id=ws).all()}
        patterns = {n for (n,) in db.query(ConsultingPattern.name).filter_by(workspace_id=ws).all()}
        assert docs == {"DocB"}
        assert concepts == {"B1", "B2"}          # A1/A2 gone; B1/B2 kept
        assert patterns == {"PAB"}               # A-only pattern deleted
        pab = db.query(ConsultingPattern).filter_by(workspace_id=ws, name="PAB").one()
        assert [int(x) for x in pab.source_document_ids] == [b]   # A dropped, B retained
        assert a1 not in [int(x) for x in pab.related_concept_ids]
        assert _orphans(db, ws) == 0
        gmm.invalidate(ws)
        g = gmm.rebuild_workspace_graph(ws, db)
        assert g.number_of_nodes() == 2 and g.number_of_edges() == 1   # only B's contribution
    finally:
        gmm.invalidate(ws)
        Base.metadata.drop_all(bind=engine); engine.dispose()
