"""
add_missing_edges.py
Migration script: insert manually-curated supply edges and missing company nodes.
Skips any edge whose (source, target, type) combo already exists.
Skips any node whose id already exists.
"""

import sqlite3
import uuid
import json
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "econgraph.db"

PROV_SNIPPET = "[Manual curation: publicly documented supply relationship]"
EXTRACTED_BY = "manual"
CONFIDENCE = 0.95

# ---------------------------------------------------------------------------
# Nodes to insert
# ---------------------------------------------------------------------------
NEW_NODES = [
    {
        "id": "slug:spirit-aerosystems",
        "type": "Company",
        "name": "Spirit AeroSystems",
        "aliases": "[]",
        "tickers": "[]",
        "identifiers": "{}",
        "sector": "Industrials",
        "industry": "Aerospace & Defense",
        "country": "US",
        "metadata": "{}",
    },
    {
        "id": "slug:peloton",
        "type": "Company",
        "name": "Peloton Interactive",
        "aliases": "[]",
        "tickers": "[]",
        "identifiers": "{}",
        "sector": "Consumer Discretionary",
        "industry": "Leisure Products",
        "country": "US",
        "metadata": "{}",
    },
]

# ---------------------------------------------------------------------------
# Edges to insert  (source, target, edge_type, supply_geography)
# ---------------------------------------------------------------------------
NEW_EDGES = [
    # TSMC → customers
    ("cik:0001046179", "cik:0000320193", "supplies", "global"),   # TSMC → Apple
    ("cik:0001046179", "cik:0001045810", "supplies", "global"),   # TSMC → Nvidia
    ("cik:0001046179", "cik:0000002488", "supplies", "global"),   # TSMC → AMD
    ("cik:0001046179", "cik:0000804328", "supplies", "global"),   # TSMC → Qualcomm
    ("cik:0001046179", "cik:0001730168", "supplies", "global"),   # TSMC → Broadcom
    # Freeport → copper
    ("cik:0000831259", "commodity:copper", "supplies", "global"), # Freeport → Copper
    # Codelco → copper
    ("slug:codelco",   "commodity:copper", "supplies", "global"), # Codelco → Copper
    # Spirit AeroSystems → Boeing
    ("slug:spirit-aerosystems", "cik:0000012927", "supplies", "US"), # Spirit → Boeing
]


def insert_nodes(cur: sqlite3.Cursor) -> list[str]:
    inserted = []
    for n in NEW_NODES:
        existing = cur.execute("SELECT id FROM nodes WHERE id=?", (n["id"],)).fetchone()
        if existing:
            print(f"  [SKIP node] {n['id']} already exists")
            continue
        cur.execute(
            """
            INSERT INTO nodes (id, type, name, aliases, tickers, identifiers,
                               sector, industry, country, metadata)
            VALUES (:id, :type, :name, :aliases, :tickers, :identifiers,
                    :sector, :industry, :country, :metadata)
            """,
            n,
        )
        inserted.append(n["id"])
        print(f"  [INSERT node] {n['id']} — {n['name']}")
    return inserted


def insert_edges(cur: sqlite3.Cursor) -> list[tuple]:
    inserted = []
    for source, target, etype, geo in NEW_EDGES:
        existing = cur.execute(
            "SELECT id FROM edges WHERE source=? AND target=? AND type=?",
            (source, target, etype),
        ).fetchone()
        if existing:
            print(f"  [SKIP edge] {source} --{etype}--> {target} (already exists)")
            continue
        eid = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO edges (id, source, target, type, directed, confidence,
                               weight, prov_filing, prov_url, prov_snippet,
                               prov_extracted_by, additional_provenance,
                               below_threshold, supply_geography)
            VALUES (?, ?, ?, ?, 1, ?, NULL, '', '', ?, ?, '[]', 0, ?)
            """,
            (eid, source, target, etype, CONFIDENCE, PROV_SNIPPET, EXTRACTED_BY, geo),
        )
        inserted.append((source, target, etype, geo))
        print(f"  [INSERT edge] {source} --{etype}--> {target} [{geo}]")
    return inserted


def print_summary(cur: sqlite3.Cursor, inserted_nodes, inserted_edges):
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Nodes inserted : {len(inserted_nodes)}")
    for nid in inserted_nodes:
        print(f"  + {nid}")
    print(f"Edges inserted : {len(inserted_edges)}")
    for src, tgt, etype, geo in inserted_edges:
        print(f"  + {src} --{etype}--> {tgt} [{geo}]")

    print()
    # TSMC edge count
    tsmc_count = cur.execute(
        "SELECT COUNT(*) FROM edges WHERE source='cik:0001046179'",
    ).fetchone()[0]
    print(f"TSMC (cik:0001046179) total outgoing edges : {tsmc_count}")

    tsmc_supplies = cur.execute(
        "SELECT target FROM edges WHERE source='cik:0001046179' AND type='supplies'",
    ).fetchall()
    print(f"  supplies edges ({len(tsmc_supplies)}):")
    for (t,) in tsmc_supplies:
        name = cur.execute("SELECT name FROM nodes WHERE id=?", (t,)).fetchone()
        print(f"    -> {t} ({name[0] if name else '?'})")

    print()
    # Freeport edge count
    fmc_count = cur.execute(
        "SELECT COUNT(*) FROM edges WHERE source='cik:0000831259'",
    ).fetchone()[0]
    print(f"Freeport-McMoRan (cik:0000831259) total outgoing edges : {fmc_count}")

    fmc_supplies = cur.execute(
        "SELECT target FROM edges WHERE source='cik:0000831259' AND type='supplies'",
    ).fetchall()
    print(f"  supplies edges ({len(fmc_supplies)}):")
    for (t,) in fmc_supplies:
        name = cur.execute("SELECT name FROM nodes WHERE id=?", (t,)).fetchone()
        print(f"    -> {t} ({name[0] if name else '?'})")


def main():
    print(f"Opening database: {DB_PATH}")
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA foreign_keys = ON")
    cur = conn.cursor()

    try:
        print()
        print("--- Inserting nodes ---")
        inserted_nodes = insert_nodes(cur)

        print()
        print("--- Inserting edges ---")
        inserted_edges = insert_edges(cur)

        conn.commit()
        print_summary(cur, inserted_nodes, inserted_edges)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
