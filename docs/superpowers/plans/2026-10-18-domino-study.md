# Domino Study (Stage 1, Test A) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run the pre-registered price-domino study. When one company has a large price shock of its own, do its graph-linked suppliers, customers and competitors move more than matched unlinked look-alikes, on the same day or slowly afterwards?

**Spec:** `docs/superpowers/specs/2026-10-18-domino-study-design.md`. It is frozen; §13 lists the operational definitions this plan implements.

**Architecture:** An isolated research module, `research/dominoes/`, built as layered, restartable, file-based stages (CLAUDE.md #5):

`fetch` → `links` → `shocks` → `events` → `power` → `measure` → `report`

- Each stage reads the previous stage's parquet files from the research store, `C:/Users/Public/econgraph/research/dominoes/` (outside OneDrive), and writes its own.
- The production DB is opened read-only (`mode=ro` URI). No production table or schema changes.
- Holdout work is gated by a marker file that records the commit hash of the exploration readout.

**Tech stack:** Python 3.14, pandas 3, numpy 2, scipy (t-distribution), pyarrow (parquet), yfinance 1.3 (prices), pydantic 2 (config and manifest models), matplotlib (figures). Clustered statistics are hand-written and unit-tested; statsmodels isn't installed.

**Judgment call:** this plan specifies interfaces, test cases and commands rather than the full code of every step. That avoids writing a roughly 2,000-line pipeline twice. The code is written test-first during execution.

---

## File structure

| File | Responsibility |
|---|---|
| `research/__init__.py`, `research/dominoes/__init__.py` | Package markers |
| `research/dominoes/config.py` | Every constant from the spec (periods, windows, thresholds, K, seeds, paths, excluded extractors) plus `normalize_sector()`. One source of truth |
| `research/dominoes/io.py` | Research-store paths, parquet read/write with column-schema validation, read-only production-DB connection, holdout-marker check |
| `research/dominoes/fetch_prices.py` | yfinance batched download → `prices.parquet` (ticker, date, close, volume) + `fetch_manifest.json`. Never re-fetches cached tickers |
| `research/dominoes/fetch_factors.py` | Downloads the Ken French FF5 and momentum daily zips into their own empty directory, parses them as untrusted data → `factors.parquet` (robustness 2) |
| `research/dominoes/snapshot_links.py` | Production DB → `firms.parquet`, `links.parquet` (study links with roles), `any_edge_pairs.parquet` (every edge, used for control exclusion) |
| `research/dominoes/returns.py` | Wide return matrix on the SPY calendar, data-error rule, rolling market model (α, β, σ, n over [e−270, e−21]), cumulative sums for O(1) window CARs |
| `research/dominoes/build_shocks.py` | Shock detection (\|AR\| ≥ 3σ), dedup, period labels, first-domino CARs → `shocks.parquet` |
| `research/dominoes/build_events.py` | Role assignment, control ranking (up to 10), window outcomes for B and controls, H6 covariates → `events_{period}.parquet`, `controls_{period}.parquet` |
| `research/dominoes/stats.py` | Pure functions: clustered mean and SE (CR1), clustered OLS slope, Holm, one- and two-sided p, block bootstrap, week clusters |
| `research/dominoes/power_check.py` | Demean → inject δ → bootstrap detection rate; analytic MDE. Exploration only; prints no observed means → `power.json` |
| `research/dominoes/measure.py` | H1–H6, γ, robustness 1–9, placebo (holdout) → `results_{period}.json`, `paths_{period}.parquet` |
| `research/dominoes/report.py` | Readout markdown + CAR-path figures → `docs/research/domino-study/` |
| `research/dominoes/run.py` | CLI: `python -B -m research.dominoes.run <stage> [--period ...] [--variant ...]` |
| `tests/test_dominoes_*.py` | Unit tests per module + planted-ripple and null end-to-end tests on synthetic data |

---

### Task 1: Config, IO, package skeleton

**Files:** `research/__init__.py`, `research/dominoes/__init__.py`, `config.py`, `io.py`, `tests/test_dominoes_config.py`

- [ ] Write tests covering:
  - `normalize_sector("Healthcare") == "Health Care"`;
  - exploration and holdout boundaries;
  - windows `{"pre": (-5,-1), "d0": (0,0), "w5": (1,5), "w20": (1,20), "w60": (1,60)}`;
  - `EXCLUDED_EXTRACTORS == {"wikidata", "inference:co-mention"}`;
  - `holdout_unlocked()` is False when there is no marker, and True when the marker holds a commit hash that exists in git.
- [ ] Implement `config.py` (constants exactly as in spec §4–§5 and §13).
- [ ] Implement `io.py`:
  - `store_dir()` (env `DOMINO_STORE`, default `C:/Users/Public/econgraph/research/dominoes`);
  - `prod_conn()` (env `DOMINO_PROD_DB`, default `C:/Users/Public/econgraph/econgraph.db`, `file:...?mode=ro`). **Do not read `ECONGRAPH_DB`**: in some shells it points at a stale pre-migration copy;
  - `write_frame(name, df, schema)` / `read_frame(name, schema)`, which validate required columns and dtypes;
  - `holdout_unlocked()`.
- [ ] Run `python -B -m pytest tests/test_dominoes_config.py -q`.

### Task 2: Link snapshot

**Files:** `snapshot_links.py`, `tests/test_dominoes_links.py`

- [ ] Tests use an in-memory SQLite with `nodes` and `edges` shaped like production, covering:
  - only tickered Company nodes kept, sectors normalized, Yahoo symbol = first ticker with "." → "-";
  - study links: `below_threshold=0`, extractor not excluded, type in {supplies, competes_with}, both ends tickered;
  - roles for shocked A: `supplies(B→A)` → supplier, `supplies(A→B)` → customer, `competes_with` either way → competitor, with each competes edge expanded both ways;
  - `any_edge_pairs` includes **every** edge between two tickered companies, including excluded extractors and below-threshold edges, stored undirected.
- [ ] Implement. `links.parquet` columns: `a_id, b_id, role, extractor, weight`, expanded so each row is "if A shocks, B is A's ⟨role⟩".
- [ ] Run tests.

### Task 3: Returns and the rolling market model

**Files:** `returns.py`, `tests/test_dominoes_returns.py`

- [ ] Tests:
  - the data-error rule: a +80% print followed by a return to within 5% → both returns become NaN; a real +60% jump that holds → kept;
  - rolling α, β, σ at event index e equal a direct `numpy.polyfit` on rows [e−270, e−21] (tolerance 1e-10);
  - fewer than 200 valid pairs → NaN parameters;
  - `window_car(i, e, a, b)` from cumulative sums equals the direct sum of AR(i, t; e) over t in [e+a, e+b];
  - a window with any missing return → NaN.
- [ ] Implement vectorized rolling moments from cumulative sums of x, y, xy, x², y² and the valid count over the masked pairs, then shift by 21. Use SSR = Syy − β·Sxy (centered) and σ² = SSR/(n−2).
- [ ] Run tests.

### Task 4: Shocks

**Files:** `build_shocks.py`, `tests/test_dominoes_shocks.py`

- [ ] Tests:
  - a planted 5σ idiosyncratic move is detected, with the correct sign s;
  - a 2σ move is not detected;
  - with shocks at offsets 0, 8 and 16, offset 8 is dropped (within 10 of 0) and offset 16 is dropped too (within 10 of 8, per §13.2);
  - period labels are correct;
  - first-domino CARs: s·CAR(A) for w5, w20, w60.
- [ ] Implement → `shocks.parquet`: `shock_id, firm_id, ticker, date, e_idx, ar, sigma, z, s, period, abn_vol, sector_etf_z, cara_w5, cara_w20, cara_w60`.
- [ ] Run tests.

### Task 5: Events and controls

**Files:** `build_events.py`, `tests/test_dominoes_events.py`

- [ ] Tests:
  - controls share B's sector, are never A or B, and **never have any edge to A** (including excluded-extractor edges);
  - controls are ranked by |log DV(C) − log DV(B)| with ties broken by ticker;
  - fewer than 2 complete controls → event dropped for that window;
  - ripple = s·(CAR(B) − mean of the first K controls' CARs);
  - raw_b and raw_c are stored;
  - building holdout events raises when `holdout_unlocked()` is False.
- [ ] Implement → `events_{period}.parquet`: `event_id, shock_id, date, week, a_ticker, b_ticker, role, extractor, weight, s, z_a, abn_vol_a, sector_etf_z, dv_b`, plus for each window w and K in {3, 5, 10}: `ripple_{w}_k{K}, raw_b_{w}, raw_c_{w}_k{K}, nctl_{w}_k{K}`.
- [ ] Run tests.

### Task 6: Statistics

**Files:** `stats.py`, `tests/test_dominoes_stats.py`

- [ ] Tests:
  - `clustered_mean` matches a hand computation: CR1, se² = G/(G−1) · Σ_g (Σ_{i∈g} (v_i − v̄))² / n²;
  - with singleton clusters it equals the iid SE × √(G/(G−1));
  - `clustered_ols` slope and CR1 sandwich SE match a manual numpy computation;
  - Holm on [0.001, 0.01, 0.04] at α = 0.05 → passes [True, True, False];
  - p-values use the t distribution with G−1 degrees of freedom;
  - the block bootstrap CI is reproducible with a seed.
- [ ] Implement.
- [ ] Run tests.

### Task 7: Power check

**Files:** `power_check.py`, `tests/test_dominoes_power.py`

- [ ] Tests:
  - on synthetic null data, detection at δ = 0 is about 0 (≤ 2%);
  - detection rises monotonically with δ;
  - the MDE is within 25% of the analytic (3 + 0.84)·SE;
  - **the output JSON contains no observed means.**
- [ ] Implement. Per role, take ripple_w20_k5 (and the injected shares for w5 and w60), demean it, add δ·(window share) over a δ grid from 0 to 3% in 0.05% steps, run a 1,000-draw date-block bootstrap, and count a detection when |t| > 3. Report the bootstrap MDE (80%), the analytic MDE, SE, n_events, n_dates and an `underpowered` flag (MDE > 1.5%).
- [ ] Run tests.

### Task 8: Measurement

**Files:** `measure.py`, `tests/test_dominoes_measure.py`

- [ ] Tests:
  - **Planted-ripple end-to-end.** Synthetic market: 60 firms, 4 sectors, 2,600 days, random supplier and competitor links. Whenever A shocks, its suppliers get +0.8%·s spread over days +1 to +20. The supplier role must pass H2-exploration (|t| > 3) with a mean of about 0.8% ± 0.2%, while the competitor role must not pass.
  - **Null end-to-end.** The same market with no planting: no role passes H1 or H2.
  - **Pre-trend detector.** Planting a [−5, −1] move makes H3 fail.
  - Holdout measurement raises when locked.
- [ ] Implement exploration (H1–H6, γ, week clustering, bootstrap, robustness subsets 3 (4σ), 4, 5, 7, 8, 9) and holdout (replication, floor, placebo, robustness 6). The variants for robustness 1 (sector-ETF benchmark), 2 (FF5+UMD) and 3 (top 1%) re-run `shocks` → `events` → `measure` with `--variant` and write separate files.
- [ ] Run tests.

### Task 9: Report and CLI

**Files:** `report.py`, `run.py`, `tests/test_dominoes_report.py`

- [ ] Tests: the readout contains the verdict table and every H1–H6 section; figures are written; the CLI dispatches stages.
- [ ] Implement. Average CAR paths from day −5 to +60 (A signed; linked B raw, controls raw, and ripple by role) as PNGs in `docs/research/domino-study/figures/`. Readout markdown in `docs/research/domino-study/`.
- [ ] Run the full suite: `python -B -m pytest tests/ -q`.

### Task 10: Independent review before real data

- [ ] Run an adversarial multi-agent review of `research/dominoes/` against the spec, using **synthetic data only**. Lenses: look-ahead and off-by-one, sign conventions, control contamination, clustering correctness, spec fidelity. Fix confirmed findings, re-run the tests and commit.

### Task 11: Gated run protocol (real data)

- [ ] `fetch` (prices 2013-09-01 → today), `fetch_factors`, `links`, `shocks`, `events --period exploration`.
- [ ] `power`. Commit `power.json`, its summary and the change log entry.
- [ ] `measure --period exploration`, then `report --period exploration`. **Commit and push the exploration readout.** Register any H6 follow-ups in spec §13. Write the marker with the readout's commit hash.
- [ ] `events --period holdout`, `measure --period holdout`, `report --period holdout`. Commit and push the final readout.
- [ ] Run an adversarial verification of the readout's claims, then report to the user.
