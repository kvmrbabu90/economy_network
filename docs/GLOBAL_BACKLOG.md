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
- Phase B: backlog.
- Phase C: backlog.
- Phase D: backlog.
