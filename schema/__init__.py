"""EconGraph schema package — Pydantic models + SQLite source-of-truth."""

from .models import Edge, EdgeType, Node, NodeType, Provenance
from .store import (
    add_aliases,
    connect,
    get_edge,
    get_node,
    init_db,
    upsert_edge,
    upsert_node,
)

__all__ = [
    "Edge",
    "EdgeType",
    "Node",
    "NodeType",
    "Provenance",
    "add_aliases",
    "connect",
    "get_edge",
    "get_node",
    "init_db",
    "upsert_edge",
    "upsert_node",
]
