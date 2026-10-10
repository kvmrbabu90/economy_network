"""Signal-reliability pipeline orchestrator: prices -> outcomes -> reliability.

Pure data + math — uses NO Claude CLI / LLM, so it never touches the LLM quota and is safe to
schedule daily after market close. Each stage is restartable and reads the previous stage's
durable output (CLAUDE.md #5): outcomes re-computes only newly-mature signals, reliability is a
full recompute from the mature outcome set.

    python -B -m pipeline.run_validation           # against ECONGRAPH_DB / default
    python -B -m pipeline.run_validation --db C:/Users/Public/econgraph/econgraph.db
"""
from __future__ import annotations

import argparse
import logging
import time

from schema import store
from pipeline import signal_outcomes, intraday_outcomes, reliability

log = logging.getLogger(__name__)


def run_validation(conn) -> dict:
    t0 = time.monotonic()
    store.init_db(conn)   # idempotent: ensures the price/outcome/reliability schema + migrations
    outcomes = signal_outcomes.compute_outcomes(conn)
    conn.commit()
    intraday = intraday_outcomes.compute_intraday_outcomes(conn)   # hourly-entry model (needs published_ts)
    conn.commit()
    scorecard = reliability.compute_reliability(conn)   # embeds both daily + intraday rollups
    conn.commit()
    took = round(time.monotonic() - t0, 1)
    cov = scorecard["coverage"]
    log.info("validation done in %ss: %d mature outcome rows across %d events / %d nodes",
             took, cov["outcome_rows"], cov["distinct_events"], cov["distinct_nodes"])
    return {"outcomes": outcomes, "coverage": cov, "took_s": took}


def main() -> None:
    ap = argparse.ArgumentParser(description="Compute signal-reliability scorecard (no LLM).")
    ap.add_argument("--db", default=None, help="SQLite path (default: ECONGRAPH_DB / canonical)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = store.connect(args.db)
    try:
        result = run_validation(conn)
        print(f"[validation] {result['coverage']}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
