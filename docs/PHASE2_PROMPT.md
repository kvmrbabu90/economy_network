# Phase 2 — Extraction (Claude Code prompt)

The hard phase: cached 10-K text → typed, grounded candidate edges. Run from the repo root in a **fresh** Claude Code session. Phases 0 and 1 must be green. Two gated parts: Part A is deterministic rule work (no model calls); Part B is LLM extraction + the verify gate. Get Part A green before spending any model calls on Part B.

**Critical correction to carry in (the build summary got this wrong):** extraction does **NOT** use `ANTHROPIC_API_KEY` or the metered API. It uses **`claude -p` headless on the Max subscription** — no API key, $0 over plan. The extractor is a *configurable command* so local Gemma can be swapped in as a fallback. If you find yourself instantiating an Anthropic API client, stop — that's the wrong path.

**Two integration gotchas this phase must handle:**
1. **GICS level mismatch.** Phase 1 stored each company's **GICS Sub-Industry** in the `industry` field (that's what Wikipedia ships). But `config/regulators.yaml` is keyed on **GICS Industry** names ("Food Products", "Beverages", …). A direct string match will mostly miss. You must reconcile the levels (sub-industry → industry rollup) before the `regulated_by` join.
2. **Candidate edges, not final Edges.** Phase 2 cannot emit validated `Edge` records, because target names aren't resolved to canonical IDs yet (that's Phase 3). It emits a **CandidateEdge** shape with the target as a raw string.

---

```
Read CLAUDE.md, docs/PRD.md, and the Phase 2 prompt notes fully before doing anything. This is Phase 2 (extraction). Do PART A and get it green BEFORE writing any LLM-calling code in PART B. Do NOT do resolution/canonicalization of target names (that is Phase 3), do NOT build API or frontend, and do NOT use ANTHROPIC_API_KEY or the Anthropic API — extraction goes through `claude -p` headless on the local Max subscription.

Inputs from prior phases: data/companies.jsonl (validated Company Nodes) and data/filings/<cik>/<accession>.htm (cached 10-Ks). Output of this phase: data/edges_raw.jsonl (CandidateEdge records). No resolution.

=== Define the CandidateEdge shape (schema/models.py) ===
Add a Pydantic CandidateEdge model (separate from Edge — do not weaken Edge):
- source_id: str            # the FILING company's canonical id, known now (cik:...)
- target_raw: str           # competitor/customer name AS EXTRACTED, or a canonical regulator: id for regulated_by
- type: EdgeType            # supplies | competes_with | regulated_by  (never customer_of)
- confidence: float (0–1)
- provenance: Provenance    # filing accession, url, verbatim snippet, extracted_by
- verified: bool            # did the snippet pass the grounding check (Part B). rule-generated edges = True.
Add a couple of round-trip tests. Existing schema tests must stay green.

=== PART A — Deterministic extraction (no model calls) ===

A1. HTML → text. Build pipeline/sections.py: load a cached .htm filing, strip to clean text (BeautifulSoup get_text or similar), normalize whitespace. Cache the plain text to data/filings/<cik>/<accession>.txt so Part B and the verify step read the same normalized text.

A2. Section extraction (rule-based, tolerant). From each filing's text, locate and extract:
   - the Item 1 "Business" → "Competition" discussion, and
   - the Item 7 MD&A / customer-concentration discussion (passages naming customers, often "% of net sales/revenue").
   Use regex/heuristic anchors; tolerate filings where a section can't be found (log it, skip that section, continue). Output a per-filing record of which sections were found + their text spans.

A3. GICS reconciliation (fixes gotcha #1). Create config/gics_subindustry_to_industry.yaml mapping the GICS Sub-Industries present in data/companies.jsonl up to their GICS Industry (e.g. "Packaged Foods & Meats" → "Food Products"; "Soft Drinks & Non-alcoholic Beverages" → "Beverages"). Author it for the sub-industries actually present; raise a clear error if a company's sub-industry isn't in the map (so it fails loudly rather than silently producing no regulators). Stash the resolved gics_industry onto each company's metadata.

A4. regulated_by generation. Build pipeline/regulators.py: for each company, compute regulators = _default + sector[gics_sector] + industry[resolved_gics_industry] from config/regulators.yaml, de-dupe by regulator id. For each, emit a CandidateEdge: source_id=company cik, target_raw=regulator id (e.g. "regulator:fda"), type=regulated_by, confidence=1.0, verified=True, provenance={extracted_by:"rule", snippet:"regulators.yaml: <sector>/<industry>", url:"", filing:""}. Also emit the Regulator nodes themselves to a side file data/regulator_nodes.jsonl (validated Node records) for Phase 3 to load.

Part A acceptance gate: running Part A over Consumer Staples produces, for a food company, regulated_by candidates including FDA + USDA + FTC + SEC; section extraction finds a Competition section in the large majority of filings; the sub-industry map raises on any unmapped sub-industry. Print counts. Then proceed to Part B.

=== PART B — LLM extraction + grounding verify ===

B1. Configurable extractor. Build pipeline/extractor.py with a small interface `extract_edges(section_text, source_company) -> list[candidate dicts]` and two implementations selected by the EXTRACTOR env var (default "claude-cli"):
   - ClaudeCLIExtractor (default): shells out to `claude -p <prompt> --output-format json`, captures stdout, parses the model's reply. NO API key, NO Anthropic SDK client.
   - GemmaExtractor: calls a local Ollama endpoint (http://localhost:11434). Fallback only.
   Both return the same candidate shape. The inner prompt must instruct: "Extract ONLY relationships explicitly stated in the text below. Return a strict JSON array, no prose, no markdown. Each item: {target, type: 'supplies'|'competes_with', snippet: <a verbatim substring copied exactly from the text that states this relationship>, confidence: 0-1}. If none, return []." Parse defensively; on malformed output, retry once, then skip the section and log it.
   Tag each candidate's provenance.extracted_by with the real engine ("claude-code:<model>" or "gemma-27b").

B2. THE VERIFY GATE (invariant #4 — non-negotiable, model-agnostic). For every candidate the extractor returns, before it is allowed into edges_raw:
   - normalize whitespace on both the candidate snippet and the filing's cached .txt;
   - REJECT if the snippet is not a literal substring of the filing text (the model fabricated the quote);
   - REJECT if the snippet does not contain the target name (or a distinctive token of it);
   - candidates passing both → verified=True and written; rejected → dropped, with a per-filing tally of accepted vs rejected logged.
   Add a unit test: feed a fabricated snippet that does not appear in a source text and assert it is rejected; feed a grounded one and assert it passes.

B3. Direction. "supplies" means source_company sells to the named target. A customer-concentration passage ("Walmart accounted for ~15% of net sales") means source_company supplies Walmart → emit type=supplies, target_raw="Walmart". Never emit customer_of.

B4. Segment gating (material only). Maintain config/conglomerates.yaml — an explicit allowlist of CIKs permitted to be decomposed into seg: nodes. Default: EMPTY for Consumer Staples. Only if a company is on the allowlist AND its filing reports multiple material operating segments do you mint seg: nodes (id seg:cik<10>:slug) and part_of candidate edges. Do not auto-decompose every multi-segment filer.

B5. Resumable batching. Process filings one at a time; checkpoint completed (cik, section) pairs to data/extract_checkpoint.json and skip them on re-run. This makes a future full-S&P run splittable across quarters without redoing work. Add a --limit N flag for test runs.

B6. Assemble output. Append all CandidateEdges (LLM-extracted supplies/competes_with + rule-generated regulated_by from Part A) to data/edges_raw.jsonl. Targets stay raw strings — NO resolution.

Part B acceptance test (must pass before you stop):
   - Run extraction over Consumer Staples (claude -p, no API key).
   - P&G's filing yields a competes_with candidate naming at least Kimberly-Clark and/or Colgate, and a supplies candidate involving Walmart — each with verified=True and a snippet that literally appears in P&G's filing and contains the target name.
   - The verify gate demonstrably rejects ungrounded candidates (show the accepted/rejected tally; unit test green).
   - regulated_by candidates present for every company.
   - 0 seg: nodes (allowlist empty for this sector).
   - Re-running skips checkpointed filings (0 reprocessed).
   - No target resolution happened; edges_raw targets are raw strings / regulator ids.
   - Print the run summary (filings processed, candidates by type, accepted/rejected counts) and STOP. Summarize what Phase 3 (resolution) will consume.
```

---

## Notes for you (not part of the prompt)

- **Prereq:** Claude Code must be installed and logged in to your Max account on this machine (the same one running the pipeline), since `claude -p` inherits that auth. No `ANTHROPIC_API_KEY` needed or wanted — the prompt forbids it.
- **Expect to tune the inner prompt.** Even Opus/Sonnet occasionally wraps JSON in prose or markdown fences; the defensive parser + one retry handles it, but eyeball the first few outputs and tighten the inner prompt if the accept rate looks low. This is the one place worth a few interactive iterations.
- **The verify gate is your no-invented-suppliers guarantee** and it's deliberately dumb: a fabricated quote can't be a substring of the real filing, so it's dropped no matter how plausible it sounds. This is what lets you trust a 36-filing graph you didn't read yourself.
- **Why candidate edges, not Edges:** targets like "Kimberly-Clark Corporation" aren't canonical IDs yet. Phase 3 resolves them (to a `cik:` if they're an S&P filer, else a new `wikidata:`/`slug:` non-filer node), de-dupes, applies a confidence threshold, and only then emits validated `Edge` records. Keeping Phase 2 at the candidate layer is what keeps the single-node invariant clean.
- **On segments staying empty:** that's correct for consumer staples and keeps the MVP tight. The allowlist + reported-segments gate is the mechanism that will let Berkshire/Alphabet decompose later without flooding the graph now.

When this gate is green, Phase 3 (resolution) is the next prompt — the entity-matching phase where target names collapse to canonical nodes and the single-node invariant gets enforced in practice. That's also where the alias table earns its keep.
