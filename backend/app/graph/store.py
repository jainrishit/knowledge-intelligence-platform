"""
Workspace graph store — thread-safe in-process cache of per-workspace NetworkX graphs.

The store is the single source of graph instances. No code outside
GraphMemoryManager should ever read from or write to this store directly.
"""
from __future__ import annotations

import threading

import networkx as nx


class WorkspaceGraphStore:
    """
    Thread-safe dictionary mapping workspace_id → nx.DiGraph.

    All mutations acquire an RLock so that concurrent ingestion pipelines
    (each running in a background thread) cannot corrupt a graph that is
    simultaneously being traversed by a QA or deliverable request.
    """

    def __init__(self) -> None:
        self._graphs: dict[int, nx.DiGraph] = {}
        self._lock = threading.RLock()

    def get(self, workspace_id: int) -> nx.DiGraph | None:
        """Return the cached graph for this workspace, or None if not loaded."""
        with self._lock:
            return self._graphs.get(workspace_id)

    def has(self, workspace_id: int) -> bool:
        """Return True if a graph is loaded for this workspace."""
        with self._lock:
            return workspace_id in self._graphs

    def workspace_ids(self) -> list[int]:
        """Return sorted list of workspace IDs currently in the store."""
        with self._lock:
            return sorted(self._graphs.keys())

    def put(self, workspace_id: int, graph: nx.DiGraph) -> None:
        """Replace (or set) the graph for this workspace."""
        with self._lock:
            self._graphs[workspace_id] = graph

    def evict(self, workspace_id: int) -> None:
        """Remove the graph for this workspace from the store."""
        with self._lock:
            self._graphs.pop(workspace_id, None)

    def clear(self) -> None:
        """Remove all graphs (used in tests)."""
        with self._lock:
            self._graphs.clear()
