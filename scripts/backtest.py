"""Directional-expectation eval for the "So What?" impact engine.

This is NOT a market-price backtest (see docs/BACKTEST.md). It runs curated news
events through run_impact and scores how often the engine's verdict DIRECTION
matches an expert-labelled expectation — a repeatable correctness/regression
signal with no external dependencies.

Usage:
    python -B scripts/backtest.py --limit 3 --runs 1
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CASES = REPO_ROOT / "scripts" / "backtest_cases.jsonl"
DB = REPO_ROOT / "econgraph.db"

OUTCOMES = ("correct", "opposite", "no_effect", "unscored", "not_in_trace", "not_in_graph")


def classify(expected: str, verdict: Optional[dict]) -> str:
    """expected in {positive,negative}; verdict is the engine's impact dict for the
    resolved node, or None if the node was not in the trace."""
    if verdict is None:
        return "not_in_trace"
    d = verdict.get("direction")
    if d == expected:
        return "correct"
    if d in ("positive", "negative"):
        return "opposite"
    if d == "unscored":
        return "unscored"
    return "no_effect"


def score_case(case: dict, impacts_by_id: dict, resolve: Callable[[str], Optional[str]]) -> dict:
    """resolve(name) -> node_id | None. Returns per-entity outcomes."""
    rows = []
    for exp in case.get("expect", []):
        nid = resolve(exp["entity"])
        outcome = "not_in_graph" if nid is None else classify(exp["direction"], impacts_by_id.get(nid))
        rows.append({"entity": exp["entity"], "expected": exp["direction"],
                     "node_id": nid, "outcome": outcome})
    return {"id": case.get("id"), "headline": case.get("headline"), "rows": rows}


def aggregate(results: list[dict]) -> dict:
    c: Counter = Counter()
    total = 0
    for res in results:
        for row in res["rows"]:
            total += 1
            c[row["outcome"]] += 1
    scorable = total - c["not_in_graph"]
    accuracy = (c["correct"] / scorable) if scorable else 0.0
    return {"total": total, "scorable": scorable, "accuracy": round(accuracy, 3), "counts": dict(c)}


def load_cases(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _majority_impacts(runs_impacts: list[list[dict]]) -> dict:
    """Across K runs, build {node_id: verdict} taking the majority direction
    (ties broken by, and the representative chosen as, the highest magnitude
    among the majority direction). For K=1 this is just the single run."""
    by_id: dict[str, list[dict]] = {}
    for impacts in runs_impacts:
        for v in impacts:
            by_id.setdefault(v["node_id"], []).append(v)
    out = {}
    for nid, vs in by_id.items():
        dirs = Counter(v.get("direction") for v in vs)
        top_dir = dirs.most_common(1)[0][0]
        reps = [v for v in vs if v.get("direction") == top_dir]
        out[nid] = max(reps, key=lambda v: float(v.get("magnitude") or 0.0))
    return out


def resolve_entity_any(conn, name: str) -> Optional[str]:
    """Resolve an entity name to a node id across ALL node types.

    api.impact._resolve_entity is Company-only (it filters type='Company'), so
    commodities/regions never match there. We try it first (it handles filer
    tickers/aliases), then fall back to a general name/alias match across every
    node type — essential since many backtest expectations are commodities."""
    from api.impact import _resolve_entity
    node = _resolve_entity(conn, name)
    if node:
        return node["id"]
    q = (name or "").lower().strip()
    if not q:
        return None
    row = conn.execute("SELECT id FROM nodes WHERE LOWER(name) = ? LIMIT 1", (q,)).fetchone()
    if row:
        return row["id"]
    row = conn.execute(
        "SELECT n.id FROM aliases a JOIN nodes n ON n.id = a.node_id "
        "WHERE a.alias_normalized = ? LIMIT 1",
        (q,),
    ).fetchone()
    if row:
        return row["id"]
    row = conn.execute("SELECT id FROM nodes WHERE LOWER(name) LIKE ? LIMIT 1", (q + "%",)).fetchone()
    if row:
        return row["id"]
    if len(q) >= 5:
        row = conn.execute("SELECT id FROM nodes WHERE LOWER(name) LIKE ? LIMIT 1", (f"%{q}%",)).fetchone()
        if row:
            return row["id"]
    return None


def run_backtest(cases, *, conn, provider, runs, resolve):
    from api.impact import run_impact
    results = []
    for case in cases:
        runs_impacts = []
        for _ in range(max(1, runs)):
            r = run_impact(case["headline"], conn=conn, provider=provider)
            runs_impacts.append(r.get("impacts", []))
        impacts_by_id = _majority_impacts(runs_impacts)
        results.append(score_case(case, impacts_by_id, resolve))
    return results


def format_report(results: list[dict], agg: dict) -> str:
    lines = [
        "# Backtest report — directional-expectation eval",
        "",
        f"**Accuracy: {agg['accuracy'] * 100:.0f}%** "
        f"({agg['counts'].get('correct', 0)}/{agg['scorable']} scorable expectations)",
        "",
        f"Counts: `{agg['counts']}`",
        "",
        "_Not a market-price backtest — see docs/BACKTEST.md._",
        "",
    ]
    gaps = []
    for res in results:
        lines += [f"## {res['headline']}", "", "| entity | expected | engine node | outcome |", "|---|---|---|---|"]
        for row in res["rows"]:
            lines.append(f"| {row['entity']} | {row['expected']} | {row['node_id'] or '—'} | {row['outcome']} |")
            if row["outcome"] == "not_in_graph":
                gaps.append(row["entity"])
        lines.append("")
    if gaps:
        lines += ["## Curation gaps (not_in_graph)", ", ".join(sorted(set(gaps)))]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Directional-expectation eval for the impact engine.")
    ap.add_argument("--cases", default=str(DEFAULT_CASES))
    ap.add_argument("--limit", type=int, default=0, help="only the first N cases (0 = all)")
    ap.add_argument("--runs", type=int, default=1, help="runs per case; >1 majority-votes direction")
    ap.add_argument("--provider", default="claude", choices=["claude", "ollama"])
    ap.add_argument("--out", default=str(REPO_ROOT / "data"))
    args = ap.parse_args()

    if not DB.exists():
        print(f"ERROR: db not found at {DB}; run `python -m pipeline.build_graph` first", file=sys.stderr)
        return 2
    cases = load_cases(Path(args.cases))
    if args.limit > 0:
        cases = cases[:args.limit]
    if not cases:
        print("ERROR: no cases loaded", file=sys.stderr)
        return 2

    from schema.store import connect
    conn = connect(DB)

    def resolve(name: str) -> Optional[str]:
        return resolve_entity_any(conn, name)

    try:
        results = run_backtest(cases, conn=conn, provider=args.provider, runs=args.runs, resolve=resolve)
    finally:
        conn.close()

    agg = aggregate(results)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "backtest_report.md").write_text(format_report(results, agg), encoding="utf-8")
    (out_dir / "backtest_report.json").write_text(
        json.dumps({"aggregate": agg, "results": results}, indent=2), encoding="utf-8")
    print(f"accuracy={agg['accuracy'] * 100:.0f}%  counts={agg['counts']}")
    print(f"report -> {out_dir / 'backtest_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
