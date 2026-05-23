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
    prov_filing      TEXT NOT NULL,
    prov_url         TEXT NOT NULL,
    prov_snippet     TEXT NOT NULL,
    prov_extracted_by TEXT NOT NULL CHECK (prov_extracted_by IN ('llm','rule','manual')),
    FOREIGN KEY (source) REFERENCES nodes(id),
    FOREIGN KEY (target) REFERENCES nodes(id)
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(type);
-- De-dupe key for competes_with (treat as unordered pair, see PRD §4).
CREATE UNIQUE INDEX IF NOT EXISTS uq_edges_triple
    ON edges(source, target, type);

CREATE TABLE IF NOT EXISTS aliases (
    alias        TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    PRIMARY KEY (alias, node_id),
    FOREIGN KEY (node_id) REFERENCES nodes(id)
);

CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias);
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


def upsert_edge(conn: sqlite3.Connection, edge: Union[Edge, dict[str, Any]]) -> Edge:
    """Validate via Pydantic then upsert into `edges`. Returns the validated Edge."""
    e = edge if isinstance(edge, Edge) else Edge.model_validate(edge)
    conn.execute(
        """
        INSERT INTO edges (id, source, target, type, directed, confidence, weight,
                           prov_filing, prov_url, prov_snippet, prov_extracted_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            prov_extracted_by=excluded.prov_extracted_by
        """,
        (
            e.id,
            e.source,
            e.target,
            e.type if isinstance(e.type, str) else e.type.value,
            1 if e.directed else 0,
            e.confidence,
            e.weight,
            e.provenance.filing,
            e.provenance.url,
            e.provenance.snippet,
            e.provenance.extracted_by,
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
        }
    )


def add_aliases(conn: sqlite3.Connection, node_id: str, aliases: Iterable[str]) -> None:
    """Map alias strings -> canonical node id. Idempotent."""
    rows = [(a, node_id) for a in aliases if a]
    if not rows:
        return
    conn.executemany(
        "INSERT OR IGNORE INTO aliases (alias, node_id) VALUES (?, ?)", rows
    )
    conn.commit()
