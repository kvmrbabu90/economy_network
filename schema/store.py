"""SQLite source-of-truth for nodes, edges, and aliases.

Schema is deliberately narrow: three tables only. No `customer_of` edge type,
no reverse-edge table — that view is derived at query time by reversing
`supplies` (CLAUDE.md invariant #2).

Every write goes through the Pydantic models in `schema.models`, so validation
happens at the boundary between in-memory dicts and persisted rows.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from .models import Edge, Node

# Module-level DDL — applied by init_db(). Keeping it as a single block makes
# it cheap to diff and review against the data model in docs/PRD.md §4.
DDL = """
CREATE TABLE IF NOT EXISTS nodes (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    name         TEXT NOT NULL,
    aliases      TEXT NOT NULL DEFAULT '[]',   -- JSON array
    tickers      TEXT NOT NULL DEFAULT '[]',   -- JSON array
    identifiers  TEXT NOT NULL DEFAULT '{}',   -- JSON object
    sector       TEXT,
    industry     TEXT,
    country      TEXT,
    metadata     TEXT NOT NULL DEFAULT '{}'    -- JSON object
);

CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_sector ON nodes(sector);

CREATE TABLE IF NOT EXISTS edges (
    id           TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    target       TEXT NOT NULL,
    type         TEXT NOT NULL,
    directed     INTEGER NOT NULL DEFAULT 1,
    confidence   REAL NOT NULL,
    weight       REAL,
    -- Provenance is required on every edge (CLAUDE.md invariant #3).
    prov_filing      TEXT NOT NULL,    -- may be empty for rule-extracted edges
    prov_url         TEXT NOT NULL,    -- may be empty for rule-extracted edges
    prov_snippet     TEXT NOT NULL,    -- never empty (CLAUDE.md invariant #4)
    prov_extracted_by TEXT NOT NULL
        CHECK (prov_extracted_by IN ('llm','llm:claude-cli','llm:gemma','rule','manual')),
    -- Phase 3 may merge multiple competes_with rows onto one edge. The
    -- additional provenances (full Provenance dicts, JSON-serialized) are
    -- stashed here so SQLite stays the source of truth.
    additional_provenance TEXT NOT NULL DEFAULT '[]',
    -- Phase 5 adds this flag so the API can serve a clean "core" graph
    -- (below_threshold=0, the high-confidence above-cutoff edges) AND a
    -- separate "audit" view (below_threshold=1, mostly provisional-slug
    -- competes_with edges capped low in Phase 3). One source of truth;
    -- the include_provisional API toggle reads this column.
    below_threshold INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (source) REFERENCES nodes(id),
    FOREIGN KEY (target) REFERENCES nodes(id)
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);
CREATE INDEX IF NOT EXISTS idx_edges_below_threshold ON edges(below_threshold);
-- De-dupe key for competes_with (treat as unordered pair, see PRD §4).
CREATE UNIQUE INDEX IF NOT EXISTS uq_edges_triple
    ON edges(source, target, type);

CREATE TABLE IF NOT EXISTS aliases (
    alias              TEXT NOT NULL,
    -- Phase 5 stores the resolver-normalized form so /search can look up
    -- "procter" or "p&g" without re-implementing normalize() in SQL.
    alias_normalized   TEXT NOT NULL DEFAULT '',
    node_id            TEXT NOT NULL,
    PRIMARY KEY (alias, node_id),
    FOREIGN KEY (node_id) REFERENCES nodes(id)
);

CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias);
CREATE INDEX IF NOT EXISTS idx_aliases_normalized ON aliases(alias_normalized);
"""


PathLike = Union[str, Path]


def connect(db_path: PathLike = "econgraph.db") -> sqlite3.Connection:
    """Open a SQLite connection with sensible defaults for this project."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if they do not already exist."""
    conn.executescript(DDL)
    conn.commit()


def upsert_node(conn: sqlite3.Connection, node: Union[Node, dict[str, Any]]) -> Node:
    """Validate via Pydantic then upsert into `nodes`. Returns the validated Node."""
    n = node if isinstance(node, Node) else Node.model_validate(node)
    conn.execute(
        """
        INSERT INTO nodes (id, type, name, aliases, tickers, identifiers,
                           sector, industry, country, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            type=excluded.type,
            name=excluded.name,
            aliases=excluded.aliases,
            tickers=excluded.tickers,
            identifiers=excluded.identifiers,
            sector=excluded.sector,
            industry=excluded.industry,
            country=excluded.country,
            metadata=excluded.metadata
        """,
        (
            n.id,
            n.type if isinstance(n.type, str) else n.type.value,
            n.name,
            json.dumps(n.aliases),
            json.dumps(n.tickers),
            json.dumps(n.identifiers),
            n.sector,
            n.industry,
            n.country,
            json.dumps(n.metadata),
        ),
    )
    conn.commit()
    return n


def upsert_edge(
    conn: sqlite3.Connection,
    edge: Union[Edge, dict[str, Any]],
    *,
    below_threshold: bool = False,
) -> Edge:
    """Validate via Pydantic then upsert into `edges`. Returns the validated Edge.

    `below_threshold=True` flags the row as audit-layer (Phase 5 §A1). The
    column has DEFAULT 0 so existing callers stay correct without changes.
    Invariant: never insert a row whose `type == "customer_of"` -- that
    relationship is derived on read (CLAUDE.md invariant #2).
    """
    e = edge if isinstance(edge, Edge) else Edge.model_validate(edge)
    edge_type = e.type if isinstance(e.type, str) else e.type.value
    if edge_type == "customer_of":
        raise ValueError(
            "Refusing to store an Edge with type='customer_of'. The customer "
            "view is derived from `supplies` at query time (invariant #2)."
        )
    extra_prov = [p.model_dump() if hasattr(p, "model_dump") else p for p in e.additional_provenance]
    conn.execute(
        """
        INSERT INTO edges (id, source, target, type, directed, confidence, weight,
                           prov_filing, prov_url, prov_snippet, prov_extracted_by,
                           additional_provenance, below_threshold)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            source=excluded.source,
            target=excluded.target,
            type=excluded.type,
            directed=excluded.directed,
            confidence=excluded.confidence,
            weight=excluded.weight,
            prov_filing=excluded.prov_filing,
            prov_url=excluded.prov_url,
            prov_snippet=excluded.prov_snippet,
            prov_extracted_by=excluded.prov_extracted_by,
            additional_provenance=excluded.additional_provenance,
            below_threshold=excluded.below_threshold
        """,
        (
            e.id,
            e.source,
            e.target,
            edge_type,
            1 if e.directed else 0,
            e.confidence,
            e.weight,
            e.provenance.filing,
            e.provenance.url,
            e.provenance.snippet,
            e.provenance.extracted_by,
            json.dumps(extra_prov),
            1 if below_threshold else 0,
        ),
    )
    conn.commit()
    return e


def get_node(conn: sqlite3.Connection, node_id: str) -> Optional[Node]:
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
    if row is None:
        return None
    return Node.model_validate(
        {
            "id": row["id"],
            "type": row["type"],
            "name": row["name"],
            "aliases": json.loads(row["aliases"]),
            "tickers": json.loads(row["tickers"]),
            "identifiers": json.loads(row["identifiers"]),
            "sector": row["sector"],
            "industry": row["industry"],
            "country": row["country"],
            "metadata": json.loads(row["metadata"]),
        }
    )


def get_edge(conn: sqlite3.Connection, edge_id: str) -> Optional[Edge]:
    row = conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone()
    if row is None:
        return None
    try:
        extra = json.loads(row["additional_provenance"]) if row["additional_provenance"] else []
    except (KeyError, IndexError):
        extra = []
    return Edge.model_validate(
        {
            "id": row["id"],
            "source": row["source"],
            "target": row["target"],
            "type": row["type"],
            "directed": bool(row["directed"]),
            "confidence": row["confidence"],
            "weight": row["weight"],
            "provenance": {
                "filing": row["prov_filing"],
                "url": row["prov_url"],
                "snippet": row["prov_snippet"],
                "extracted_by": row["prov_extracted_by"],
            },
            "additional_provenance": extra,
        }
    )


def add_aliases(conn: sqlite3.Connection, node_id: str, aliases: Iterable[str]) -> None:
    """Map raw alias strings -> canonical node id. Idempotent.

    The normalized column gets a best-effort lowercase fallback so this older
    entrypoint (used by Phase 0 fixtures) still works. Phase 3+ callers
    should prefer `add_alias_rows()` which carries the resolver's
    deterministic normalize() output.
    """
    rows = [(a, a.lower().strip(), node_id) for a in aliases if a]
    if not rows:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO aliases (alias, alias_normalized, node_id) VALUES (?, ?, ?)",
        rows,
    )
    conn.commit()


def add_alias_rows(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    """Bulk-load aliases.jsonl rows produced by `pipeline.resolve`.

    Each row must carry: alias, alias_normalized, canonical_id.
    Idempotent on (alias, node_id). Returns rows inserted (incl. ignored).
    """
    tuples = [
        (r["alias"], r.get("alias_normalized") or r["alias"].lower().strip(), r["canonical_id"])
        for r in rows
        if r.get("alias") and r.get("canonical_id")
    ]
    if not tuples:
        return 0
    conn.executemany(
        "INSERT OR IGNORE INTO aliases (alias, alias_normalized, node_id) VALUES (?, ?, ?)",
        tuples,
    )
    conn.commit()
    return len(tuples)
