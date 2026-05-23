# EconGraph

A single, queryable, directed, typed graph of the economy. Nodes are economic
entities (companies, commodities, materials, regions, regulators); edges are
typed and directed (`supplies`, `competes_with`, `regulated_by`). The
"customer of" view is derived at query time by reversing `supplies` — never
stored — so every entity has exactly one canonical node.

See [`docs/PRD.md`](docs/PRD.md) for the full product spec and
[`CLAUDE.md`](CLAUDE.md) for the hard invariants.

## Current phase

**Phase 0 — scaffold & schema.** Repo layout, Pydantic Node/Edge models,
SQLite source-of-truth, round-trip tests. No ingestion, extraction, API, or
frontend logic yet (those come in later phases per
[`docs/PRD.md §9`](docs/PRD.md)).

## Layout

```
econgraph/
├── CLAUDE.md             # project rules for Claude Code (invariants)
├── docs/PRD.md           # full product requirements
├── config/regulators.yaml # industry -> US regulator mapping
├── schema/
│   ├── models.py         # Pydantic Node / Edge / Provenance
│   └── store.py          # SQLite schema + upsert helpers
├── pipeline/             # stages 1-4 (later phases)
├── api/                  # FastAPI (Phase 5)
├── web/                  # Vite + TS + Sigma.js (Phase 6)
├── data/                 # gitignored: cached filings, jsonl artifacts, *.db
└── tests/test_schema.py  # Phase 0 acceptance tests
```

## Running the tests

Requires Python 3.11+.

```sh
# from the repo root
python -m pip install -e ".[dev]"   # or: pip install pydantic pytest
python -m pytest
```

The Phase 0 acceptance criterion: `pytest` is green, the P&G/Costco node and
edge round-trips work, and a `regulated_by` edge with a non-`regulator:`
target is rejected at validation time.

## Conventions (recap from CLAUDE.md)

- **One row per entity.** Never duplicate a node for a different vantage point.
- **`customer_of` is derived, never stored.** Reverse `supplies` at query time.
- **Every edge carries provenance** (filing + URL + snippet + `extracted_by`).
- **Canonical IDs**: `cik:0000080424`, `wikidata:Qxxxx`, `slug:crude-oil`,
  `regulator:<slug>`.
- **Layered, restartable stages.** Each pipeline stage reads the previous
  stage's files from `data/` and writes its own.
