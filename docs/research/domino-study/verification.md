# Domino study — independent verification and interpretation (2026-10-18)

Written after the holdout readout (`readout-holdout.md`). Four independent verifiers re-implemented parts of the analysis from the research store with their own code, not the pipeline's. A separate whole-branch code review checked the implementation. None of this changes the pre-registered verdict, which is **same-day ripple only** (supplier, competitor) and **no ripple** (customer).

## What was re-derived, and matched

- **Statistics.** Each of these was recomputed with an independent date-clustered CR1 implementation, and the results matched the readouts and results JSON to floating-point precision:
  - every H1, H2 (with Holm), H3 and H4 cell;
  - the holdout customer [+1,+5] chain;
  - H6-F1 and its exploration basis.

  The §7/§13.17 verdict was also re-derived by hand and matched.
- **Events from raw prices.**
  - 12 random primary events (6 exploration, 6 holdout, 2 per role) and 10 targeted edge cases were rebuilt from `prices.parquet` with an independent market model. Ripple, raw moves, control counts, z and s all matched within 1e-9.
  - A full scan of 559,260 control rows found:
    - 0 controls outside B's sector;
    - 0 controls equal to A or B;
    - 0 controls with any edge to A.
  - The 2022 embargo holds, with 0 finite exploration values past 2021-12-31.
- **Placebo.** A re-implementation of §13.15 using the registered seed reproduces the stored values exactly (p50 −0.012%, p95 +0.197%, coverage 2017, 9.53% of links dropped). With other seeds, p95 ranged from +0.17% to +0.21%, which is Monte-Carlo noise. The real mean (−0.08%) sits below the placebo median, and 69–71% of random pairs do at least as well.
- **Process order.** File timestamps, git history and the session transcript agree on this sequence:
  1. power files written, then committed (`fd58910`);
  2. exploration measured, then its readout committed (`16eb523`);
  3. H6-F1 registered (`a59f50e`);
  4. holdout unlocked;
  5. holdout events built.

  The exploration integrity table matches the research store byte for byte. Since `6bc906f` the committed JSON copies are stored byte-for-byte (`-text`), so `sha256` of the committed files equals the table.

## How to read the result (strongest honest statement)

When one of about 565 large US stocks makes a big move of its own (≥ 3σ), the stocks the map lists as its competitors move the same way **that day**, by more than random stocks from the same broad sector. Its suppliers do too, less reliably. Two things explain most of that same-day move:

- **Ordinary co-movement.** The linked stocks already track the shocked firm on ordinary days.
  - The linked stocks' pre-period residual correlation with A is 0.21–0.27, against 0.09 for the sector look-alikes.
  - Controls matched on correlation cut the same-day effect by about 40%; the most-correlated controls cut it by 75–85%.
- **Common news.** About half of the competitor effect comes from the ~10% of events where the linked firm had its own ≥ 3σ move on the same day.

**After the shock day, no role shows a reliable further drift.** No slow ripple at or above roughly 0.3–0.75% over 20 days passed the pre-registered tests, and those are the MDEs the power check showed the data could detect. The only exploration candidate (customers, days +1 to +5, +0.32%) reversed in the holdout (−0.08%).

**Decision:** stop the price-ripple trading path (Stage 2). This decision holds under either reading of the same-day effect, transmission or common factor, because nothing is left after day 0 at close-to-close resolution.

## What was NOT shown

- That the map's links carry information beyond ordinary return correlation, or that the same-day effect is causal A→B transmission. Close-to-close data cannot separate instant pricing from a common factor or simultaneous news.
- That slow effects **below ~0.5%** are absent. The pooled 20-day 95% CI for suppliers reaches about +0.54%.
- Anything about **news-defined events** (Test B), **intraday timing**, trading costs, or links as they existed historically (today's graph is applied to the past).
- That the holdout **competitor drift** (+0.17% / +0.25% / +0.48% over days +1..5 / +1..20 / +1..60) is real. It was not an exploration candidate, it misses the 50 bp floor, and it fails Holm across the 9 holdout cells (adjusted p ≈ 0.14–0.16). It is driven by IT up-shocks and about 10 large linked firms, and it vanishes under sector-ETF returns or correlation-matched controls. At most it is a hypothesis, and testing it needs a new pre-registration on genuinely new data; 2022–2026 is now spent.

## Data-quality findings (do not change the verdict)

- **Corporate-action artifacts pass as shocks.** The §13.3 bad-print rule catches only reverting spikes. Three non-reverting adjustment jumps became first-domino shocks and seeded about 14 of 55,926 events:
  - DHR on 2016-07-05 (Fortive spin-off: adjusted +61%, raw +3.6%; 5 events);
  - KDP on 2018-07-10 (merger special dividend; 7 events);
  - KT on 2023-12-28 (dividend ex-date; 2 events).

  This is negligible for the means. A future run should flag days where the adjusted and raw returns disagree by more than a few percent.
- **Nine symbols have no usable Yahoo history** (fewer than 200 rows, or no data): CAJ, ERJ, SATS, TEF, AVB, EA, EQR, PSKY, WBD. The cause (delisting, take-private, re-ticker) was not verified.
- The 5 manual `supplies` edges all involve 300750.SZ, which is excluded as a foreign listing (§13.12). So the R8 "manual only" supplier and customer subsets are empty by construction.

## Precision caveats

- **Clustering.** Same-day t-statistics of 15–21 overstate precision because the same linked firms recur; two-way (date × firm) t is 10–14. The supplier same-day exploration pass has a two-way t of 2.83 (holdout 3.02).
- **Pre-trends.** Two pre-trends sit just under the |t| < 2 bar: supplier 1.91 in exploration and competitor 1.90 in the holdout. Both would flag under firm clustering.
- **Supplier effective dates.** Supplier inference rests on few effective dates, about 22 by variance.
- **Projected power.** The projected full-chain power of 100% at 1.5% is calendar arithmetic from the exploration SE. The ~75% in §13.28 refers to a role whose exploration MDE is exactly 1.5%. The real MDEs are 0.30–0.75%.
