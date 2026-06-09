"""Phase K-4: Sync financial weights from edges.jsonl into the live SQLite DB.

After extract_weights.py updates edges.jsonl with weight and source_tier,
this script does targeted UPDATE statements on the existing econgraph.db
without needing to drop/recreate it (which would require killing the API server).

Only touches rows where weight or source_tier changed — safe to run while
the API is serving requests.

Usage:
    python -m pipeline.sync_weights_to_db [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = REPO_ROOT / "data"


def main(dry_run: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                        stream=sys.stdout)

    edges_path = DATA_DIR / "edges.jsonl"
    if not edges_path.exists():
        log.error("edges.jsonl not found at %s", edges_path)
        sys.exit(1)

    db_path = REPO_ROOT / "econgraph.db"
    if not db_path.exists():
        log.error("econgraph.db not found at %s", db_path)
        sys.exit(1)

    # Read all edges that have weight or source_tier set
    to_sync: list[dict] = []
    with edges_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            if e.get("weight") is not None or e.get("source_tier") is not None:
                to_sync.append(e)

    log.info("Edges to sync: %d (weight set: %d, source_tier set: %d)",
             len(to_sync),
             sum(1 for e in to_sync if e.get("weight") is not None),
             sum(1 for e in to_sync if e.get("source_tier") is not None))

    if not to_sync:
        log.info("Nothing to sync.")
        return

    if dry_run:
        for e in to_sync[:10]:
            log.info("[DRY] Would update %s: weight=%s tier=%s",
                     e.get("id"), e.get("weight"), e.get("source_tier"))
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # reduce locking conflicts

    n_updated = 0
    n_missing = 0
    try:
        for e in to_sync:
            edge_id = e.get("id")
            if not edge_id:
                continue
            weight = e.get("weight")
            source_tier = e.get("source_tier")
            confidence = e.get("confidence")
            # Check if this edge exists in the DB
            row = conn.execute("SELECT id, weight, source_tier FROM edges WHERE id = ?",
                               (edge_id,)).fetchone()
            if row is None:
                log.debug("Edge not in DB: %s", edge_id)
                n_missing += 1
                continue
            # Only update if values changed
            db_weight = row["weight"]
            db_tier = row["source_tier"]
            if db_weight == weight and db_tier == source_tier:
                continue  # already in sync
            conn.execute(
                "UPDATE edges SET weight=?, source_tier=?, confidence=? WHERE id=?",
                (weight, source_tier, confidence, edge_id)
            )
            n_updated += 1
            if n_updated % 1000 == 0:
                conn.commit()
                log.info("  ...%d rows updated", n_updated)

        conn.commit()
        log.info("Sync complete: %d rows updated, %d edges not found in DB",
                 n_updated, n_missing)

    except Exception as exc:
        log.error("Sync failed: %s", exc)
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync edge weights from edges.jsonl to SQLite")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be updated without writing")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
