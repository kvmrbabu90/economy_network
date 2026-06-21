# "So What?" Surface Uncertainty — Design Spec

**Date:** 2026-06-17
**Status:** Approved autonomously (user delegated 2.2–2.4 AFK)
**Work stream:** 2 of 4 (Correctness & trust), sub-project 3 of 4
**Branch:** `feat/impact-uncertainty` (stacked on `feat/impact-verdict-verification`)

---

## Context

Today every impact verdict reads as equally authoritative — a node is just a
color, whether the engine was sure or guessing. This sub-project attaches a
`confidence` (0–1) to every verdict and renders it, so a solid call is visually
distinct from a shaky one. It builds directly on 2.2: verified verdicts already
carry the verifier's confidence; this fills in confidence for the rest and
surfaces it everywhere.

### Autonomous design decisions
- **Two confidence sources, clearly distinguished:** verified verdicts (2.2) use
  the *verifier's* confidence; everything else gets a transparent *heuristic*
  (hop distance + grounding), shown in the inspector as "est." vs "verified".
- **Confidence is assigned at scoring time** (in `_impact_row`), so it rides on the
  per-hop stream events and the live reveal can shade by certainty — not bolted on
  only at the end. Verification overwrites verified nodes' confidence afterward.
- **Render as opacity, generalizing the existing Phase K precedent** (estimated
  nodes were already drawn at 0.70 alpha). Low confidence → more transparent, with
  a visibility floor so nothing vanishes. This reuses the one place colors live
  (`impact.ts` `tintColor`/`tintColorRGB`) so 2D Sigma and the 3D globe both pick it
  up.
- **Heuristic (deliberately simple and honest):**
  `base = {1:0.70, 2:0.55, 3:0.45}[hop]` (deeper hop = less certain), `+0.15` when
  the inbound edge is SEC-grounded (`is_estimated == False`), capped at 0.90 (an
  unverified heuristic should never read as near-certain). Seeds = 1.0 (straight
  from the news / named entity). `unscored` = 0.0. `no_effect` = unset (hidden).

### Non-goals
- Changing how verdicts are produced or verified (1, 2.1, 2.2).
- A learned/calibrated confidence model — the heuristic is explicitly a proxy;
  calibration against outcomes is 2.4's territory.
- Per-hop *live* re-shading beyond what falls out of confidence being on the
  verdicts (the final reapply already re-renders with confidence).

---

## Section 1 — Backend: assign confidence (`api/impact.py`)

- Add `_heuristic_confidence(hop: int, is_estimated: bool) -> float`:
  ```python
  def _heuristic_confidence(hop: int, is_estimated: bool) -> float:
      base = {1: 0.70, 2: 0.55, 3: 0.45}.get(hop, 0.35)
      if not is_estimated:
          base += 0.15           # SEC-grounded inbound edge
      return round(min(0.90, base), 2)
  ```
- In `_impact_row(nb, hop, direction, magnitude, reasoning)` set `confidence`:
  - `unscored` → `0.0`
  - `positive`/`negative` → `_heuristic_confidence(hop, ew is None)`
  - `no_effect` → `None` (not displayed)
  Add `"confidence": <that>` to the returned dict.
- Seed impact rows (the hop-0 init loop) set `confidence = 1.0`.
- `_verification_pass` already overwrites `confidence` for verified nodes — no
  change there; ordering (verification after scoring) means verified nodes end with
  the verifier's number, others keep the heuristic. (Confidence is set before
  verification runs, so verified nodes are correctly overwritten.)

No new event or summary; `confidence` is already an optional field on the verdict
(added to the TS type in 2.2). It is now populated for all non-`no_effect` verdicts.

---

## Section 2 — Frontend: render confidence

**`web/src/impact.ts`** — confidence-driven alpha, generalizing the current
`is_estimated → 0.70` rule:
- In `tintColor` (the positive/negative path, after tier selection), compute:
  ```ts
  const conf = typeof verdict.confidence === "number" ? verdict.confidence : null;
  const alpha = conf !== null
    ? 0.4 + 0.6 * Math.max(0, Math.min(1, conf))   // conf 0→0.4 faint, 1→1 opaque
    : (verdict.is_estimated !== false && verdict.edge_weight == null ? 0.70 : 1.0); // pre-confidence archives
  ```
  (Floor 0.4 keeps low-confidence nodes visible.) The `unscored` branch stays first
  and unchanged (its slate marker is not confidence-modulated).
- In `tintColorRGB` (globe), replace the estimated-grey blend with a
  confidence-driven blend toward neutral grey: `blend = conf !== null ? (0.4 + 0.6*conf)
  : (is_estimated ? 0.70 : 1.0)`, then `c*blend + grey*(1-blend)`.

**`web/src/ui/inspector.ts`** — in the impact-box:
- Add a confidence row when `typeof v.confidence === "number"`:
  `confidence {Math.round(v.confidence*100)}%` + a source tag `· verified` (when
  `v.verified`) or `· est.`.
- When `v.verification` is present, add a line: `Verifier: {verdict} — {reasoning}`.
- Fix the direction label/class to handle `"unscored"` → label `UNSCORED`, a neutral
  class (currently unscored falls through to "NO EFFECT", which is wrong).

**CSS:** add minimal styles for the new classes (`impact-confidence`, `conf-badge`,
`conf-source`, `impact-verify`) next to the existing `impact-exposure`/`exposure-badge`
rules (same file those live in).

---

## Section 3 — Testing

**Backend** (extend `tests/test_impact_stream.py`):
- `_heuristic_confidence`: hop-1 estimated = 0.70, hop-1 grounded = 0.85, hop-3
  estimated = 0.45, capped at 0.90.
- A run: every non-seed `positive`/`negative` verdict has a `confidence` in (0,1];
  every seed has `confidence == 1.0`; `unscored` have `0.0`; `no_effect` have
  `confidence is None`.
- Verified-overwrite: with a verify fake returning `confidence 0.33` for a strong
  node, that node's final `confidence == 0.33` (verifier wins over heuristic).

**Frontend** (extend `web/src/__tests__/impact-tint.test.ts`):
- `tintColor` alpha increases with confidence: a `confidence: 0.2` positive verdict
  yields a more transparent `rgba(...)` (alpha < 0.6) than a `confidence: 0.95` one
  (alpha > 0.9 → `rgb(...)` or alpha≈1).
- Missing confidence falls back to the legacy `is_estimated` behavior (no crash).
- `tintColorRGB` returns a greyer color for low confidence than high.

No new e2e LLM trace required for 2.3 (it's rendering of an existing field); a
backend unit check that confidence is populated + the frontend tint tests suffice.
A type-check + full web test run is the gate.

---

## Files touched

| File | Change |
|---|---|
| `api/impact.py` | `_heuristic_confidence`; set `confidence` in `_impact_row` + seed rows |
| `tests/test_impact_stream.py` | confidence-population + verified-overwrite tests |
| `web/src/impact.ts` | confidence-driven alpha in `tintColor`/`tintColorRGB` |
| `web/src/ui/inspector.ts` | confidence row + verifier line + unscored label fix |
| `web/src/styles.css` (or wherever `impact-*` styles live) | styles for new classes |
| `web/src/__tests__/impact-tint.test.ts` | confidence→alpha tests |

Additive only; no schema/DB/pipeline changes; no contract changes beyond populating
the already-typed `confidence` field.
