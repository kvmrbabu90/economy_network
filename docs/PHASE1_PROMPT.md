# Phase 1 — Schema migration (segments) + Ingestion (Claude Code prompt)

Run from the repo root in a **fresh** Claude Code session. Phase 0 must be complete and green. This phase has two parts: a small schema migration (Part A) that must pass before any ingestion code is written, then the ingestion pipeline (Part B).

**Two decisions already made (override in the prompt if you want them different):**
- **Latest 10-K only** per company for the MVP. A `--years N` flag may exist but defaults to 1 (latest).
- **EDGAR contact comes from an environment variable**, never hardcoded, never committed.

---

```
Read CLAUDE.md and docs/PRD.md fully before doing anything. This is Phase 1. It has two parts. Do PART A completely and get tests green BEFORE starting PART B. Do NOT write any extraction, API, or frontend logic — that is later phases (build-discipline rule). Do NOT populate any Segment nodes or part_of edges in this phase; Part A only adds schema HEADROOM that Phase 2 will use.

=== PART A — Schema migration: segment support (headroom only) ===

Rationale: some S&P 500 filers wrap multiple distinct businesses (e.g. Berkshire → BNSF, GEICO; Alphabet → Google Services, Cloud). We add the schema to model these now so it isn't a painful retrofit, but we populate it for ZERO companies in Phase 1.

1. In schema/models.py:
   - Add `Segment` to the NodeType enum.
   - Add `part_of` to the EdgeType enum.
   - Add a validator on Edge: if type == part_of, then (a) the source node id must be a Segment (we can't check node type from the edge alone, so validate by convention: source id starts with "seg:") and (b) the target id must start with "cik:" (a Company filer). Document the "seg:" id convention in a comment.
   - Add "seg:" to the set of accepted canonical-ID prefixes in the Node id validator. A Segment node id looks like: seg:cik0000080424:google-cloud (parent CIK + slug). Document this format.

2. In tests/test_schema.py add tests:
   - A Segment node with a well-formed "seg:" id round-trips through SQLite.
   - A part_of edge from a seg: source to a cik: target validates.
   - A part_of edge whose target is NOT a cik: id raises a validation error.
   - Re-confirm the existing Phase 0 tests still pass (customer_of still absent from EdgeType, etc.).

3. Run pytest. All tests (old + new) must be green before you continue. Print the output.

Acceptance gate for Part A: pytest green, Segment + part_of usable, part_of target validation enforced. Then proceed to Part B.

=== PART B — Ingestion pipeline (pipeline/ingest.py) ===

Goal: produce (1) a roster of S&P 500 Company nodes as validated records, and (2) cached latest-10-K filings, filtered to one sector. NO graph edges yet (resolution/extraction are later phases).

Requirements:

A. CONTACT / USER-AGENT (SEC requirement)
   - Read the EDGAR contact from the environment variable EDGAR_USER_AGENT (e.g. "Jane Doe jane@example.com").
   - If it is missing or empty, FAIL LOUDLY with a clear message — do not fall back to a placeholder. SEC requires a real contact.
   - Send this exact string as the User-Agent header on every request to sec.gov.

B. RATE LIMITING
   - Implement a single GLOBAL rate limiter capped at <=10 requests/second that applies across BOTH data.sec.gov and www.sec.gov (the limit is per-source-IP across all sec.gov hosts, not per-host). All SEC requests in the process go through it.

C. S&P 500 ROSTER (from Wikipedia)
   - Fetch "List of S&P 500 companies" from Wikipedia.
   - Save the raw HTML to data/sources/sp500_wikipedia_<YYYYMMDD>.html for reproducibility before parsing.
   - Parse the constituents table by COLUMN HEADER NAME, never by fixed column index. Expected columns include Symbol, Security, GICS Sector, GICS Sub-Industry, CIK. If any expected column is missing, raise a clear error rather than guessing.
   - Capture: ticker(s), company name, GICS sector, GICS sub-industry, CIK.

D. SHARE-CLASS / DUPLICATE DEDUPE (important)
   - Some entities appear as multiple rows / tickers but are ONE company (e.g. GOOGL + GOOG; BRK.A + BRK.B). Dedupe on CIK: collapse multiple rows sharing a CIK into a single Company node, accumulating all tickers into the node's `tickers` list. The node id is cik:<10-digit-zero-padded-CIK>.
   - Zero-pad every CIK to 10 digits consistently so ids match the cik:0000080424 canonical format.

E. SECTOR FILTER
   - Support --sector "Consumer Staples" to restrict the run to one GICS sector. Default behavior if omitted: process all (but for the MVP we will run with the sector filter).

F. 10-K FETCH + CACHE
   - For each company, use the EDGAR submissions JSON API (https://data.sec.gov/submissions/CIK##########.json, CIK zero-padded to 10 digits) to locate the most recent 10-K filing. You may use the `edgartools` library for this if it's cleaner; either approach is fine as long as it goes through the global rate limiter and User-Agent.
   - Default: fetch the LATEST 10-K only. Provide a --years N flag (default 1) for future use, but do not fetch multiple years unless asked.
   - Cache each filing's primary document to data/filings/<cik>/<accession>.txt (or .htm as fetched). Per invariant #6: if the cached file already exists, DO NOT re-fetch.
   - Record the accession number and the filing URL alongside the company (these become edge provenance later).

G. OUTPUT
   - Emit data/companies.jsonl: one line per company, each a Company Node record VALIDATED through the Pydantic Node model from schema/models.py. Populate id, type=Company, name, aliases (start with the registered name + any obvious variants), tickers (all share classes), identifiers.cik, sector, industry (use GICS sub-industry), country="US", and stash the latest 10-K accession + url under metadata.
   - Do NOT write any edges, Segment nodes, or part_of edges in this phase.
   - Log a summary: companies discovered, deduped count, filings fetched vs. served-from-cache, any companies where a 10-K could not be located.

H. ROBUSTNESS
   - Cache-first everywhere; the pipeline must be safely re-runnable without re-hitting SEC for already-fetched filings.
   - Handle companies with no locatable 10-K gracefully (log and skip; still emit the company node, with metadata noting no filing found).

Acceptance test (must pass before you stop):
   - Running `python -m pipeline.ingest --sector "Consumer Staples"` produces ~30-40 cached 10-Ks under data/filings/ and a data/companies.jsonl whose every line validates as a Company Node with a non-null CIK and GICS sector/industry.
   - Share-class dedupe is demonstrated: confirm that any consumer-staples entity with multiple share classes appears as ONE node with multiple tickers (note: if none exist in Consumer Staples, instead add a unit test that feeds two mock rows sharing a CIK and asserts they collapse to one node with both tickers).
   - A second run fetches ZERO new filings (all served from cache).
   - No Segment nodes, no edges of any type were written.
   - Print the run summary and the test output, then STOP and summarize what Phase 2 (extraction) will consume.
```

---

## Notes for you (not part of the prompt)

- Set the env var before the session, e.g. PowerShell: `$env:EDGAR_USER_AGENT = "Your Name your@email.com"`. Keep it out of the repo; the prompt makes a missing value fail loudly so you can't forget.
- The Wikipedia table currently carries CIKs directly, so resolution is mostly free — the EDGAR submissions API is then used to find the actual 10-K, not to look up the CIK. If Wikipedia ever drops the CIK column, the header-keyed parser will raise rather than silently misalign, which is the behavior you want.
- Segments stay empty here by design. Phase 2 will decide, per company, whether to mint `seg:` nodes — gated on the 10-K's reported operating-segment disclosure and likely restricted to an explicit conglomerate list at first (same "material only" discipline as everything else).
- When this gate is green, the Phase 2 extraction prompt is the next one to write — it's the hard phase (free text → typed edges via the Claude API), and it's where the segment-decomposition threshold rule gets implemented.
