"""
GraphMemoryManager — single entry point for all graph operations.

Architecture
------------
SQL tables remain the system of record. The NetworkX graph is a derived,
in-memory operational layer maintained by this manager.

Every node stores:
    source_document_ids : set[int]  — which documents contributed this concept
    evidence_count      : int       — number of contributing documents

Every edge stores:
    source_document_ids : set[int]  — which documents support this relationship
    evidence_count      : int       — number of supporting documents

Lifecycle
---------
startup_load()              — called once on application startup; loads all
                              workspaces that have at least one complete document.
get_workspace_graph()       — returns the in-memory graph; builds from SQL on
                              first access (cache miss).
add_document_contributions()  — incremental node/edge additions after ingestion.
remove_document_contributions() — evidence-based pruning after document deletion.
invalidate()                — evict the cached graph so the next access rebuilds.
rebuild_workspace_graph()   — force a full SQL rebuild for one workspace.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

import networkx as nx
from sqlalchemy.orm import Session

from app.db.models import Concept, Relationship, Workspace
from app.db.session import SessionLocal
from app.graph.builder import build_workspace_graph
from app.graph.store import WorkspaceGraphStore

logger = logging.getLogger(__name__)

def _node_attrs_from_concept(c: Concept) -> dict:
    """Build the node attribute dict for a concept, including provenance sets."""
    doc_ids: set[int] = {c.source_document_id} if c.source_document_id else set()
    return {
        "name": c.name,
        "type": c.type or "General",
        "description": c.description or "",
        "source_document_id": c.source_document_id,
        "source_excerpt": c.source_excerpt or "",
        "confidence": float(getattr(c, "confidence", 0.8)),
        # provenance
        "source_document_ids": doc_ids,
        "evidence_count": len(doc_ids),
    }


def _edge_attrs_from_relationship(r: Relationship) -> dict:
    """Build the edge attribute dict for a relationship, including provenance sets."""
    doc_ids: set[int] = {r.source_document_id} if r.source_document_id else set()
    strength = float(getattr(r, "strength", 0.7)) or 0.7
    return {
        "relationship_id": r.id,
        "relationship_type": r.relationship_type,
        "source_document_id": r.source_document_id,
        "strength": strength,
        "weight": round(1.0 / max(strength, 0.01), 4),
        # provenance
        "source_document_ids": doc_ids,
        "evidence_count": len(doc_ids),
    }


def _build_provenance_graph(db: Session, workspace_id: int) -> nx.DiGraph:
    """
    Build a provenance-aware graph from SQL.

    Differs from builder.build_workspace_graph() in that each node and edge
    carries a `source_document_ids` set and `evidence_count` integer, which
    enable incremental add/remove operations without a full SQL rebuild.
    """
    G = nx.DiGraph()

    concepts = db.query(Concept).filter(Concept.workspace_id == workspace_id).all()
    for c in concepts:
        G.add_node(c.id, **_node_attrs_from_concept(c))

    relationships = db.query(Relationship).filter(Relationship.workspace_id == workspace_id).all()
    for r in relationships:
        if not (G.has_node(r.source_concept_id) and G.has_node(r.target_concept_id)):
            continue
        G.add_edge(r.source_concept_id, r.target_concept_id, **_edge_attrs_from_relationship(r))

    return G


class GraphMemoryManager:
    """
    Manages the lifecycle of in-memory workspace graphs.

    One instance is created at module level as the application singleton.
    All services call get_workspace_graph() instead of build_workspace_graph().
    """

    def __init__(self) -> None:
        self._store = WorkspaceGraphStore()
        self._build_lock: dict[int, threading.Lock] = {}
        self._meta_lock = threading.Lock()

    def startup_load(self) -> None:
        """
        Load all workspaces with at least one complete document into memory.
        Called once from main.py lifespan after init_db().
        """
        db = SessionLocal()
        try:
            from app.db.models import Document  # local import avoids circular dependency
            workspace_ids = (
                db.query(Workspace.id)
                .join(Document, Document.workspace_id == Workspace.id)
                .filter(Document.upload_status == "complete")
                .distinct()
                .all()
            )
            ws_ids = [row[0] for row in workspace_ids]
            logger.info(
                "[graph_memory] Startup load: %d workspace(s) with complete documents.",
                len(ws_ids),
            )
            for ws_id in ws_ids:
                t0 = time.monotonic()
                G = _build_provenance_graph(db, ws_id)
                self._store.put(ws_id, G)
                logger.info(
                    "[graph_memory] ws=%d loaded: %d nodes, %d edges (%.0fms)",
                    ws_id, G.number_of_nodes(), G.number_of_edges(),
                    (time.monotonic() - t0) * 1000,
                )
        except Exception:
            logger.exception("[graph_memory] Startup load failed — graphs will be built on first access.")
        finally:
            db.close()

    def get_workspace_graph(self, workspace_id: int, db: Session | None = None) -> nx.DiGraph:
        """
        Return the in-memory graph for this workspace.

        If the graph is not yet loaded, builds it from SQL (cache miss).
        A per-workspace lock prevents duplicate builds when two requests arrive
        simultaneously for an un-loaded workspace.

        Parameters
        ----------
        workspace_id : the workspace to retrieve.
        db           : optional SQLAlchemy session; only used on cache miss.
                       If None on a miss, a new session is opened internally.
        """
        G = self._store.get(workspace_id)
        if G is not None:
            return G

        build_lock = self._get_build_lock(workspace_id)
        with build_lock:
            # Re-check after acquiring the lock — another thread may have built it.
            G = self._store.get(workspace_id)
            if G is not None:
                return G

            return self._build_and_store(workspace_id, db)

    def add_document_contributions(self, workspace_id: int, document_id: int, db: Session) -> None:
        """
        Incrementally add a document's concepts and relationships to the
        workspace graph.  If the graph is not loaded, performs a full build
        from SQL (which will include the new document's contributions naturally).

        This is called by the ingestion pipeline immediately after
        upload_status is set to "complete".
        """
        G = self._store.get(workspace_id)
        if G is None:
            self._build_and_store(workspace_id, db)
            logger.info(
                "[graph_memory] ws=%d doc=%d — graph was not loaded; performed full build.",
                workspace_id, document_id,
            )
            self._bump_graph_version(db, workspace_id)
            return

        t0 = time.monotonic()
        build_lock = self._get_build_lock(workspace_id)
        with build_lock:
            # Load only concepts and relationships from this document.
            new_concepts = (
                db.query(Concept)
                .filter(Concept.workspace_id == workspace_id, Concept.source_document_id == document_id)
                .all()
            )
            new_relationships = (
                db.query(Relationship)
                .filter(
                    Relationship.workspace_id == workspace_id,
                    Relationship.source_document_id == document_id,
                )
                .all()
            )

            nodes_added = 0
            edges_added = 0

            for c in new_concepts:
                if G.has_node(c.id):
                    # Merge provenance into existing node.
                    existing_ids: set[int] = G.nodes[c.id].get("source_document_ids", set())
                    existing_ids.add(document_id)
                    G.nodes[c.id]["source_document_ids"] = existing_ids
                    G.nodes[c.id]["evidence_count"] = len(existing_ids)
                else:
                    G.add_node(c.id, **_node_attrs_from_concept(c))
                    nodes_added += 1

            for r in new_relationships:
                if not (G.has_node(r.source_concept_id) and G.has_node(r.target_concept_id)):
                    continue
                if G.has_edge(r.source_concept_id, r.target_concept_id):
                    # Merge provenance into existing edge.
                    existing_ids = G.edges[r.source_concept_id, r.target_concept_id].get(
                        "source_document_ids", set()
                    )
                    existing_ids.add(document_id)
                    G.edges[r.source_concept_id, r.target_concept_id]["source_document_ids"] = existing_ids
                    G.edges[r.source_concept_id, r.target_concept_id]["evidence_count"] = len(existing_ids)
                else:
                    G.add_edge(
                        r.source_concept_id,
                        r.target_concept_id,
                        **_edge_attrs_from_relationship(r),
                    )
                    edges_added += 1

        self._bump_graph_version(db, workspace_id)
        logger.info(
            "[graph_memory] ws=%d doc=%d — incremental add: +%d nodes, +%d edges (%.0fms)",
            workspace_id, document_id, nodes_added, edges_added,
            (time.monotonic() - t0) * 1000,
        )

    def remove_document_contributions(self, workspace_id: int, document_id: int) -> None:
        """
        Remove a document's evidence from the workspace graph.

        Nodes and edges that still have other supporting documents are kept;
        only those whose entire evidence base was this document are removed.
        This is called by the documents API immediately before the DB delete.
        """
        G = self._store.get(workspace_id)
        if G is None:
            # Nothing cached — the next access will rebuild from updated SQL.
            return

        t0 = time.monotonic()
        build_lock = self._get_build_lock(workspace_id)
        with build_lock:
            # Remove document from edge provenance; collect edges to delete.
            edges_to_remove: list[tuple[int, int]] = []
            for src, tgt, data in list(G.edges(data=True)):
                doc_ids: set[int] = data.get("source_document_ids", set())
                doc_ids.discard(document_id)
                if not doc_ids:
                    edges_to_remove.append((src, tgt))
                else:
                    data["source_document_ids"] = doc_ids
                    data["evidence_count"] = len(doc_ids)

            for src, tgt in edges_to_remove:
                G.remove_edge(src, tgt)

            # Remove document from node provenance; collect nodes to delete.
            nodes_to_remove: list[int] = []
            for node_id, data in list(G.nodes(data=True)):
                doc_ids = data.get("source_document_ids", set())
                doc_ids.discard(document_id)
                if not doc_ids:
                    nodes_to_remove.append(node_id)
                else:
                    data["source_document_ids"] = doc_ids
                    data["evidence_count"] = len(doc_ids)

            for node_id in nodes_to_remove:
                G.remove_node(node_id)

        logger.info(
            "[graph_memory] ws=%d doc=%d removed: -%d nodes, -%d edges (%.0fms)",
            workspace_id, document_id, len(nodes_to_remove), len(edges_to_remove),
            (time.monotonic() - t0) * 1000,
        )

    def invalidate(self, workspace_id: int) -> None:
        """Evict the cached graph; next get_workspace_graph() will rebuild from SQL."""
        self._store.evict(workspace_id)
        logger.debug("[graph_memory] ws=%d evicted from cache.", workspace_id)

    def rebuild_workspace_graph(self, workspace_id: int, db: Session) -> nx.DiGraph:
        """Force a full SQL rebuild for one workspace, replacing the cached graph."""
        self._store.evict(workspace_id)
        return self._build_and_store(workspace_id, db)

    def _build_and_store(self, workspace_id: int, db: Session | None) -> nx.DiGraph:
        t0 = time.monotonic()
        own_session = db is None
        if own_session:
            db = SessionLocal()
        try:
            G = _build_provenance_graph(db, workspace_id)
            self._store.put(workspace_id, G)
            logger.info(
                "[graph_memory] ws=%d full build: %d nodes, %d edges (%.0fms)",
                workspace_id, G.number_of_nodes(), G.number_of_edges(),
                (time.monotonic() - t0) * 1000,
            )
            return G
        finally:
            if own_session:
                db.close()

    def _get_build_lock(self, workspace_id: int) -> threading.Lock:
        with self._meta_lock:
            if workspace_id not in self._build_lock:
                self._build_lock[workspace_id] = threading.Lock()
            return self._build_lock[workspace_id]

    def _bump_graph_version(self, db: Session, workspace_id: int) -> None:
        """Increment graph_version and set graph_last_updated on the Workspace row."""
        try:
            ws = db.get(Workspace, workspace_id)
            if ws:
                ws.graph_version = (ws.graph_version or 0) + 1
                ws.graph_last_updated = datetime.now(timezone.utc)
                db.commit()
        except Exception:
            logger.exception(
                "[graph_memory] ws=%d — failed to bump graph_version.", workspace_id
            )


# Application-level singleton — shared across all threads.
graph_memory_manager = GraphMemoryManager()
