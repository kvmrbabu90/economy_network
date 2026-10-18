# Domino study (Stage 1): do price shocks ripple through the EconGraph map?

*Design spec and pre-registration. Written on 2026-10-18, before any long-history price data was downloaded or examined. Status: **APPROVED 2026-10-18, frozen at the commit that introduces this line.** Any later change is logged in §13.*

## 0. In plain language

When one company's stock makes a big move of its own, do the companies connected to it on the map (its suppliers, customers and competitors) move too? If so, do they move on the same day, or slowly over the following days and weeks?

We compare connected companies with similar companies that are **not** connected, so an industry-wide move is not counted as a ripple. The study uses no news and no LLM: only prices and the map.

Only a **slow** ripple could support a valuable strategy. A same-day ripple means the market already prices the link instantly.

## 1. Purpose and stage gate

This is Stage 1 of a four-stage research program:
1. Do ripples exist in prices, and how do they move?
2. Can an LLM or a dedicated model predict them?
3. How fast does the market price them in?
4. Practicality and data speed.

Stage 2 starts only if Stage 1 finds a **slow** ripple that passes the rules in §6 (the decisions are in §7). This study makes no claim about tradability. As agreed, returns are close-to-close and latency is ignored.

## 2. Questions

- **Q1. Same-day ripple.** On the day of A's shock, do linked firms move more than unlinked look-alikes, in a consistent direction?
- **Q2. Slow ripple (primary).** From the day after A's shock, do linked firms keep moving relative to look-alikes over the next 5, 20 and 60 trading days?
- **Q3. Direction by relationship.** Does the ripple go the same way as A's move for A's suppliers, A's customers and A's competitors?
- **Q4. The first domino.** After its shock, does A keep drifting or snap back?
- **Q5. What makes ripples bigger.** Candidates: shock direction, shock size, abnormal trading volume on the shock day, link strength (revenue share where known), and the size of the linked company.

## 3. Data

**Universe.** The 573 Company nodes in the production graph that have a ticker. The graph is read-only for this study. All 573 have a GICS sector; normalize "Healthcare" to "Health Care".

**Prices.** Daily split- and dividend-adjusted close and volume from Yahoo Finance via `yfinance`, from 2013-09-01 to the latest available date. The pre-2015 history gives early-2015 events their full [−270, −21] estimation window. Coverage is the 573 tickers, SPY, and the 11 SPDR sector ETFs. Data is cached once to a research store outside OneDrive and never re-fetched while cached.

**Factors (robustness only).** Fama-French 5 factors plus momentum, daily, from the Ken French data library.

**Links.** Company-to-company edges from the production graph, kept when both ends have tickers and the edge is above threshold. Excluded: edges with `prov_extracted_by` equal to `wikidata` (property P1830 means "owner of" but was loaded as competitor) or `inference:co-mention`. On 2026-10-18 this leaves:
- `supplies` (directed; X supplies Y): **150 edges** (145 LLM-extracted, 5 manual). 38 carry a revenue-share weight. They touch 134 companies.
- `competes_with` (undirected): **652 edges** (642 LLM-extracted, 10 manual), touching 382 companies.

The edge list is snapshotted to a file when the study starts and is not changed during the study.

**Link roles relative to the shocked firm A.** In each event, a linked firm B has one role:
- **A's supplier.** An edge says B supplies A, so a customer's shock reaches its supplier. This is the classic Cohen-Frazzini channel.
- **A's customer.** An edge says A supplies B.
- **A's competitor.** A `competes_with` edge links A and B.

A pair with more than one role appears once per role. The number of such pairs is reported; we expect it to be small.

**Periods.**
- **Exploration:** event dates from 2015-01-01 to 2021-12-31.
- **Holdout (locked):** event dates from 2022-01-01 onward, for events whose outcome windows are complete. The holdout is not analyzed until the exploration readout has been written and committed.

Today's graph is applied to past years, when some relationships may not have existed. This mostly adds noise, so it biases the study toward finding nothing. The holdout is the period closest to when the graph was built.

## 4. Definitions

**Time.** Day 0 is the shock day. All windows count trading days.

**Abnormal return (AR).** Market model:

AR(i,t) = r(i,t) − (α(i) + β(i) · r(SPY,t))

- α and β are estimated by OLS over days [−270, −21] relative to each event.
- At least 200 valid days are required. Otherwise the firm is excluded from that event.
- σ(i) is the standard deviation of the estimation-window residuals.
- CAR over a window is the sum of the daily ARs.

**Shock (the first domino).**
- Firm A has a shock on day 0 if |AR(A,0)| ≥ 3·σ(A).
- A shock within 10 trading days after an earlier shock of the same firm is dropped, so one story is not counted twice.
- The shock sign is s = sign(AR(A,0)).

**Data-error rule.** If |AR| > 50%, check the next day's price. If the move fully reverses, treat it as a bad print and drop it.

**Event.** A (shock at A, linked firm B, role) triple. It is excluded if B lacks price data for the window being measured, or if B has fewer than 2 eligible controls.

**Controls (look-alikes).** Each event gets up to K = 5 control firms C, which must meet all of these:
- C is in the same sector as B.
- There is no edge of any type, extractor or threshold between C and A, in either direction.
- C is neither A nor B.
- C is chosen by nearest log average daily dollar volume to B over days [−80, −21]. Ties are broken by ticker.

**Outcomes for each window W:**
- Linked move: s · CAR(B, W)
- Control move: s · mean CAR(C, W) across the event's controls
- **Ripple (primary measure): s · [CAR(B, W) − mean CAR(C, W)].** A positive value means B followed A's direction more than its unlinked look-alikes did.
- First-domino drift: s · CAR(A, W)

**Windows.**
- Pre-trend: [−5, −1]
- Same day: [0, 0]
- Slow: [+1, +5], [+1, +20], [+1, +60]

**Transmission coefficient γ.** For each role and window, regress [CAR(B, W) − mean CAR(C, W)] on AR(A, 0) by OLS. γ is the share of A's shock that shows up in B beyond what the look-alikes show.

## 5. Statistics

- **Clustering.**
  - Primary: standard errors clustered by event date (day 0).
  - Robustness: clustering by calendar week, and a block bootstrap that resamples whole event dates (5,000 draws, fixed seed).
- **One-sided tests.** Holdout replication uses a one-sided test in the direction found during exploration.
- **Multiple testing.** The primary family is the 9 H2 tests (3 roles × 3 slow windows). In exploration, a test passes only if it clears both:
  - Holm-Bonferroni at family α = 0.05, and
  - |t| > 3, the Harvey, Liu and Zhu bar.
- **Averaging.** Every mean is equal-weighted across events.
- **Placebo (permutation).** In the holdout, re-run the primary statistic 500 times with the real links replaced by random pairs. Each random pair keeps the sector pairing and role direction of a real link and is not itself a real link. Real links must beat the placebo's 95th percentile.
- **Power check.** This runs first, on exploration data only, before any hypothesis test.
  - Inject a synthetic slow ripple of size δ, spread evenly over [+1, +20], into linked firms' returns.
  - Measure how often the full pass rule detects it.
  - Report each role's minimum detectable effect (MDE) at 80% power.
  - If a role's MDE exceeds **1.5% over 20 days** (roughly the historical Cohen-Frazzini effect per month), that role is reported as **underpowered / inconclusive**, never as "no effect".

## 6. Hypotheses and pass rules (pre-registered)

| # | Hypothesis | Pass rule |
|---|---|---|
| H1 | A same-day ripple exists | For at least one role, mean Ripple[0,0] > 0 with t > 3 in exploration, and one-sided p < 0.05 in the holdout |
| **H2 (primary)** | **A slow ripple exists** | For a role and a window in {[+1,+5], [+1,+20], [+1,+60]}, all four hold: it passes Holm and t > 3 in exploration; the holdout has the same sign with one-sided p < 0.05; the holdout mean is at least **0.5% (50 bp)**, a floor set comfortably above retail round-trip costs; and it beats the holdout placebo's 95th percentile |
| H3 | No pre-trend | For every role that passes H1 or H2, Ripple[−5,−1] has \|t\| < 2. If this fails, the result is flagged as possible shared information rather than transmission |
| H4 | Direction by role | Descriptive: sign and size of the Ripple measure by role and window. The competitor sign is tested two-sided |
| H5 | First-domino drift | Descriptive: s·CAR(A) over the slow windows (continuation vs reversal) |
| H6 | What makes ripples bigger | Descriptive and hypothesis-generating, exploration only. See the splits below |

H6 splits:
- shock sign;
- shock size (3–4σ vs. >4σ);
- abnormal volume (day-0 volume divided by the [−80, −21] average: >2× vs. ≤2×);
- link weight (weighted vs. not; terciles where the counts allow);
- B's size (dollar-volume terciles).

To be claimed, any H6 pattern must be registered as a new hypothesis in §13 before the holdout is opened, and then confirmed in the holdout.

## 7. What each outcome means

- **H2 passes for any role:** slow ripples are real and large enough to study. Go to Stage 2 (prediction). Stage 3 (speed) becomes worth measuring with intraday data.
- **H1 passes, H2 fails, power is adequate:** the market prices the link on the same day. Any value would sit in a latency race, where faster data alone is unlikely to be enough. Stage 2 is not justified for trading.
- **Nothing passes, power is adequate:** the map's links don't carry measurable price ripples. Close the price-ripple thesis for these link types.
- **A role is underpowered:** strengthen the map by extracting supplier and customer links, with revenue weights, from 10-K filings. Then re-run under this same frozen protocol.

## 8. Robustness checks

These are pre-specified. They are reported but do not change the verdict.

1. Sector-ETF excess return instead of the market model.
2. Fama-French 5 factors plus momentum.
3. Shock threshold of 4σ; separately, the top 1% of |AR(A)| per firm.
4. Exclude event dates from 2020-02-15 to 2020-06-30 (the COVID crash).
5. Exclude events where A's sector ETF moved ≥ 2σ on day 0 (industry-wide shocks).
6. Holdout restricted to 2024 onward, the years closest to the graph's filing dates.
7. K = 3 and K = 10 controls.
8. LLM-extracted links only; separately, manual links only.
9. Raw linked and control moves, reported separately, to show how much of the move is industry-wide rather than link-specific.

## 9. Test B (news dominoes): outline only

Test B uses the same measurement, but day 0 is a news event that names A instead of a price shock.
- Events come from GDELT GKG organization mentions, matched to graph nodes by the existing matcher. No LLM tracing.
- News types come from GKG themes and are assigned by rules.

The data acquisition (which GKG slices, how many years) and the exact definitions will be written as a separate pre-registration. That happens after Test A's readout and before any news-outcome data is examined.

## 10. Implementation outline

- **Module.** A new isolated module, `research/dominoes/`, built as layered, restartable, file-based stages (CLAUDE.md #5): `fetch_prices` → `snapshot_links` → `build_shocks` → `build_events` (with controls) → `power_check` → `measure` → `report`. Each stage reads the previous stage's files and writes its own.
- **Storage.**
  - The research store lives at `C:/Users/Public/econgraph/research/dominoes/`, outside OneDrive.
  - The production DB is opened read-only, and no production table or schema changes.
- **Fetching.**
  - `yfinance` requests are batched and cached, and never repeated while cached (CLAUDE.md #6).
  - The Fama-French archive is downloaded into its own empty directory and parsed as untrusted data.
- **Typed records.** Data passed between stages uses Pydantic models or declared column schemas (CLAUDE.md #9).
- **Tests.** Unit tests on synthetic data cover:
  - AR estimation and shock detection;
  - control selection, including that no control is linked to A;
  - role assignment and signed outcomes;
  - date clustering;
  - the power-injection routine.

  They also include two end-to-end checks: a **planted ripple that the pipeline must detect** and a **no-ripple dataset where it must find nothing**.
- **Output.**
  - A readout document with tables.
  - Average CAR paths from day −5 to +60 for A, linked B by role, controls, and B minus controls.

## 11. Known limitations

- **Survivorship.** Delisted companies are missing.
- **Look-ahead in links.** Today's graph is applied to past years. Mitigated by the holdout and robustness check 6.
- **Thin supplier links.** There are only 150 edges. The power check decides whether the supplier and customer roles can be interpreted.
- **Deliberately research-only.** Close-to-close returns, with no costs and no latency, as agreed. Tradability is a later stage.
- **Noise shocks.** Price shocks without news include noise and liquidity events, which may dilute ripples. The abnormal-volume split (H6) and Test B address this.
- **Data quality.** `yfinance` has occasional bad prints. These are handled by the data-error rule in §4.

## 12. Pre-registration discipline

**Frozen when approved:** the questions, data filters, definitions, windows, statistics, hypotheses, pass rules and the robustness list.

**Allowed to change, logged with a date in §13:**
- bug fixes that don't depend on outcome data;
- data-error handling;
- new analyses explicitly labeled exploratory.

The holdout stays closed until the exploration readout is committed.

## 13. Change log

- 2026-10-18: drafted.
- 2026-10-18: self-review fixes before approval. Price history now starts 2013-09-01 so early-2015 events get a full estimation window; the placebo is defined in the holdout; means are equal-weighted across events.
- 2026-10-18: approved by the user and frozen. No long-history price data had been downloaded at this point.
- 2026-10-18: clarifications and operational definitions, written while planning the build and **before any price data was downloaded**:
  1. **H2 direction.** In exploration the test is two-sided: |t| > 3, with Holm applied to two-sided p-values. Holdout replication is one-sided, in the direction found during exploration. The 50 bp floor applies to the holdout mean's size in that same direction. This resolves a conflict in §6 between "same sign" and "≥ 0.5%", consistent with the |t| in §5. H1 stays directional (positive follow), as written.
  2. **Shock dedup.** A shock is dropped if any earlier shock of the same firm, kept or dropped, occurred within the previous 10 trading days.
  3. **Data-error rule.** Applied to raw daily returns before model estimation, so bad prints also stay out of estimation windows. If |r(t)| > 50% and the next day brings the price back within 5% of its pre-move level, both days' returns are set to missing.
  4. **Window completeness.** A firm's outcome window is used only if the firm has a valid return on every trading day of the window. Controls without a complete window are dropped for that window. If fewer than 2 controls remain, the event is excluded for that window.
  5. **Dollar volume.** Size matching needs at least 20 valid days in [−80, −21].
  6. **Control ranking.** Up to 10 controls are ranked per event. The primary analysis uses the first 5; robustness check 7 uses the first 3 and all 10.
  7. **Abnormal volume (H6).** The shocked firm A's day-0 volume divided by its average over [−80, −21].
  8. **Trading calendar.** SPY's trading days.
  9. **Robustness check 5.** "A's sector ETF moved ≥ 2σ" means the ETF's own market-model AR on day 0 is at least 2σ in absolute value. The ETF's model is estimated the same way as a firm's.
  10. **Robustness check 3, top 1%.** The threshold is each firm's 99th percentile of |AR| across all its dates in the study period.
  11. **Holdout gate.** Holdout events are not built until the exploration readout is committed. The code enforces this with a marker file that records the readout's commit hash.
- 2026-10-18 (building the pipeline, **before any price data was downloaded**): 12. **US listings only.**
  - Each firm uses its first US-style ticker: 1–5 capital letters, with an optional one-letter share class.
  - The share-class dot becomes a dash for Yahoo, so `BRK.B` becomes `BRK-B`.
  - One firm has only a foreign listing (`300750.SZ`), so the universe is 572, not 573. That listing trades on a different calendar and time zone, so a US day-0 shock would show up in its next close. That could create a false "slow ripple".
