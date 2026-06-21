# "So What?" Directional-Expectation Eval — Design Spec

**Date:** 2026-06-17
**Status:** Approved autonomously (user delegated 2.2–2.4 AFK)
**Work stream:** 2 of 4 (Correctness & trust), sub-project 4 of 4
**Branch:** `feat/impact-backtest` (stacked on `feat/impact-uncertainty`)

---

## Context

Streams 2.1–2.3 made the engine more honest about *what it couldn't do* and
removed unsupported verdicts. This sub-project measures whether what it *does*
output is actually right — the "validate against reality" goal. It adds an
offline eval harness: a curated set of news events, each labelled with the
direction a domain expert would expect for a handful of named entities, and a
runner that scores how often the engine agrees.

### Honest scope (key autonomous decision)
A *true* market backtest (did the predicted winners/losers actually move in the
market after the event?) needs a historical price feed (e.g. a paid/3rd-party
API) and event-date→price-window matching — out of scope for this free-data
project. Instead this is a **directional-expectation eval**: the labels encode
*expert-expected directions* for unambiguous events, and the harness measures the
engine's agreement. That is a genuine, repeatable correctness signal (a
regression guard + a sanity score) without external dependencies. True
price-based backtesting is documented as a future extension.

### Other autonomous decisions
- **Harness is the deliverable; the accuracy number is the user's to generate.**
  The engine is LLM-driven (slow, nondeterministic, costs Max-plan credits), so a
  full live run is the user's call. We build + unit-test the harness with a mocked
  engine, ship a starter case set, and document how to run it live (`--limit`,
  `--runs`).
- **Pure scorer, injected resolver** — the scoring logic is a pure function over
  (expectations, impacts, resolve_fn) so it's unit-tested with no DB/LLM. I/O
  (running the engine, writing the report) is separate.
- **Outcome taxonomy** per expected entity: `correct` (engine direction == expected),
  `opposite` (engine gave the other definite direction), `no_effect`, `unscored`,
  `not_in_trace` (resolved to a node but it wasn't in the impact set),
  `not_in_graph` (name didn't resolve). Accuracy = `correct / total_expectations`.
  `not_in_graph` is reported separately so a curation gap doesn't masquerade as a
  model error.
- **Commodity direction = price direction**, matching the engine's own semantics
  (a supply shock → commodity `positive` = price up). Labels follow that.
- **Fixtures are illustrative starter labels**, anchored on entities known to exist
  (Wheat, Crude Oil, and large-cap companies) with clear economic logic; the user
  is expected to review/expand them. Unresolved entities degrade gracefully to
  `not_in_graph` rather than crashing.

### Non-goals
- Live market-price comparison / a price-feed integration.
- Any change to the engine itself (1, 2.1–2.3). This sub-project only *reads* the
  engine's output.
- A pass/fail CI gate on accuracy (the score is informational; thresholds are the
  user's call once they've curated a trusted case set).

---

## Section 1 — Fixtures (`scripts/backtest_cases.jsonl`)

`data/` is gitignored, so the curated cases live under `scripts/` (tracked). One
JSON object per line:

```jsonc
{"id": "blacksea-grain", "headline": "Russia suspends Black Sea grain exports",
 "expect": [{"entity": "Wheat", "direction": "positive"},
            {"entity": "Archer-Daniels-Midland", "direction": "positive"}],
 "note": "grain export halt → wheat price up; ADM (grain trader) benefits"}
```

~8–10 cases, each with 2–3 expectations on entities with clear directional logic
(grain/oil supply shocks, export bans, capacity changes). Direction is
`positive`|`negative` only (the things we can label confidently). `note` documents
the reasoning so a reviewer can sanity-check or correct a label.

---

## Section 2 — Harness (`scripts/backtest.py`)

Pure, testable core + thin I/O shell:

```python
OUTCOMES = ("correct", "opposite", "no_effect", "unscored", "not_in_trace", "not_in_graph")

def classify(expected: str, verdict: Optional[dict]) -> str:
    """expected in {positive,negative}; verdict is the engine's impact dict for the
    resolved node, or None if the node wasn't in the trace."""
    if verdict is None:
        return "not_in_trace"
    d = verdict.get("direction")
    if d == expected:            return "correct"
    if d in ("positive", "negative"):  return "opposite"
    if d == "unscored":          return "unscored"
    return "no_effect"

def score_case(case: dict, impacts_by_id: dict[str, dict], resolve) -> dict:
    """resolve(name) -> node_id | None. Returns per-entity outcomes + counts."""
    rows = []
    for exp in case["expect"]:
        nid = resolve(exp["entity"])
        if nid is None:
            outcome = "not_in_graph"
        else:
            outcome = classify(exp["direction"], impacts_by_id.get(nid))
        rows.append({"entity": exp["entity"], "expected": exp["direction"],
                     "node_id": nid, "outcome": outcome})
    return {"id": case["id"], "headline": case["headline"], "rows": rows}
```

Orchestration (`run_backtest`): for each case, run `run_impact(headline, conn=…,
provider=…)` (×`runs`; when `runs>1`, take the majority `direction` per node so
LLM noise is dampened), build `impacts_by_id = {v["node_id"]: v}`, resolve expected
entities via a closure over `api.impact._resolve_entity(conn, name)`, call
`score_case`. Aggregate: total expectations, `correct`, accuracy =
`correct/(total − not_in_graph)` (exclude curation gaps from the denominator) AND a
raw accuracy over all. Write `data/backtest_report.md` + `data/backtest_report.json`
and print a summary table.

CLI (argparse): `--cases <path>` (default `scripts/backtest_cases.jsonl`),
`--limit N`, `--runs K` (default 1), `--provider claude|ollama`, `--out <dir>`
(default `data/`). Skips gracefully + clear error if `econgraph.db` is absent.

Report (markdown): a header with overall accuracy + counts, then per-case a small
table (entity | expected | engine direction+magnitude | outcome), and a trailing
"not_in_graph" list flagging curation gaps.

---

## Section 3 — Testing (`tests/test_backtest.py`)

Deterministic, no DB/LLM — imports the pure functions from `scripts/backtest.py`:
- `classify`: each branch — correct, opposite, no_effect, unscored, not_in_trace.
- `score_case`: a case with 3 expectations against a hand-built `impacts_by_id` and a
  dict-backed `resolve`; assert per-entity outcomes and that a name returning `None`
  from `resolve` yields `not_in_graph`.
- An aggregate helper test: given several scored cases, the accuracy math
  (correct / (total − not_in_graph)) is correct, and `not_in_graph` is excluded
  from the denominator.

(`scripts/` is importable in tests via the existing `REPO_ROOT` path or a direct
module load; mirror how other tests import. If `scripts` isn't a package, the test
adds `REPO_ROOT` to `sys.path` and `import backtest`.)

---

## Section 4 — Runbook (`docs/BACKTEST.md`)

A short doc: what the harness measures (and explicitly what it does NOT — it's not a
market-price backtest), how to add cases to `scripts/backtest_cases.jsonl`, how to
run it (`python -B scripts/backtest.py --limit 3`), how to read the report, the
`--runs K` noise-dampening note, and the future-extension note (wire a price feed
to turn expected-direction labels into realized-return checks).

---

## Section 5 — Verification

- Unit tests (Section 3) pass — the scoring logic is correct.
- A live smoke run of ONE case (`python -B scripts/backtest.py --limit 1`) against
  the real engine produces a well-formed report and a sane outcome — confirms the
  end-to-end wiring (resolver + run_impact + report). (One case ≈ one ~3-min
  Max-plan trace; bounded.)

---

## Files touched

| File | Change |
|---|---|
| `scripts/backtest_cases.jsonl` | NEW — curated directional-expectation cases |
| `scripts/backtest.py` | NEW — pure scorer + orchestration + report writer + CLI |
| `tests/test_backtest.py` | NEW — classify/score_case/aggregate unit tests |
| `docs/BACKTEST.md` | NEW — runbook + scope/limitations |

No changes to the engine, schema, DB, or frontend. Read-only consumer of
`run_impact`. Report output lands in gitignored `data/`.
