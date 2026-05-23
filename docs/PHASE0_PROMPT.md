# Phase 0 — Scaffold & Schema (Claude Code prompt)

Copy everything in the block below into a fresh Claude Code session, run from an empty repo directory that already contains `CLAUDE.md` and `docs/PRD.md`. Stop when the acceptance test passes; do not proceed to Phase 1 in the same session.

---

```
Read CLAUDE.md and docs/PRD.md fully before doing anything. This is Phase 0: scaffold and schema only. Do NOT write ingestion, extraction, API, or frontend logic — that is later phases. Build ahead and you violate the build-discipline rule.

Tasks:

1. Scaffold this exact repo layout:
   econgraph/
   ├── pyproject.toml            # Python 3.11+, deps: pydantic, pytest (only what Phase 0 needs)
   ├── config/
   │   └── regulators.yaml       # leave as a placeholder comment; I will supply it
   ├── data/                     # create with a .gitkeep; add data/ to .gitignore
   ├── pipeline/                 # empty package (__init__.py) for now
   ├── api/                      # empty package for now
   ├── web/                      # empty dir with a .gitkeep for now
   ├── schema/
   │   ├── __init__.py
   │   ├── models.py             # Pydantic Node + Edge models
   │   └── store.py              # SQLite schema + create/connect helpers
   └── tests/
       └── test_schema.py

2. In schema/models.py implement Pydantic v2 models matching PRD §4 EXACTLY:
   - NodeType enum: Company | Commodity | Material | Region | Regulator
   - EdgeType enum: supplies | competes_with | regulated_by   (NOTE: no customer_of — it is derived, per invariant #2)
   - Node: id, type, name, aliases (list), tickers (list), identifiers (dict), sector, industry, country, metadata (dict). id must match the canonical-ID format in CLAUDE.md (validate the prefix).
   - Provenance: filing, url, snippet, extracted_by (Literal["llm","rule","manual"]).
   - Edge: id (uuid default), source, target, type (EdgeType), directed (bool), confidence (float 0–1), provenance (Provenance), weight (Optional[float]).
   - Add a validator on Edge: if type == regulated_by, target id must start with "regulator:".

3. In schema/store.py define a SQLite schema with three tables — nodes, edges, aliases — plus connect()/init_db() helpers and functions to upsert a Node and an Edge (validating through the Pydantic models first). aliases maps alias_string -> canonical node id. Do NOT add a customer_of edge type or a reverse-edge table.

4. In tests/test_schema.py write pytest tests that:
   - Construct the P&G node and the Costco node from the PRD §4 examples and round-trip them through SQLite (upsert then read back equal).
   - Construct the P&G --supplies--> Costco edge and the P&G --competes_with--> Kimberly-Clark edge and round-trip them.
   - Assert a regulated_by edge with a non-"regulator:" target raises a validation error.
   - Assert an Edge cannot be created without provenance.

5. Write a short README section documenting how to run the tests.

Acceptance test (must pass before you stop): `pytest` is green, the P&G/Costco round-trip works, and the regulated_by target validation rejects a bad target. Print the final test output. Then STOP and summarize what you built and what Phase 1 will need.
```

---

## After this passes

- Drop your `regulators.yaml` into `config/` (it's a separate file in this handoff).
- Start Phase 1 in a **new** session with the Phase 1 prompt from PRD §9.
- If `claude doctor` flags anything (Node version, auth, MCP), fix it before Phase 1 — ingestion is the first network-heavy phase.
