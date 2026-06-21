# "So What?" Verdict Verification — Design Spec

**Date:** 2026-06-17
**Status:** Approved autonomously (user delegated 2.2–2.4 AFK with judgment calls)
**Work stream:** 2 of 4 (Correctness & trust), sub-project 2 of 4
**Branch:** `feat/impact-verdict-verification` (stacked on `feat/impact-scoring-coverage`)

---

## Context

Each BFS hop scores candidates with a single LLM call producing
`direction + magnitude + reasoning`. Nothing checks those verdicts — the model's
top failure mode is a confident, plausible-but-unsupported strong call (e.g.
"this distant supplier is +0.8" with no real causal basis). This sub-project adds
an **adversarial verification pass** that tries to *refute* the high-impact
verdicts and downgrades the ones that don't hold up, so hallucinated positives
don't survive into the conclusion. It also attaches a per-verdict verifier
`confidence`, which sub-project 2.3 will surface in the UI.

### Autonomous design decisions (would normally be brainstormed with the user)
- **Adversarial refutation, not N-way self-consistency voting.** A single skeptical
  "try to refute this" pass over the *high-impact subset* is bounded (~handful of
  batched calls) and directly targets the false-positive failure mode. N-way
  voting on every node would multiply cost across the whole ring. (Majority-of-K
  on the very strongest verdicts is noted as a future extension.)
- **Verify only the verdicts that matter:** `direction in (positive, negative)` AND
  `magnitude >= VERIFY_MAG_THRESHOLD`, capped at `VERIFY_MAX_NODES`, highest
  magnitude first. Seeds, `no_effect`, and `unscored` are not verified (nothing to
  refute / they're not driving a false conclusion).
- **Verification only downgrades or annotates — never upgrades.** Anti-hallucination
  is about removing unsupported positives; a refuter shouldn't manufacture new
  impact. `refuted → no_effect`; `weakened → magnitude × 0.5`; `upheld → unchanged`.
- **Runs after refinement, before `done`** — mirrors `_refinement_pass`, reuses the
  batch-LLM structure, and rides the existing post-hoc re-tint path (a new
  `verification` stream event, like `refinement`).
- **Gated by `VERIFY_ENABLED` (default on)** so it can be disabled for speed.

### Non-goals
- The confidence *UI* (fading low-confidence nodes, inspector display) — that's 2.3.
- Backtesting/accuracy measurement — that's 2.4.
- Re-verifying weak/no_effect/unscored verdicts.
- Touching the refinement pass, scoring coverage (2.1), or streaming (1) contracts
  beyond additive fields.

---

## Section 1 — Backend: the verification pass (`api/impact.py`)

A new `_verification_pass(*, text, impacts, seeds_block, conn, debug_log) -> dict`,
structured like `_refinement_pass`:

1. **Select** eligible verdicts: not `is_seed`, `direction in ("positive","negative")`,
   `magnitude >= VERIFY_MAG_THRESHOLD` (default 0.45). Sort by magnitude desc, cap at
   `VERIFY_MAX_NODES` (default 24).
2. **Build per-node context blocks** (reuse the chain context the refinement pass
   already assembles — the node's impacted neighbours + its own verdict + sector/country),
   pack into batches of `VERIFY_BATCH_SIZE` (default 6).
3. **Adversarial prompt** (`_VERIFY_BATCH_PROMPT_TEMPLATE`): "You are a skeptical
   economist. For each node, TRY TO REFUTE the claimed impact given the news and the
   node's actual position. Return `upheld` only if the causal path is concrete and
   defensible; `weakened` if real but overstated; `refuted` if the claim is
   speculative, geographically implausible, or unsupported." Output per node:
   `{node_id, verdict: "upheld"|"weakened"|"refuted", confidence: 0.0-1.0, reasoning}`.
4. **Apply** (mutating `impacts`), recording the original under a `verification` key:
   - `refuted` → `direction = "no_effect"`, `magnitude = 0.0`.
   - `weakened` → `magnitude *= 0.5`.
   - `upheld` → unchanged.
   - In all three, set `impacts[nid]["confidence"] = <verifier confidence>` and
     `impacts[nid]["verification"] = {verdict, confidence, reasoning}`, and set
     `impacts[nid]["verified"] = True`.
5. Return a summary `{checked, upheld, weakened, refuted}`.

**Integration** in `run_impact_stream` (after the `refinement` event, before `done`):

```python
verification_summary = _verification_pass(text=text, impacts=impacts,
                                          seeds_block=seeds_block, conn=conn,
                                          debug_log=debug_log) if VERIFY_ENABLED else \
    {"checked": 0, "upheld": 0, "weakened": 0, "refuted": 0}
yield {"event": "verification",
       "updated": [v for v in impacts.values() if v.get("verified")],
       "summary": verification_summary}
```
and add `"verification": verification_summary` to the `done` `result`.

**Config:** `VERIFY_ENABLED` (env `IMPACT_VERIFY`, default "1"), `VERIFY_MAG_THRESHOLD`
(`IMPACT_VERIFY_MAG`, 0.45), `VERIFY_MAX_NODES` (`IMPACT_VERIFY_MAX`, 24),
`VERIFY_BATCH_SIZE` (`IMPACT_VERIFY_BATCH`, 6).

**Notes:**
- `_verification_pass` shares the parallel-LLM + tolerant-parse shape; it may reuse
  `_collect_verdicts`-style parsing but keyed to the verify schema (a small local
  parse is fine — it returns `{node_id: {verdict, confidence, reasoning}}`).
- A failed/empty verifier batch leaves those verdicts UNCHANGED (fail-open — we do
  not drop or downgrade a verdict we couldn't verify; that would trade one silent
  error for another). Counts reflect only nodes actually adjudicated.
- `confidence` is added ONLY to verified nodes here; the global confidence model for
  all nodes is 2.3.

---

## Section 2 — Schema / contract

Additive only:
- `done.result` gains `"verification": {checked, upheld, weakened, refuted}`.
- A new stream event `{ "event":"verification", "updated":[...], "summary":{...} }`
  between `refinement` and `done`.
- Verified verdicts gain `verified: true`, `confidence: float`, and
  `verification: {verdict, confidence, reasoning}`.
- `/impact` (non-streaming) carries `verification` in its result (same code path).
- TS: `ImpactVerdict` gains optional `verified?`, `confidence?`,
  `verification?: { verdict: "upheld"|"weakened"|"refuted"; confidence: number; reasoning: string }`.
  `ImpactResponse` gains optional `verification?: { checked; upheld; weakened; refuted }`.
  `ImpactStreamEvent` gains a `verification` variant.
- `runImpactStream`'s reader needs no change (it already forwards every event to
  `onEvent`); the consumer (`main.ts`) gains a `verification` branch that merges
  `updated` into the accumulator + reapply, exactly like `refinement` (see Section 3).

---

## Section 3 — Frontend (minimal, this sub-project)

This sub-project is backend-weighted; the only required frontend change keeps the
live trace correct when verdicts get downgraded mid-stream:
- `web/src/main.ts` `handleImpactRun` streaming `onEvent`: add a `verification`
  branch that does `ev.updated.forEach(v => acc.set(v.node_id, v)); reapply(false)`
  — identical to the `refinement` branch. (Refuted nodes flip to `no_effect`, so on
  re-tint they correctly drop out of the lit set.)
- Status line (optional): append `· N refuted` when `resp.verification?.refuted > 0`.

No tint/color change here — confidence rendering is 2.3. The refuted→no_effect
nodes simply stop being highlighted, which is the correct visible effect.

---

## Section 4 — Testing

**Backend** (extend `tests/test_impact_stream.py`), deterministic fakes that route on
the verify prompt (a distinct marker string in `_VERIFY_BATCH_PROMPT_TEMPLATE`, e.g.
"TRY TO REFUTE"):
- **Refute downgrades:** a fake verifier that returns `refuted` for a chosen strong
  node → assert that node's final `direction == "no_effect"`, `magnitude == 0`, and it
  carries `verification.verdict == "refuted"`; `summary.refuted >= 1`.
- **Weaken halves magnitude:** verifier returns `weakened` → final magnitude == original × 0.5,
  `verification.verdict == "weakened"`.
- **Upheld unchanged:** verifier returns `upheld` → direction/magnitude unchanged, but
  `verified == True` and `confidence` set.
- **Only strong verdicts checked:** with all ring verdicts at magnitude 0.5 and
  `VERIFY_MAG_THRESHOLD` patched to 0.9, `summary.checked == 0` and no node is verified.
- **Fail-open:** verifier returns empty/garbage → no verdict changes; `checked` counts
  only adjudicated nodes (0 here).
- **Disabled:** `VERIFY_ENABLED=False` → `verification` event/ summary all zero, no calls.
- **Event ordering:** `seeds → hop* → refinement → verification → done` (update the
  existing ordering test).
- **No regression:** reconcile + wrapper-equivalence tests still pass (verification is
  additive; `verification` rides in both event and done).

**Frontend** (extend `web/src/__tests__/impact-stream.test.ts`): a stream containing a
`verification` event whose `updated` flips a node to `no_effect` is applied in order and
the `done` result still resolves.

---

## Files touched

| File | Change |
|---|---|
| `api/impact.py` | `_VERIFY_*` config; `_VERIFY_BATCH_PROMPT_TEMPLATE`; `_verification_pass`; integration + `verification` event + done field |
| `tests/test_impact_stream.py` | refute/weaken/uphold/threshold/fail-open/disabled/ordering tests |
| `web/src/api.ts` | `verified?`/`confidence?`/`verification?` on verdict; `verification?` on response; `verification` stream event |
| `web/src/main.ts` | `verification` onEvent branch (merge+reapply); optional status `· N refuted` |
| `web/src/__tests__/impact-stream.test.ts` | verification-event application test |

No schema/DB/pipeline changes. `/impact`, `/impact/multi`, the stream-1 event contract,
the 2.1 scoring contract, and the archive change only by additive fields.
