"""So What? V2 · Phase 4 — full-cycle orchestrator.

Runs the ingest -> precompute -> aggregate pipeline once, against one DB, with
per-stage error isolation. Idempotent + restartable. Schedule this every 12h.

    python -B -m pipeline.run_cycle
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Callable, Optional

from pipeline import ingest_news, precompute_impacts, aggregate_impacts
from schema import store

log = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "econgraph.db"


def _run_ingest(db_path) -> dict:
    return ingest_news.run_ingest(db_path=db_path)          # P1 entrypoint: run_ingest(db_path=DB_PATH)


def _run_precompute(db_path, provider) -> dict:
    return precompute_impacts.run_precompute(db_path, provider=provider)


def _run_aggregate(db_path) -> dict:
    conn = store.connect(db_path); store.init_db(conn)
    try:
        return aggregate_impacts.aggregate(conn)
    finally:
        conn.close()


def _stage(name: str, fn: Callable[[], dict], summary: dict) -> None:
    try:
        summary[name] = fn()
    except Exception as exc:                                # isolate: log, record, continue
        log.exception("run_cycle: stage %s failed", name)
        summary[name] = {"error": repr(exc)}
        summary["ok"] = False


def run_cycle(db_path=DB_PATH, *, provider: Optional[str] = None) -> dict:
    t0 = time.time()
    summary: dict = {"ok": True}
    _stage("ingest", lambda: _run_ingest(db_path), summary)
    _stage("precompute", lambda: _run_precompute(db_path, provider), summary)
    _stage("aggregate", lambda: _run_aggregate(db_path), summary)
    summary["elapsed_s"] = round(time.time() - t0, 1)
    log.info("run_cycle: %s", summary)
    return summary


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    s = run_cycle()
    print(f"cycle: {s}")
    return 0 if s["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
