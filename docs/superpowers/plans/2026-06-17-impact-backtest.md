# Directional-Expectation Eval Implementation Plan (stream 2.4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Checkbox steps.

**Goal:** An offline harness that runs curated news events through the impact engine and scores how often the engine's verdict direction matches an expert-labelled expectation — a repeatable correctness/regression signal. NOT a market-price backtest.

**Architecture:** `scripts/backtest.py` with pure, unit-testable scoring (`classify`/`score_case`/`aggregate`/`_majority_impacts`) plus a thin I/O shell that runs `run_impact`, resolves expected entities via `api.impact._resolve_entity`, and writes a markdown+JSON report. Curated cases in `scripts/backtest_cases.jsonl`. The engine is unchanged — read-only consumer.

**Tech Stack:** Python/pytest. **Branch:** `feat/impact-backtest`. **Spec:** `docs/superpowers/specs/2026-06-17-impact-backtest-design.md`. Run Python with `python -B`.

---

## Task 1: Harness + fixtures + unit tests

**Files:** Create `scripts/backtest.py`, `scripts/backtest_cases.jsonl`, `tests/test_backtest.py`.

- [ ] **Step 1: Write the failing unit tests** — create `tests/test_backtest.py`:

```python
"""Unit tests for the directional-expectation eval scorer (no DB / no LLM)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import backtest  # noqa: E402


def test_classify_all_branches():
    assert backtest.classify("positive", {"direction": "positive"}) == "correct"
    assert backtest.classify("negative", {"direction": "negative"}) == "correct"
    assert backtest.classify("positive", {"direction": "negative"}) == "opposite"
    assert backtest.classify("positive", {"direction": "no_effect"}) == "no_effect"
    assert backtest.classify("positive", {"direction": "unscored"}) == "unscored"
    assert backtest.classify("positive", None) == "not_in_trace"


def test_score_case_outcomes_and_not_in_graph():
    case = {"id": "c1", "headline": "h", "expect": [
        {"entity": "Alpha", "direction": "positive"},
        {"entity": "Beta", "direction": "negative"},
        {"entity": "Ghost", "direction": "positive"},
    ]}
    impacts_by_id = {
        "n:alpha": {"node_id": "n:alpha", "direction": "positive", "magnitude": 0.8},
        "n:beta": {"node_id": "n:beta", "direction": "positive", "magnitude": 0.5},
    }
    resolve = {"Alpha": "n:alpha", "Beta": "n:beta", "Ghost": None}.get
    res = backtest.score_case(case, impacts_by_id, resolve)
    outs = {r["entity"]: r["outcome"] for r in res["rows"]}
    assert outs == {"Alpha": "correct", "Beta": "opposite", "Ghost": "not_in_graph"}


def test_aggregate_excludes_not_in_graph_from_denominator():
    results = [{"rows": [
        {"outcome": "correct"}, {"outcome": "correct"},
        {"outcome": "opposite"}, {"outcome": "not_in_graph"},
    ]}]
    agg = backtest.aggregate(results)
    assert agg["total"] == 4
    assert agg["scorable"] == 3              # not_in_graph excluded
    assert agg["counts"]["correct"] == 2
    assert agg["accuracy"] == round(2 / 3, 3)


def test_majority_impacts_picks_majority_direction_then_max_mag():
    runs = [
        [{"node_id": "x", "direction": "positive", "magnitude": 0.6}],
        [{"node_id": "x", "direction": "positive", "magnitude": 0.8}],
        [{"node_id": "x", "direction": "negative", "magnitude": 0.9}],
    ]
    out = backtest._majority_impacts(runs)
    assert out["x"]["direction"] == "positive"   # 2 of 3 runs
    assert out["x"]["magnitude"] == 0.8          # highest-mag among the majority


def test_load_cases_round_trip(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text('{"id":"a","headline":"h","expect":[{"entity":"X","direction":"positive"}]}\n\n',
                 encoding="utf-8")
    cases = backtest.load_cases(p)
    assert len(cases) == 1 and cases[0]["id"] == "a"
```

- [ ] **Step 2: Run — fail.** `python -B -m pytest tests/test_backtest.py -v` → `ModuleNotFoundError: backtest`.

- [ ] **Step 3: Create `scripts/backtest.py`** verbatim:

```python
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
    from api.impact import _resolve_entity
    conn = connect(DB)

    def resolve(name: str) -> Optional[str]:
        node = _resolve_entity(conn, name)
        return node["id"] if node else None

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
```

- [ ] **Step 4: Create `scripts/backtest_cases.jsonl`** (one object per line, exactly):

```
{"id": "blacksea-grain", "headline": "Russia suspends Black Sea grain exports", "expect": [{"entity": "Wheat", "direction": "positive"}, {"entity": "Archer-Daniels-Midland", "direction": "positive"}], "note": "grain export halt -> wheat price up; ADM (grain trader) benefits"}
{"id": "opec-cut", "headline": "OPEC+ announces deep crude oil production cuts", "expect": [{"entity": "Crude Oil", "direction": "positive"}, {"entity": "Exxon Mobil", "direction": "positive"}], "note": "output cut -> oil price up; producer Exxon benefits"}
{"id": "opec-surge", "headline": "OPEC+ floods the market with a large crude oil output increase", "expect": [{"entity": "Crude Oil", "direction": "negative"}, {"entity": "Exxon Mobil", "direction": "negative"}], "note": "supply glut -> oil price down; producer Exxon hurt"}
{"id": "hormuz", "headline": "A blockade of the Strait of Hormuz halts oil tanker traffic", "expect": [{"entity": "Crude Oil", "direction": "positive"}, {"entity": "Chevron", "direction": "positive"}], "note": "supply route cut -> oil price up; producer Chevron benefits"}
{"id": "chip-ban", "headline": "The US bars Nvidia from selling advanced AI chips to China", "expect": [{"entity": "Nvidia", "direction": "negative"}], "note": "lost China sales -> Nvidia hurt"}
{"id": "tsmc-expand", "headline": "TSMC announces a record capacity expansion to ease the chip shortage", "expect": [{"entity": "Taiwan Semiconductor", "direction": "positive"}, {"entity": "Nvidia", "direction": "positive"}], "note": "more foundry capacity -> TSMC grows; chip buyer Nvidia benefits"}
{"id": "wheat-bumper", "headline": "A record global wheat harvest floods the market", "expect": [{"entity": "Wheat", "direction": "negative"}, {"entity": "General Mills", "direction": "positive"}], "note": "oversupply -> wheat price down; cereal maker General Mills gains cheaper input"}
{"id": "russia-oil-sanctions", "headline": "New sanctions cut off Russian crude oil exports", "expect": [{"entity": "Crude Oil", "direction": "positive"}, {"entity": "Exxon Mobil", "direction": "positive"}], "note": "supply cut -> oil price up; non-Russian producer Exxon benefits"}
```

- [ ] **Step 5: Run — pass.** `python -B -m pytest tests/test_backtest.py -v` → 5 pass. Confirm `python -B -c "import json,pathlib; [json.loads(l) for l in pathlib.Path('scripts/backtest_cases.jsonl').read_text().splitlines() if l.strip()]"` parses cleanly (no JSON errors in the fixtures).

- [ ] **Step 6: Commit**
```bash
git add scripts/backtest.py scripts/backtest_cases.jsonl tests/test_backtest.py
git commit -m "feat(backtest): directional-expectation eval harness + curated cases"
```

---

## Task 2: Runbook + live smoke run

**Files:** Create `docs/BACKTEST.md`.

- [ ] **Step 1: Write `docs/BACKTEST.md`:**

```markdown
# Directional-Expectation Eval ("backtest")

`scripts/backtest.py` measures how often the "So What?" engine's verdict
**direction** matches an expert-labelled expectation on a curated set of news
events (`scripts/backtest_cases.jsonl`).

## What this is — and isn't
- **Is:** a repeatable directional sanity/regression score. Each case labels a few
  named entities with the direction a domain expert expects (commodity direction =
  price direction, matching the engine). The harness runs the event through
  `run_impact` and counts agreement.
- **Is NOT:** a market-price backtest. It does not check whether the predicted
  winners/losers actually moved in the market. That needs a historical price feed
  and event-date→return matching — a future extension (see below).

## Running it
```bash
python -B scripts/backtest.py            # all cases, claude provider
python -B scripts/backtest.py --limit 3  # first 3 cases (each ~1–3 min LLM run)
python -B scripts/backtest.py --runs 3   # 3 runs/case, majority-vote direction (dampens LLM noise)
python -B scripts/backtest.py --provider ollama
```
Writes `data/backtest_report.md` and `data/backtest_report.json`, and prints the
headline accuracy. Requires `econgraph.db` and a working LLM provider.

## Reading the report
Per expected entity, the outcome is one of: `correct`, `opposite`, `no_effect`,
`unscored`, `not_in_trace` (node exists but the BFS didn't reach it), or
`not_in_graph` (the entity name didn't resolve to a node). **Accuracy =
correct / (total − not_in_graph)** — curation gaps are excluded from the
denominator so a missing node doesn't read as a model error. The report lists
`not_in_graph` entities separately so you can fix labels or add nodes.

## Adding cases
Append one JSON object per line to `scripts/backtest_cases.jsonl`:
`{"id": "...", "headline": "...", "expect": [{"entity": "<name as it appears in the graph>", "direction": "positive|negative"}], "note": "why"}`.
Prefer unambiguous events and entities you can confirm exist (run a search first).

## Future extension: real price backtesting
Replace the expert label with a realized return: for each case, record the event
date, pull each entity's N-day forward return from a price feed, and treat
`sign(return)` as the ground-truth direction. The scorer (`classify`/`score_case`)
is already direction-based, so only the label source changes.
```

- [ ] **Step 2: Live smoke run (one case)** — confirms the resolver + run_impact + report wiring end to end:
```bash
python -B scripts/backtest.py --limit 1
```
Expected: prints an `accuracy=…% counts={…}` line and writes `data/backtest_report.md`. Inspect the report: the case's entities should resolve (node ids shown, not all `not_in_graph`) and outcomes be populated. (If the live LLM is unavailable, note it; the unit tests already prove the scoring logic.)

- [ ] **Step 3: Commit**
```bash
git add docs/BACKTEST.md
git commit -m "docs: backtest runbook (scope, usage, future price-feed extension)"
```

---

## Self-Review

- Harness with pure scorer + orchestration + report + CLI → Task 1 Step 3. ✓
- Curated fixtures, graceful `not_in_graph` → Task 1 Step 4 + `classify`/`score_case`. ✓
- Outcome taxonomy + accuracy excluding `not_in_graph` → `classify`/`aggregate` + tests. ✓
- `--runs` majority voting → `_majority_impacts` + test. ✓
- Unit tests (no DB/LLM) → Task 1 Step 1. ✓
- Runbook with honest scope + future extension → Task 2. ✓
- Live smoke → Task 2 Step 2. ✓
- Engine unchanged (read-only) — no engine edits in any step. ✓
- Placeholder scan: full code/fixtures/doc in every step; no TBD. ✓
- Naming consistency: `classify`/`score_case`/`aggregate`/`_majority_impacts`/`run_backtest`/`format_report`/`load_cases`; outcomes tuple; `--cases/--limit/--runs/--provider/--out` consistent across script, tests, and doc. ✓
```
