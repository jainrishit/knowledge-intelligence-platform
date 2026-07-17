"""
Knowledge graph layer — builds a NetworkX graph from SQL tables and provides
traversal utilities.  All external callers should use graph_memory_manager
instead of calling build_workspace_graph() directly.
"""
from app.graph.memory_manager import graph_memory_manager

__all__ = ["graph_memory_manager"]
