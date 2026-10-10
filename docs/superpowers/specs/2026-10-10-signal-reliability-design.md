# Signal Reliability — feedback/validation layer (design)

**Goal.** Measure whether the impact engine's propagation signals are borne out by real stock
behavior, turn that into a trustworthy, improvable reliability score, and surface it on a
dashboard. Phase 1 (this spec) = the measurement + scorecard. Phase 2 (designed-for, NOT built)
= auto-tune the model from the measured reliability.

## Agreed design brief
Validate each **propagation signal** — one `event_impacts` row `(event→node, hop, direction,
magnitude, published_at)` — against that node's **abnormal return** (sector-relative) at a
**panel of horizons** `+1/+5/+20/+60 trading days`. Pool signals into **classes**
(`order × magnitude-band × direction × horizon`) and score each class on **(a) directional
hit-rate, (b) magnitude information-coefficient, (c) confidence-calibration**. Only
**tickered** Company nodes are in scope (~573). Prices from a provider-agnostic layer
(yfinance default — keyless; Tiingo when `TIINGO_API_KEY` set).

`hop` IS the "order": hop 0 = first-order (direct news), hop 1 = second-order
(supplier/competitor), hop 2+ = third-order. The order→horizon mapping is MEASURED, not assumed.

## Methodology (what makes it trustworthy)
- **Abnormal return** = `stock_return − benchmark_return` over the same window. Benchmark =
  the node's GICS **sector ETF** (XLK/XLF/XLE/…); fall back to **SPY** when the sector is
  unknown. Isolates the company-specific reaction from market/sector drift.
- **No look-ahead.** Base date = the first trading day **on/after** `published_at` (the point
  we'd actually know the news). Returns run forward from that day's close.
- **Maturity gating.** A signal's horizon outcome is computed only once `base + horizon` trading
  days of price data exist. Immature signals are pending, not scored.
- **Clean-subset flag.** A second/third-order signal is `clean=1` only if the node had **no
  direct (hop-0) news of its own** in the window — so a node's own first-order move can't
  contaminate the propagated-effect measurement. Reliability is reported both all-signals and
  clean-only.
- **Statistical honesty.** Reliability is a property of a *class*, not a single signal. Every
  class carries `n` and a Wilson confidence interval on its hit-rate; small-n classes are shown
  but flagged low-power.

## Components (layered, file/DB-based, restartable — CLAUDE.md #5)
1. `pipeline/prices.py` — provider-agnostic daily adjusted-close fetch (yfinance | Tiingo),
   cached in the `prices` SQLite table (the table IS the cache) with politeness; upserts into
   the `prices` table. `ensure_prices(conn, tickers, start, end)`. Sector→ETF map lives here.
2. `pipeline/signal_outcomes.py` — join `event_impacts × events × nodes(ticker,sector)`, compute
   per-(signal × horizon) abnormal returns, hit, clean, maturity; upsert `signal_outcomes`.
   Idempotent + incremental (only newly-mature signals). `compute_outcomes(conn)`.
3. `pipeline/reliability.py` — aggregate mature outcomes into `signal_reliability` (per class:
   n, hit_rate + Wilson CI, mean/median abn_ret, IC) and emit a scorecard JSON. `compute_reliability(conn)`.
4. `pipeline/run_validation.py` — orchestrator prices→outcomes→reliability. On-demand now,
   schedulable **daily** (EOD prices). Uses **NO LLM** — no Claude-CLI quota.
5. `api/main.py` — `/reliability/summary` + `/reliability/classes` serve the scorecard.
6. `web/` — a new **"Reliability" tab**: overall hit-rate; hit-rate heatmap `order × horizon`;
   confidence-calibration bars (hit-rate by magnitude band); IC by order×horizon; coverage/
   maturity status; n + CI everywhere.

## Schema (Pydantic + Zod, validated at boundaries — CLAUDE.md #9)
- `prices(ticker, date, adj_close)` — PK (ticker, date).
- `signal_outcomes(event_id, node_id, ticker, hop, direction, magnitude, base_date, horizon_days,
  stock_ret, benchmark, benchmark_ret, abn_ret, hit, clean, computed_at)` — PK (event_id,node_id,horizon_days).
- `signal_reliability(hop, direction, mag_band, horizon_days, clean, n, hit_rate, ci_low, ci_high,
  mean_abn, median_abn, ic, computed_at)` — PK (hop,direction,mag_band,horizon_days,clean).

## Non-goals (Phase 1)
No auto-tuning of model weights/confidence (Phase 2 reads `signal_reliability`). No per-signal
live badge on the map (the dashboard is the surface). No intraday prices. No market-model/beta
abnormal returns (sector-excess is the Phase-1 method; beta is a possible later refinement).

## Reality note
The historical signal corpus was pruned; the live set is ~days old, so the scorecard starts
sparse and matures over days (short horizons) to ~a quarter (+60d). A one-time historical
re-trace backfill (LLM-quota-heavy) would populate it faster — deferred, offered post-reset.

## Operations + Phase 2 (built 2026-10-10)
Three follow-ups turned the measurement layer into a self-maintaining, improvable system:

1. **Daily auto-run (no LLM).** `scripts/daily_validation.ps1` runs `pipeline/run_validation`
   once a day (6:00 PM local) via the `EconGraph daily validation` scheduled task, launched
   through `scripts/run_validation_hidden.vbs` (zero-flash wscript pattern, like the keepalive).
   Idempotent + incremental, so the scorecard matures on its own.

2. **Historical backfill.** `scripts/backfill_history.py` ingests + traces old GDELT-GKG news so
   the longer horizons mature immediately. INGEST (sample historical slices → match nodes →
   dedupe → materiality-threshold → queue; rule-only, NO LLM) / TRACE (bounded precompute
   batches, the only quota cost) / VALIDATE (after each batch, so `signal_outcomes` — never
   pruned — persists even after the old events age out). It must NOT run via `run_cycle` (whose
   prune would delete the old events); `run_cycle` now honors `PRUNE_OLDER_THAN_DAYS` as a safety
   valve, and the one-shot runner (`scripts/run_backfill_oneshot.ps1`) pauses the hourly cycle for
   the duration and always re-enables it. Armed as a one-shot task for after the quota reset.
   Two bugs this surfaced, both fixed (they also harden the daily path): `prices.ensure_prices`
   now backfills EARLIER history (not just forward), and NaN closes from yfinance are dropped.

3. **Auto-tune hook (opt-in, no LLM).** `pipeline/reliability_tune.py` maps a signal class
   `(hop, direction, mag_band)` at one horizon to a reliability multiplier, applied to the
   per-contribution weight in `aggregate_impacts.aggregate()`. A class earns a non-neutral
   multiplier only with demonstrated skill (n ≥ N_MIN AND a Wilson CI excluding 0.5), then a
   shrunk, clamped `[0.5, 1.5]` tilt. Gated by `RELIABILITY_TUNE_ENABLED` (default `0`) → every
   multiplier is 1.0 and aggregation is byte-identical. Lives in the derived node_impact layer,
   never event_impacts, so it can't feed back into its own measurement.
