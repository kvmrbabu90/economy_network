# LLM minimization — measured findings & decisions

**Date:** 2026-07-07
**Tooling:** `scripts/ab_quality.py` (live A/B harness) + `pipeline/quality_metrics.py` (pure metrics, unit-tested). LLM responses cached to `scripts/.ab_cache.json`.

## The headline finding

**The LLM hop-scorer is only ~94% self-consistent on direction.** Tracing the *same* config twice and diffing the impact maps yields **direction agreement 0.941** (over 169 shared nodes). Therefore "98% of LLM quality" **on the direction axis is not achievable** — the reference is not 98% consistent with itself. The achievable ceiling on direction is ~94%. The deterministic axes (which nodes; materiality) are a different story — see below.

## What was measured

### 1. Materiality gate — the prior cannot replace the LLM
- Among 40 live GKG candidates (all high-prior, post pre-cap), the LLM gate **dropped 27 (68%) as non-material**. The prior does not predict the LLM's keep decision.
- The gate is a **single batched call** regardless of how many candidates enter it → prior-based auto-keep saved **zero** calls while tracing 27 noise events.
- **Decision:** the prefilter now **defers to the LLM** for all GKG/RSS candidates (materiality agreement → **1.000**); only the 8-K sentinel auto-keeps (definitionally material); clear low-prior noise auto-drops. Shipped.

### 2. Seeds — deterministic vs LLM extraction
- LLM headline-extraction returns **[]** for most GKG events (they are article-driven: "Kospi tumbles 6%", "BIOHK conference"). The old path fell back to the primary GKG seed anyway.
- **End-to-end impact-map A/B (full seed set):** node set **jaccard 1.000** — deterministic seeds reproduce the exact set of impacted nodes.

### 3. Seed-cap trade-off — no free lunch (both corners measured)
| strategy | node coverage | direction agreement | LLM cost |
|---|---|---|---|
| full GKG seed set (`PRECOMPUTE_SEED_CAP=0`) | **1.000** | 0.633 (secondaries get direct verdicts) | higher (bigger frontier) |
| primary only (`=1`, default) | 0.839 | **0.940** (= noise floor) | lower |

- Neither corner is within 2% on *both* axes. Primary-only puts direction at the LLM's own ceiling but loses secondary-org neighborhoods; full-set reproduces coverage exactly but changes ~30% of directions (the secondary named orgs get *direct* scores instead of propagated ones — arguably more accurate, but unverifiable from agreement alone).
- **Decision:** default `PRECOMPUTE_SEED_CAP=1` (primary) — keeps the safety-critical direction axis at the LLM ceiling; configurable to `0` for full coverage. Shipped.

### 4. Stranded-parent fallback — reliability + noise
- **12% of seeds have 0 above-threshold edges** (google, instagram, linkedin). Their traces chase the below-threshold co-mention ring; google's 72-edge ring **times out at 100s** and returns an empty, degraded hop.
- **Decision:** cap the stranded ring to the top-K by weight (`IMPACT_STRANDED_FALLBACK_CAP=8`). A completed capped hop is strictly better than a timeout that returns nothing. Shipped (+test).

### 5. Tone → seed-direction — evaluated and REJECTED
- Direction is the noise-limited axis (~94% ceiling); the seed direction sets the whole propagation axis. Article-level GKG tone is a strictly worse signal than the single `_score_seed_set` call it would replace (e.g. "OPEC+ raises output" reads neutral/positive in tone but is negative for an oil producer). Poor risk/reward for saving 1 call/event. Harness `--tone` mode is available if we want the empirical number later.

## Net effect

- **LLM calls per traced GKG event: ~5 → ~2–3** (skip entity-extraction + commodity re-discovery; one batched seed score; theme-gated commodity; hops unchanged).
- **Materiality precision restored** (no auto-keep noise flooding the trace queue).
- **No timeout-degraded traces** (stranded-ring cap).
- **Deterministic axes preserved:** which-nodes and materiality reproduce the LLM at ~1.0; direction sits at the LLM's own ~0.94 self-consistency ceiling.

## The honest bottom line

"98% of LLM quality" splits by axis:
- **Materiality & node coverage (single-seed events): ~100% preserved** — these are deterministic.
- **Direction: ~94% is the ceiling** because the LLM itself is only 94% self-consistent; our path sits at that ceiling (primary-cap).
- **Multi-seed coverage** is a tunable trade-off (`PRECOMPUTE_SEED_CAP`) with no option inside 2% on both coverage and direction simultaneously.

All changes are behind fail-open flags (`TRUST_KNOWN_SEEDS`, `INGEST_MATERIALITY_RULES`, `PRECOMPUTE_SEED_CAP`, `IMPACT_STRANDED_FALLBACK_CAP`) — any regression is an env-var revert.
