"""Read-only query layer for the EconGraph API.

The one piece of real logic in this phase is the `customer_of` derivation
(CLAUDE.md invariant #2): the DB stores only `supplies | competes_with |
regulated_by`. When a query asks for `customer_of`, we synthesize edges by
REVERSING the relevant `supplies` rows -- "A supplies B" becomes "B is a
customer_of A". Derived edges carry a synthetic key
``derived:customer_of:<underlying_supplies_edge_id>`` so the API's edge
endpoint can resolve back to the real provenance.

All SQL is parameterized; no string interpolation. Functions take an open
``sqlite3.Connection`` and return plain dicts (graphology-shaped).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import deque
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from rapidfuzz import fuzz, process


log = logging.getLogger("api.query")


# Edge types known to the storage layer (per the EdgeType enum).
STORED_EDGE_TYPES = {"supplies", "competes_with", "regulated_by", "part_of"}
DERIVED_EDGE_TYPES = {"customer_of"}
ALL_QUERY_TYPES = STORED_EDGE_TYPES | DERIVED_EDGE_TYPES

# Hard limits for the subgraph endpoint.
MAX_HOPS = 3
MAX_SUBGRAPH_NODES = 500

# Sentinel key prefix for derived edges -- keeps them distinguishable from
# real stored edges in the response stream.
DERIVED_KEY_PREFIX = "derived:customer_of:"


# ---------------------------------------------------------------------------
# Row -> dict conversion (graphology attribute shape)
# ---------------------------------------------------------------------------

def _node_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    metadata = json.loads(row["metadata"] or "{}")
    return {
        "key": row["id"],
        "attributes": {
            "label": row["name"],
            "type": row["type"],
            "sector": row["sector"],
            "industry": row["industry"],
            "country": row["country"],
            "tickers": json.loads(row["tickers"] or "[]"),
            "identifiers": json.loads(row["identifiers"] or "{}"),
            "aliases": json.loads(row["aliases"] or "[]"),
            "provisional": bool(metadata.get("provisional", False)),
            "identity_unverified": bool(metadata.get("identity_unverified", False)),
            "metadata": metadata,
        },
    }


def _edge_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    extra = json.loads(row["additional_provenance"] or "[]")
    return {
        "key": row["id"],
        "source": row["source"],
        "target": row["target"],
        "attributes": {
            "type": row["type"],
            "directed": bool(row["directed"]),
            "confidence": row["confidence"],
            "weight": row["weight"],
            "below_threshold": bool(row["below_threshold"]),
            "provenance": {
                "filing": row["prov_filing"],
                "url": row["prov_url"],
                "snippet": row["prov_snippet"],
                "extracted_by": row["prov_extracted_by"],
            },
            "additional_provenance": extra,
            "derived": False,
        },
        "undirected": row["type"] == "competes_with",
    }


def _derived_customer_of(supplies_row: sqlite3.Row) -> dict[str, Any]:
    """Synthesize a `customer_of` edge by reversing a stored `supplies` row.

    "A supplies B" (source=A, target=B)  ==>  "B customer_of A" (source=B, target=A).
    The synthetic key points back at the underlying supplies edge so the
    /edge endpoint can resolve to real provenance.
    """
    underlying_id = supplies_row["id"]
    extra = json.loads(supplies_row["additional_provenance"] or "[]")
    return {
        "key": DERIVED_KEY_PREFIX + underlying_id,
        "source": supplies_row["target"],  # reversed
        "target": supplies_row["source"],
        "attributes": {
            "type": "customer_of",
            "directed": True,
            "confidence": supplies_row["confidence"],
            "weight": supplies_row["weight"],
            "below_threshold": bool(supplies_row["below_threshold"]),
            "provenance": {
                "filing": supplies_row["prov_filing"],
                "url": supplies_row["prov_url"],
                "snippet": supplies_row["prov_snippet"],
                "extracted_by": supplies_row["prov_extracted_by"],
            },
            "additional_provenance": extra,
            "derived": True,
            "underlying_edge_id": underlying_id,
        },
        "undirected": False,
    }


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def resolve_id(conn: sqlite3.Connection, raw_id: str) -> Optional[str]:
    """Return the canonical id for `raw_id`. If it's already a node id, returns
    it unchanged. If it's an alias (raw or normalized), resolves via the
    alias table. Returns None if nothing matches.
    """
    if not raw_id:
        return None
    # Direct hit on the canonical id column.
    row = conn.execute("SELECT id FROM nodes WHERE id = ?", (raw_id,)).fetchone()
    if row:
        return row["id"]
    # Alias lookup: try raw, then normalized (best-effort lowercase fallback).
    row = conn.execute(
        "SELECT node_id FROM aliases WHERE alias = ? LIMIT 1", (raw_id,)
    ).fetchone()
    if row:
        return row["node_id"]
    # Normalized lookup -- use the resolver's normalize() so the same
    # normalization that built the table is used at read time.
    from pipeline.resolve import normalize as _normalize  # local import: avoid heavy dep at module load
    norm = _normalize(raw_id)
    if not norm:
        return None
    row = conn.execute(
        "SELECT node_id FROM aliases WHERE alias_normalized = ? LIMIT 1", (norm,)
    ).fetchone()
    return row["node_id"] if row else None


def get_node(conn: sqlite3.Connection, node_id: str) -> Optional[dict[str, Any]]:
    canonical = resolve_id(conn, node_id)
    if canonical is None:
        return None
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (canonical,)).fetchone()
    return _node_row_to_dict(row) if row else None


def get_edge(conn: sqlite3.Connection, edge_id: str) -> Optional[dict[str, Any]]:
    """Return an edge by key, transparently handling derived customer_of keys."""
    if edge_id.startswith(DERIVED_KEY_PREFIX):
        underlying = edge_id[len(DERIVED_KEY_PREFIX):]
        row = conn.execute(
            "SELECT * FROM edges WHERE id = ? AND type = 'supplies'", (underlying,)
        ).fetchone()
        return _derived_customer_of(row) if row else None
    row = conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone()
    return _edge_row_to_dict(row) if row else None


def _normalize_types(types: Optional[Iterable[str]]) -> set[str]:
    """Normalize the `types` filter into a set the queries can consult."""
    if not types:
        return set(ALL_QUERY_TYPES)
    out: set[str] = set()
    for t in types:
        t = (t or "").strip()
        if t in ALL_QUERY_TYPES:
            out.add(t)
    return out


def get_node_edges(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    types: Optional[Iterable[str]] = None,
    include_provisional: bool = False,
    include_inferred: bool = False,
) -> list[dict[str, Any]]:
    """Return 1-hop edges touching `node_id`, with customer_of synthesized.

    Direction semantics (per Phase 5 prompt §B2):
      suppliers of N        = stored supplies edges where target=N (incoming)
      customers of N        = stored supplies edges where source=N, REVERSED into customer_of
      competitors of N      = competes_with neighbors (treated undirected)
      regulators of N       = regulated_by where source=N
    """
    requested = _normalize_types(types)
    if not requested:
        return []
    # Layer filter -- which subsets of below_threshold rows are visible.
    # Core (below_threshold=0) is always included. The two toggle layers:
    #   - audit / provisional: below_threshold=1 AND extracted_by NOT LIKE 'inference:%'
    #   - inference (co-mention closure): below_threshold=1 AND extracted_by LIKE 'inference:%'
    layers: list[str] = ["below_threshold = 0"]
    if include_inferred:
        layers.append("(below_threshold = 1 AND prov_extracted_by LIKE 'inference:%')")
    if include_provisional:
        layers.append("(below_threshold = 1 AND prov_extracted_by NOT LIKE 'inference:%')")
    below_clause = "AND (" + " OR ".join(layers) + ")"

    edges: list[dict[str, Any]] = []

    # supplies (incoming OR outgoing) -- the actual stored rows.
    if "supplies" in requested:
        rows = conn.execute(
            f"""
            SELECT * FROM edges
            WHERE type = 'supplies' AND (source = ? OR target = ?) {below_clause}
            """,
            (node_id, node_id),
        ).fetchall()
        edges.extend(_edge_row_to_dict(r) for r in rows)

    # customer_of -- synthesized by reversing EVERY stored supplies edge that
    # touches `node_id`, in either direction.
    #
    # Reversal rule (PHASE5_PROMPT §B1):
    #   stored (A --supplies--> B)   ==>   derived (B --customer_of--> A)
    #
    # So for an ego query on N:
    #   * stored supplies with target=N -> N appears as SOURCE of derived
    #     customer_of edges ("N is a customer of <supplier>"). This is the
    #     case the Walmart test exercises -- 15 filers each named Walmart
    #     as their >10% customer; the API should surface "Walmart is a
    #     customer of each of those 15 filers."
    #   * stored supplies with source=N -> N appears as TARGET of derived
    #     customer_of edges ("<the customer> is a customer of N").
    if "customer_of" in requested:
        rows = conn.execute(
            f"""
            SELECT * FROM edges
            WHERE type = 'supplies' AND (source = ? OR target = ?) {below_clause}
            """,
            (node_id, node_id),
        ).fetchall()
        edges.extend(_derived_customer_of(r) for r in rows)

    if "competes_with" in requested:
        rows = conn.execute(
            f"""
            SELECT * FROM edges
            WHERE type = 'competes_with' AND (source = ? OR target = ?) {below_clause}
            """,
            (node_id, node_id),
        ).fetchall()
        edges.extend(_edge_row_to_dict(r) for r in rows)

    if "regulated_by" in requested:
        # Both directions: ego(Company) returns its regulators; ego(Regulator)
        # returns the companies it regulates. The edge itself is directed
        # company -> regulator, so the BFS traversal walks it either way.
        rows = conn.execute(
            f"""
            SELECT * FROM edges
            WHERE type = 'regulated_by' AND (source = ? OR target = ?) {below_clause}
            """,
            (node_id, node_id),
        ).fetchall()
        edges.extend(_edge_row_to_dict(r) for r in rows)

    if "part_of" in requested:
        rows = conn.execute(
            f"""
            SELECT * FROM edges
            WHERE type = 'part_of' AND (source = ? OR target = ?) {below_clause}
            """,
            (node_id, node_id),
        ).fetchall()
        edges.extend(_edge_row_to_dict(r) for r in rows)

    return edges


def _collect_nodes_for_edges(
    conn: sqlite3.Connection, edges: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Fetch every node referenced by the given edge dicts. Preserves order."""
    seen: set[str] = set()
    ids: list[str] = []
    for e in edges:
        for k in (e["source"], e["target"]):
            if k not in seen:
                seen.add(k)
                ids.append(k)
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM nodes WHERE id IN ({placeholders})", tuple(ids)
    ).fetchall()
    by_id = {r["id"]: _node_row_to_dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def ego(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    types: Optional[Iterable[str]] = None,
    include_provisional: bool = False,
    include_inferred: bool = False,
) -> Optional[dict[str, Any]]:
    """Ego-graph: center node + 1-hop neighbors along requested types."""
    canonical = resolve_id(conn, node_id)
    if canonical is None:
        return None
    center = get_node(conn, canonical)
    if center is None:
        return None
    edges = get_node_edges(
        conn, canonical,
        types=types,
        include_provisional=include_provisional,
        include_inferred=include_inferred,
    )
    neighbors = _collect_nodes_for_edges(conn, edges)
    # Ensure the center is in the node list (and first).
    nodes_by_id = {n["key"]: n for n in neighbors}
    nodes_by_id[center["key"]] = center
    ordered_keys = [center["key"]] + [n["key"] for n in neighbors if n["key"] != center["key"]]
    return {
        "center": center["key"],
        "nodes": [nodes_by_id[k] for k in ordered_keys],
        "edges": edges,
    }


def bfs_subgraph(
    conn: sqlite3.Connection,
    seed: str,
    *,
    hops: int,
    types: Optional[Iterable[str]] = None,
    include_provisional: bool = False,
    include_inferred: bool = False,
    node_cap: int = MAX_SUBGRAPH_NODES,
) -> Optional[dict[str, Any]]:
    """N-hop BFS from `seed` along requested edge types. customer_of derived."""
    if hops < 0:
        raise ValueError("hops must be >= 0")
    if hops > MAX_HOPS:
        raise ValueError(f"hops capped at {MAX_HOPS}")
    canonical = resolve_id(conn, seed)
    if canonical is None:
        return None

    visited: set[str] = {canonical}
    queue: deque[tuple[str, int]] = deque([(canonical, 0)])
    edges_by_key: dict[str, dict[str, Any]] = {}
    truncated = False
    while queue:
        current, depth = queue.popleft()
        if depth >= hops:
            continue
        for edge in get_node_edges(
            conn, current,
            types=types,
            include_provisional=include_provisional,
            include_inferred=include_inferred,
        ):
            if edge["key"] in edges_by_key:
                continue
            edges_by_key[edge["key"]] = edge
            for neighbor in (edge["source"], edge["target"]):
                if neighbor in visited:
                    continue
                if len(visited) >= node_cap:
                    truncated = True
                    continue
                visited.add(neighbor)
                queue.append((neighbor, depth + 1))
        if truncated:
            # Stop expanding further once we've hit the node cap; the partial
            # graph is still returned with the truncated flag set.
            break

    placeholders = ",".join("?" * len(visited))
    node_rows = conn.execute(
        f"SELECT * FROM nodes WHERE id IN ({placeholders})", tuple(visited)
    ).fetchall()
    return {
        "seed": canonical,
        "hops": hops,
        "nodes": [_node_row_to_dict(r) for r in node_rows],
        "edges": list(edges_by_key.values()),
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@dataclass
class AliasIndex:
    """In-memory index of (alias_normalized -> [(node_id, alias_raw)]).

    Built once at API startup. 392 aliases is tiny -- rapidfuzz over the
    whole list per query is fast enough (sub-millisecond at this size).
    """

    # canonical_id -> name (for display in results)
    name_by_id: dict[str, str]
    # All (alias_normalized, canonical_id) tuples in load order.
    normalized: list[tuple[str, str]]

    @classmethod
    def build(cls, conn: sqlite3.Connection) -> "AliasIndex":
        names = {
            r["id"]: r["name"]
            for r in conn.execute("SELECT id, name FROM nodes").fetchall()
        }
        norm_rows = conn.execute(
            "SELECT alias_normalized, node_id FROM aliases"
        ).fetchall()
        return cls(
            name_by_id=names,
            normalized=[(r["alias_normalized"], r["node_id"]) for r in norm_rows],
        )

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Rank candidates by exact, prefix, and fuzzy score on alias_normalized."""
        from pipeline.resolve import normalize as _normalize

        q_norm = _normalize(query)
        if not q_norm:
            return []
        # Score each canonical id once -- take the BEST score across its aliases.
        best: dict[str, tuple[int, str]] = {}  # canonical_id -> (score, matched_alias)
        for alias_norm, canonical_id in self.normalized:
            score = 0
            if alias_norm == q_norm:
                score = 100
            elif alias_norm.startswith(q_norm) or q_norm in alias_norm:
                # Substring/prefix bonus, but never beats exact.
                score = 95
            else:
                score = int(fuzz.token_set_ratio(q_norm, alias_norm))
            existing = best.get(canonical_id)
            if existing is None or score > existing[0]:
                best[canonical_id] = (score, alias_norm)
        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])
        results = []
        for canonical_id, (score, matched) in ranked[:limit]:
            results.append(
                {
                    "id": canonical_id,
                    "name": self.name_by_id.get(canonical_id, canonical_id),
                    "score": score,
                    "matched_alias_normalized": matched,
                }
            )
        return results


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def health(conn: sqlite3.Connection) -> dict[str, Any]:
    """Counts + the invariant check the test relies on."""
    n_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    e_count = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    customer_of_count = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE type = 'customer_of'"
    ).fetchone()[0]
    core_count = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE below_threshold = 0"
    ).fetchone()[0]
    audit_count = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE below_threshold = 1"
    ).fetchone()[0]
    return {
        # ``ok`` is True iff invariant #2 holds in the live DB.
        "status": "ok" if customer_of_count == 0 else "INVARIANT_VIOLATED",
        "node_count": n_count,
        "edge_count": e_count,
        "core_edge_count": core_count,
        "audit_edge_count": audit_count,
        "customer_of_rows_in_db": customer_of_count,
    }
