"""EconGraph schema package — Pydantic models + SQLite source-of-truth."""

from .models import CandidateEdge, Edge, EdgeType, Node, NodeType, Provenance
from .store import (
    add_alias_rows,
    add_aliases,
    connect,
    get_edge,
    get_node,
    init_db,
    upsert_edge,
    upsert_node,
)

__all__ = [
    "CandidateEdge",
    "Edge",
    "EdgeType",
    "Node",
    "NodeType",
    "Provenance",
    "add_alias_rows",
    "add_aliases",
    "connect",
    "get_edge",
    "get_node",
    "init_db",
    "upsert_edge",
    "upsert_node",
]
