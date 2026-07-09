"""One-time deterministic cleanup (no LLM): remove region-mediated 'market-drift'
impacts already sitting in the live map, matching the going-forward behavior of
IMPACT_SUPPRESS_REGION_EXPANSION.

A region-sink is a Company whose ONLY above-threshold neighbors are Region/Regulator
aggregates — so any hop>=1 impact on it arrived purely via region fan-out (generic
market ripple, e.g. a broadcaster 'impacted' by a semiconductor deal). We delete
those hop>=1 rows (keeping any hop-0 DIRECT seed impact) and re-aggregate node_impact
from the cleaned event_impacts. Reversible: the next cycle re-traces cleanly.

    python -B scripts/purge_region_sink_impacts.py
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from schema import store                                  # noqa: E402
from pipeline.aggregate_impacts import aggregate          # noqa: E402

_AGG = {"Region", "Regulator"}


def region_sinks(conn) -> list[str]:
    types = dict(conn.execute("SELECT id, type FROM nodes").fetchall())
    out = []
    for nid, t in types.items():
        if t != "Company":
            continue
        nbrs = [r[0] for r in conn.execute(
            "SELECT target FROM edges WHERE source=? AND below_threshold=0 "
            "UNION SELECT source FROM edges WHERE target=? AND below_threshold=0", (nid, nid))]
        if nbrs and all(types.get(o) in _AGG for o in nbrs):
            out.append(nid)
    return out


def main() -> int:
    conn = store.connect(store.default_db_path())
    sinks = region_sinks(conn)
    if not sinks:
        print("no region-sinks found")
        return 0
    ph = ",".join("?" * len(sinks))
    before = conn.execute(
        f"SELECT COUNT(*) FROM event_impacts WHERE node_id IN ({ph}) AND hop >= 1", sinks).fetchone()[0]
    ni_before = conn.execute(
        f"SELECT COUNT(*) FROM node_impact WHERE node_id IN ({ph})", sinks).fetchone()[0]
    conn.execute(f"DELETE FROM event_impacts WHERE node_id IN ({ph}) AND hop >= 1", sinks)
    conn.commit()
    print(f"region-sinks: {len(sinks)}  |  deleted {before} region-mediated (hop>=1) event_impacts rows"
          f"  |  {ni_before} of them had a combined node_impact")
    summary = aggregate(conn)          # full rebuild of node_impact from cleaned event_impacts
    print(f"re-aggregated node_impact: {summary}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
