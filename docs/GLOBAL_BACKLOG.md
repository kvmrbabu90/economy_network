# Global Expansion Backlog

Tracks Phases B, C, D from the global-coverage roadmap. Phase A
(foreign companies with US ADR listings via 20-F filings) is being
delivered now; this file captures what's left.

---

## Phase B — Wikidata-anchored expansion (~2–3 weeks)

**Goal:** add ~500–1000 companies that have **no US listing** and
therefore no SEC filings to pull from. Coverage gaps after Phase A
include: Saudi Aramco, Nestlé, LVMH, BYD, Reliance Industries, Tencent
(if no ADR), Maersk, Airbus (note: AIR.PA, no 20-F), Inditex, etc.

**Architecture:**
- Use Wikidata as the spine. SPARQL enumerates top-N by
  `P:2139` (revenue) or `P:2226` (market cap), filtered by country
  / sector.
- For each Q-id we capture: official name, country, HQ city, lat/lon
  (we already do this in `pipeline/wikidata.py`).
- Pull **P:1830 competitor**, **P:1071 supplier**, **P:127 owned-by**,
  **P:355 has-subsidiary** at scale. (Wikidata enrichment already
  handles P:1830 and P:1071; expand to ownership graphs.)
- Fetch each company's Wikipedia "Business" / "Operations" section
  via the MediaWiki REST API. Pipe through `claude -p` for
  competitor / customer / supplier extraction. Same verify gate as
  the 10-K extractor: snippet must literally name the target.

**Provenance trade-off:** edges will carry `extracted_by =
"llm:claude-cli"` with `provenance.filing = "wikipedia:<title>"`
rather than an SEC accession. Less rigorous, but transparent.

**Open decisions for the user:**
- Cap at N companies (suggest 750: top 250 European + top 250 Asian
  + top 100 Middle East / Africa + top 150 Latin America).
- Whether to gate Phase B nodes behind a separate audit-layer toggle
  ("show Wikipedia-derived edges") so the user can disable them.
- Cost: ~750 LLM extraction calls + N rounds of Wikidata SPARQL.
  Bounded; ~$30–80 worth of Max-plan-credit tokens.

---

## Phase C — Global regulators (~1 week)

**Goal:** every company that's regulated outside the US needs the
right `regulated_by` edges. Today every node in `config/regulators.yaml`
is a US federal regulator.

**Work to do:**
- Add EU regulators: ECB, ESMA, EBA, EFSA, EMA, ACM, BEREC.
- Add national EU regulators: BaFin (DE), AMF (FR), CNMV (ES),
  CONSOB (IT), Bundeskartellamt (DE), AdC (PT).
- Add UK regulators (post-Brexit): FCA, PRA, CMA, Ofcom, Ofgem, MHRA.
- Add Asian regulators: JFSA, METI, CSRC, PBoC, SEBI, RBI, MAS, FSC-KR.
- Add Australia (ACCC, ASIC) + Canada (CSA, Health Canada) regulators.
- Rewrite `config/gics_subindustry_to_industry.yaml` mapping so
  GICS sub-industries route to BOTH the US federal regulator AND the
  applicable foreign regulator based on company `country` field.
- Likely needs a new `config/country_subindustry_to_regulators.yaml`
  with structure `{country: {sub_industry: [regulator_ids]}}`.

**Implementation:** extend `pipeline/regulators.py` to consult the
new country-aware map. Backward-compatible: US companies still get
US regs only.

---

## Phase D — Localized retail markets (~3 days)

**Goal:** today every B2C industry routes to a hard-coded subset of
the 7 retail-region nodes, with no notion that (say) a Korean
electronics company sells primarily in Korea + China + Japan, not
Brazil. The Phase D upgrade is country-aware retail routing.

**Work to do:**
- For each foreign company, infer its primary markets from its
  `country` field plus its sub-industry. (E.g., Samsung Electronics:
  KR -> JP, CN, SEA + US/EU. LVMH: FR -> EU, US, CN, JP.)
- Add a `config/country_default_retail_markets.yaml` that maps country
  codes to default consumer markets:
  ```yaml
  KR: [korea-consumer, china-consumer, japan-consumer, us-consumer, eu-consumer]
  JP: [japan-consumer, us-consumer, eu-consumer, china-consumer]
  ...
  ```
- Add new `region:` consumer nodes for under-represented markets:
  Korea, Australia, Canada, Mexico, Middle East, Sub-Saharan Africa.
- Rewire `pipeline/commodities.py` to consult country defaults
  alongside the GICS sub-industry mapping.

---

## Phase E — Geography-aware impact reasoning (~1 week)

**Goal:** eliminate false-positive impact verdicts caused by the LLM
treating domestic supply relationships as globally applicable. Observed
failure case: Chic-fil-A enters India → LLM scores Tyson Foods
*positive* (hop 1) because the graph has `Tyson → Chicken (Poultry)`
extracted from US 10-K filings. Tyson has no India supply presence;
the inference is wrong.

**Root causes (two independent gaps):**

1. **Supply edges carry no geographic scope.** Every `supplies` edge
   extracted from a 10-K is implicitly US-scoped, but the schema has no
   `supply_geography` field. The LLM cannot distinguish "Tyson supplies
   Chic-fil-A in the US" from "Tyson supplies Chic-fil-A globally."

2. **Impact prompt has no geography filter.** `api/impact.py` asks the
   model to traverse the graph and score nodes but does not instruct it
   to check whether a supplier actually operates in the event's
   geography before assigning a positive verdict.

**Two-track implementation:**

**Track A — Prompt fix (quick, ~1 day, deploy independently):**
- Extend the system prompt in `api/impact.py` with an explicit
  geography-reasoning step:
  *"Before scoring any supply-chain node, reason about whether that
  supplier has documented operations in the geography of the event.
  If the event is country-specific (e.g. 'enters India') and the node's
  `country` field or known market presence does not include that country,
  assign `direction: no_effect` with reasoning citing the geographic
  mismatch. Do not infer benefit from a supply relationship that is
  geographically incompatible with the event."*
- Add `company_country` to the node context passed to the LLM for each
  hop so it can make the comparison without hallucinating.
- **Acceptance test:** "Chic-fil-A enters India" → Tyson Foods verdict
  must be `no_effect`; Indian poultry / Consumer Market India must be
  `positive`.

**Track B — Edge metadata enrichment (thorough, ~3–4 days):**
- Add `supply_geography: str | None` field to the `Edge` Pydantic model
  and the SQLite schema (nullable, default `null` = unknown/global).
- During extraction (`pipeline/extract.py`), prompt the LLM to infer
  geographic scope from the filing snippet context. 10-K filings are
  inherently US-scoped; set `supply_geography = "US"` by default unless
  the snippet explicitly names a non-US geography.
- For Wikidata/Wikipedia-sourced edges, infer scope from the company's
  `country` field.
- Expose `supply_geography` in the `/edge/:id` API response and in the
  impact propagation node context so the LLM (Track A) can reference it
  directly instead of reasoning from `country` alone.
- **Acceptance test:** `Tyson → Chicken (Poultry)` edge carries
  `supply_geography: "US"`; impact run on India event filters it out at
  the edge-context stage, not just the LLM reasoning stage.

**Recommended sequencing:** ship Track A first (prompt-only, no data
migration, immediate improvement). Track B during a subsequent sprint
for deeper correctness, particularly once Phase D's localized retail
routing adds more geography signals to the graph.

**Open questions:**
- Should `supply_geography` be a single country code, a list, or a
  region key (e.g. `"LATAM"`)? Suggest free-text with a controlled
  vocabulary: `"US"`, `"EU"`, `"global"`, ISO-2 for single countries.
- How to handle edges where geographic scope is genuinely ambiguous
  (e.g. a commodity supplier that ships internationally)? Default to
  `null` = "do not filter"; only assign scope when evidence is strong.

---

## Cross-phase concerns (decide before starting B/C/D)

1. **Identifier upgrade.** Today canonical ids are `cik:`,
   `wikidata:`, `slug:`, `regulator:`, `commodity:`, `region:`. For
   Phase B (no SEC filings) we'd lean on `wikidata:Q...` as the
   primary form. That's already in the schema -- no migration
   required, just policy: a Wikidata-only company gets
   `wikidata:Q12345` as its canonical id, with optional LEI / ISIN
   in the `identifiers` blob.

2. **Cross-listings.** Toyota appears as `cik:0001094517` (20-F
   filer) AND `wikidata:Q53268`. The alias-table merge logic should
   collapse them automatically -- but worth a unit test once Phase A
   data lands.

3. **Frontend implications at 2.5x scale.** Going from 500 to ~2000
   companies takes the graph from ~2,500 to ~5,000+ nodes. Test:
   - Sigma FA2 layout time (currently ~5s on 500 nodes -- should
     still settle under 30s at 2000)
   - 3D / Globe edge-count budget (we're at ~6,500 tubes today; could
     hit 25k+ with global coverage)
   - Search disambiguation when multiple companies share short names
     across jurisdictions.

4. **Currency / market-cap normalization.** If we ever want to size
   nodes by market cap or revenue, we'll need FX normalization to a
   single base currency. Defer until there's a UI feature that needs
   it.

---

## Status

- Phase A: **complete** (2026-05-25). 67 foreign companies ingested
  via 20-F (Toyota, Samsung, Shell, TSMC, Alibaba, ASML, Petrobras,
  HSBC, Sanofi, Novartis, AstraZeneca, ...). companies.jsonl 500 →
  567; total Company nodes 567; supplies edges 2,545 → 3,598;
  edges_raw 11,477 → 14,000; rebuilt graph 2,483 → 2,781 connected
  nodes. Verify smoke test: 5/7 PASS, 1 PARTIAL (Alibaba HQ shown
  as "Binjiang District" — district within Hangzhou, geocode is
  correct), 1 FAIL (Infosys has no Wikidata CIK→Q-id mapping in
  Wikidata's data — known gap, not our bug; backlog adds Infosys
  via Phase B).
- Phase A follow-ups (low priority, defer to Phase B work):
  - 6 entries commented out in foreign_filers.yaml because they
    don't file 20-F today (Tata Motors, CBD, Westpac, MercadoLibre,
    Yum China, BeiGene). All addressable via Phase B Wikidata path.
  - Add more European 20-F filers we missed (BNP Paribas, Allianz,
    Siemens — though most file 6-K + reverse merger paths not 20-F).

- Phase B: **complete** (2026-05-25). 686 non-US companies ingested
  via Wikidata SPARQL (top companies by country/sector spanning
  Europe, Asia, Middle East, Africa, Latin America). Wikipedia
  "Business" / "Operations" sections extracted + LLM-processed via
  `pipeline/extract_wikipedia.py` (incremental flush, verify gate);
  300 verified edges added. Wikidata P:1830 competitor enrichment
  added 399 competes_with edges for the new wikidata: nodes via
  `pipeline/wikidata_phase_b.py`. Final graph after B+C rebuild:
  3,743 nodes (up from 3,634), 12,907 edges (up from 12,691 pre-B),
  7,493 core + 5,414 audit edges. New files:
  `pipeline/wikidata_phase_b.py`, `pipeline/extract_wikipedia.py`
  (with incremental flush). Schema fix: `CandidateEdge.source_id`
  validator extended to allow `wikidata:` prefix.

- Phase C: **complete** (2026-05-25). Two-tier regulated_by routing
  implemented in `pipeline/regulators.py`:
  (a) `wikidata:` companies (non-SEC filers) → ONLY their country's
      regulators from `config/country_regulators.yaml`.
  (b) `cik:` foreign filers (20-F) → US sector/industry rules
      PLUS country supplements.
  New file `config/country_regulators.yaml` covers ~40 countries
  (JP, CN, KR, TW, SG, MY, TH, IN, ID, AU, NZ, SA, AE, EG, ZA,
  NG, MA, GB, DE, FR, NL, IT, ES, SE, DK, FI, AT, PT, IE, BE, NO,
  CH, PL, CA, BR, MX, CO, PE, CL, AR + `_eu_supranational` ESMA
  merged into all 27 EU member states). 60 new regulator nodes
  appended to `data/regulator_nodes.jsonl` (93 total vs 33 pre-C).
  Acceptance tests (via API ego endpoint):
  - Toyota (cik/JP 20-F): SEC + FTC + NHTSA + EPA + JP-FSA + JP-METI ✓
  - Toyota-Astra (wikidata/ID): OJK + BI only, no US rules ✓
  - ASML (cik/NL 20-F): SEC + BIS + NL-AFM + ESMA ✓

- Phase D: backlog.
- Phase E: backlog.
