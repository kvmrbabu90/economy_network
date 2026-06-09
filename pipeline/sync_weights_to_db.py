"""Phase K-4: Sync financial weights from edges.jsonl into the live SQLite DB.

After extract_weights.py updates edges.jsonl with weight and source_tier,
this script does targeted UPDATE + INSERT statements on the existing econgraph.db
without needing to drop/recreate it (which would require killing the API server).

Two operations:
  1. UPDATE existing edges where weight or source_tier changed.
  2. INSERT new edges that have source_tier='sec_explicit' and do not yet
     exist in the DB — these are created by extract_weights.py when a
     named-customer concentration is found but no graph edge existed.

Safe to run while the API is serving requests (WAL mode + no DROP/CREATE).

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
            log.info("[DRY] Would sync %s: weight=%s tier=%s",
                     e.get("id"), e.get("weight"), e.get("source_tier"))
        return

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # reduce locking conflicts

    # Pre-load all existing edge IDs into a set for O(1) membership checks.
    existing_ids: set[str] = {
        row[0] for row in conn.execute("SELECT id FROM edges")
    }
    log.info("Edges already in DB: %d", len(existing_ids))

    n_updated = 0
    n_inserted = 0
    n_skipped = 0
    try:
        for e in to_sync:
            edge_id = e.get("id")
            if not edge_id:
                continue
            weight = e.get("weight")
            source_tier = e.get("source_tier")
            confidence = e.get("confidence")

            if edge_id in existing_ids:
                # UPDATE path: only touch rows where values changed.
                row = conn.execute(
                    "SELECT weight, source_tier FROM edges WHERE id = ?",
                    (edge_id,)
                ).fetchone()
                if row is None:
                    continue  # shouldn't happen, but be safe
                if row["weight"] == weight and row["source_tier"] == source_tier:
                    n_skipped += 1
                    continue  # already in sync
                conn.execute(
                    "UPDATE edges SET weight=?, source_tier=?, confidence=? WHERE id=?",
                    (weight, source_tier, confidence, edge_id)
                )
                n_updated += 1

            else:
                # INSERT path: only for new edges created by extract_weights.py
                # (identified by source_tier='sec_explicit' — high-confidence
                # named-customer disclosures that deserve to be in the core graph).
                if source_tier != "sec_explicit":
                    log.debug("Skipping insert for non-sec_explicit edge %s", edge_id)
                    n_skipped += 1
                    continue

                prov = e.get("provenance", {})
                additional = json.dumps(e.get("additional_provenance", []),
                                        ensure_ascii=False)
                directed_int = 1 if e.get("directed", True) else 0
                below_threshold = 1 if e.get("below_threshold", False) else 0

                conn.execute(
                    """INSERT INTO edges
                       (id, source, target, type, directed, confidence, weight,
                        prov_filing, prov_url, prov_snippet, prov_extracted_by,
                        additional_provenance, below_threshold,
                        supply_geography, source_tier)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        edge_id,
                        e["source"],
                        e["target"],
                        e.get("type", "supplies"),
                        directed_int,
                        confidence or 0.90,
                        weight,
                        prov.get("filing", ""),
                        prov.get("url", ""),
                        prov.get("snippet", ""),
                        prov.get("extracted_by", "llm:claude-cli"),
                        additional,
                        below_threshold,
                        e.get("supply_geography"),
                        source_tier,
                    )
                )
                n_inserted += 1
                log.info("Inserted new edge: %s -> %s (weight=%.2f)",
                         e["source"], e["target"], weight or 0)

            if (n_updated + n_inserted) % 1000 == 0 and (n_updated + n_inserted) > 0:
                conn.commit()
                log.info("  ...%d updated, %d inserted so far",
                         n_updated, n_inserted)

        conn.commit()
        log.info(
            "Sync complete: %d updated, %d inserted, %d already-in-sync",
            n_updated, n_inserted, n_skipped,
        )

    except Exception as exc:
        log.error("Sync failed: %s", exc)
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Sync edge weights from edges.jsonl to SQLite (UPDATE + INSERT)"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be changed without writing")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
