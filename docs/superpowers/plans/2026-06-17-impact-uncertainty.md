# Surface Uncertainty Implementation Plan (stream 2.3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Checkbox steps.

**Goal:** Attach a `confidence` (0–1) to every impact verdict and render it — low-confidence nodes drawn more transparent, with a confidence row + verifier note in the inspector.

**Architecture:** Backend sets `confidence` at scoring time in `_impact_row` (heuristic from hop + grounding) and `1.0` on seeds; `_verification_pass` (2.2) already overwrites verified nodes with the verifier's confidence. Frontend generalizes the existing Phase-K `is_estimated→0.70` alpha into a confidence ramp in `tintColor`/`tintColorRGB`, and the inspector shows the number.

**Tech Stack:** Python/pytest; TypeScript/Vitest. **Branch:** `feat/impact-uncertainty`. **Spec:** `docs/superpowers/specs/2026-06-17-impact-uncertainty-design.md`. Run Python with `python -B`.

---

## Task 1: Backend confidence assignment

**Files:** `api/impact.py`; tests `tests/test_impact_stream.py`.

- [ ] **Step 1: Failing tests** — append to `tests/test_impact_stream.py`:

```python
def test_heuristic_confidence_values():
    assert impact_mod._heuristic_confidence(1, True) == 0.70
    assert impact_mod._heuristic_confidence(1, False) == 0.85
    assert impact_mod._heuristic_confidence(3, True) == 0.45
    assert impact_mod._heuristic_confidence(3, False) == 0.60
    # never reads as near-certain for an unverified heuristic
    assert impact_mod._heuristic_confidence(1, False) <= 0.90


def test_every_verdict_has_confidence(conn):
    r = _run_done(conn)  # autouse echo fake; no verifier adjudication
    for v in r["impacts"]:
        if v.get("is_seed"):
            assert v["confidence"] == 1.0
        elif v["direction"] in ("positive", "negative"):
            assert isinstance(v["confidence"], (int, float)) and 0.0 < v["confidence"] <= 1.0
        elif v["direction"] == "unscored":
            assert v["confidence"] == 0.0
        else:  # no_effect
            assert v.get("confidence") is None


def test_verifier_confidence_overwrites_heuristic(conn, monkeypatch):
    def fake(prompt):
        if "TRY TO REFUTE" in prompt:
            ids = re.findall(r"^NODE:\s*(\S+)\s*\(", prompt, re.MULTILINE)
            return json.dumps([{"node_id": i, "verdict": "upheld",
                                "confidence": 0.33, "reasoning": "t"} for i in ids])
        return _fake_llm(prompt)
    monkeypatch.setattr(impact_mod, "_llm_call", fake)
    monkeypatch.setattr(impact_mod, "MAX_FRONTIER", 9999)
    monkeypatch.setattr(impact_mod, "VERIFY_MAG_THRESHOLD", 0.45)
    r = _run_done(conn)
    verified = [v for v in r["impacts"] if v.get("verified")]
    assert verified and all(abs(v["confidence"] - 0.33) < 1e-9 for v in verified)
```

- [ ] **Step 2: Run — fail** (`_heuristic_confidence` missing; no `confidence` key).
Run: `python -B -m pytest tests/test_impact_stream.py -k "confidence" -v`

- [ ] **Step 3: Add `_heuristic_confidence`** near `_impact_row` in `api/impact.py`:

```python
def _heuristic_confidence(hop: int, is_estimated: bool) -> float:
    """Rough confidence proxy for a verdict the adversarial verifier did NOT
    adjudicate: deeper hops are less certain; an SEC-grounded inbound edge lifts
    it. Capped at 0.90 — an unverified heuristic must never read as near-certain."""
    base = {1: 0.70, 2: 0.55, 3: 0.45}.get(hop, 0.35)
    if not is_estimated:
        base += 0.15
    return round(min(0.90, base), 2)
```

- [ ] **Step 4: Set `confidence` in `_impact_row`** — change the function to compute and include it:

```python
def _impact_row(nb: dict[str, Any], hop: int, direction: str,
                magnitude: float, reasoning: str) -> dict[str, Any]:
    """Build one impacts[] entry for a scored or unscored ring candidate.
    Centralised so the scored and unscored branches can never drift."""
    ew = nb.get("edge_weight")
    if direction == "unscored":
        confidence: Optional[float] = 0.0
    elif direction in ("positive", "negative"):
        confidence = _heuristic_confidence(hop, ew is None)
    else:  # no_effect — not displayed, no confidence
        confidence = None
    return {
        "node_id": nb["id"],
        "name": nb["name"],
        "type": nb["type"],
        "direction": direction,
        "magnitude": magnitude,
        "hop": hop,
        "reasoning": reasoning,
        "via_parent": nb["via_parent"],
        "edge_type": nb["edge_type"],
        "country": nb.get("country"),
        "edge_weight": ew,
        "edge_source_tier": nb.get("edge_source_tier"),
        "is_estimated": ew is None,
        "confidence": confidence,
    }
```
(`Optional` is already imported in this module.)

- [ ] **Step 5: Set seed confidence = 1.0** — in `run_impact_stream`'s hop-0 init loop where each seed's `impacts[nid] = {...}` dict is built (the one with `"is_seed": True`), add `"confidence": 1.0,` to that dict.

- [ ] **Step 6: Run — pass.** `python -B -m pytest tests/test_impact_stream.py -v` (prior + 3 new all pass). Then `python -B -m pytest tests/ -q` (only the pre-existing Walmart failure).

- [ ] **Step 7: Commit**
```bash
git add api/impact.py tests/test_impact_stream.py
git commit -m "feat(impact): assign per-verdict confidence (heuristic + seed 1.0; verifier overwrites)"
```

---

## Task 2: Frontend confidence-driven tint

**Files:** `web/src/impact.ts`; test `web/src/__tests__/impact-tint.test.ts`.

- [ ] **Step 1: Failing tests** — append to `web/src/__tests__/impact-tint.test.ts`:

```ts
  it("tintColor alpha rises with confidence", () => {
    const low = tintColor(v({ direction: "negative", magnitude: 0.6, hop: 1, confidence: 0.2 }))!;
    const high = tintColor(v({ direction: "negative", magnitude: 0.6, hop: 1, confidence: 0.95 }))!;
    expect(low.startsWith("rgba(")).toBe(true);            // faded
    // crude alpha extraction
    const lowA = parseFloat(low.slice(low.lastIndexOf(",") + 1));
    expect(lowA).toBeLessThan(0.6);
    // high confidence → effectively opaque (rgb or alpha ~1)
    const highOpaque = high.startsWith("rgb(") ||
      parseFloat(high.slice(high.lastIndexOf(",") + 1)) > 0.9;
    expect(highOpaque).toBe(true);
  });

  it("tintColor falls back to legacy is_estimated alpha when confidence absent", () => {
    const c = tintColor(v({ direction: "negative", magnitude: 0.6, hop: 1,
                            is_estimated: true, edge_weight: null }));
    expect(c).not.toBeNull();   // no crash; legacy 0.70 alpha path
  });

  it("tintColorRGB is greyer for low confidence than high", () => {
    const lo = tintColorRGB(v({ direction: "negative", magnitude: 0.6, confidence: 0.1 }))!;
    const hi = tintColorRGB(v({ direction: "negative", magnitude: 0.6, confidence: 0.95 }))!;
    // low-confidence blends toward grey → higher blue channel for the red palette
    expect(lo.b).toBeGreaterThan(hi.b);
  });
```

- [ ] **Step 2: Run — fail.** `cd web && npm run test -- impact-tint`

- [ ] **Step 3: Confidence ramp in `tintColor`** — replace the final alpha line (currently
`const alpha = verdict.is_estimated !== false && verdict.edge_weight == null ? 0.70 : 1.0;`) with:

```ts
  const conf = typeof verdict.confidence === "number" ? verdict.confidence : null;
  const alpha = conf !== null
    ? 0.4 + 0.6 * Math.max(0, Math.min(1, conf))     // conf 0→0.4 faint, 1→opaque
    : (verdict.is_estimated !== false && verdict.edge_weight == null ? 0.70 : 1.0);
```
(The `unscored` early-return stays above this, untouched.)

- [ ] **Step 4: Confidence blend in `tintColorRGB`** — replace the estimated-grey block
(currently `if (verdict.is_estimated !== false && verdict.edge_weight == null) { const blend = 0.70; ... }`) with:

```ts
  const conf = typeof verdict.confidence === "number" ? verdict.confidence : null;
  const blend = conf !== null
    ? 0.4 + 0.6 * Math.max(0, Math.min(1, conf))
    : (verdict.is_estimated !== false && verdict.edge_weight == null ? 0.70 : 1.0);
  if (blend < 1) {
    const grey = 0.18;
    return {
      r: c.r / 255 * blend + grey * (1 - blend),
      g: c.g / 255 * blend + grey * (1 - blend),
      b: c.b / 255 * blend + grey * (1 - blend),
    };
  }
  return { r: c.r / 255, g: c.g / 255, b: c.b / 255 };
```

- [ ] **Step 5: Run — pass + tsc.** `cd web && npm run test && npx tsc --noEmit`

- [ ] **Step 6: Commit**
```bash
git add web/src/impact.ts web/src/__tests__/impact-tint.test.ts
git commit -m "feat(web): fade impact nodes by confidence (2D alpha + globe grey-blend)"
```

---

## Task 3: Inspector confidence row + verifier note + unscored label

**Files:** `web/src/ui/inspector.ts`, `web/src/styles.css`.

- [ ] **Step 1: Fix the direction label/class for `unscored`** — in `showNode`'s impact block, where `dirClass`/`dirLabel` are computed, extend:
```ts
    const dirClass = v.direction === "positive" ? "impact-pos"
      : v.direction === "negative" ? "impact-neg"
      : v.direction === "unscored" ? "impact-unscored" : "impact-neutral";
    const dirLabel = v.direction === "positive" ? "POSITIVE"
      : v.direction === "negative" ? "NEGATIVE"
      : v.direction === "unscored" ? "UNSCORED" : "NO EFFECT";
```

- [ ] **Step 2: Add the confidence row + verifier line** — after the `impact-header` div is appended (and near the existing exposure badge), add:
```ts
    if (typeof v.confidence === "number") {
      box.appendChild(el("div", { class: "impact-confidence" },
        el("span", { class: "conf-badge" }, `confidence ${Math.round(v.confidence * 100)}%`),
        el("span", { class: "conf-source" }, v.verified ? "· verified" : "· est."),
      ));
    }
    if (v.verification) {
      box.appendChild(el("p", { class: "impact-verify" },
        `Verifier: ${v.verification.verdict} — ${v.verification.reasoning}`));
    }
```

- [ ] **Step 3: Add CSS** — in `web/src/styles.css` after the `.exposure-source` rule (~line 1239):
```css
.impact-confidence {
  display: flex;
  align-items: center;
  gap: 5px;
  margin: 2px 0 6px 0;
}
.conf-badge {
  display: inline-block;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.04em;
  padding: 2px 6px;
  border-radius: 3px;
  background: rgba(124, 134, 150, 0.16);
  color: #aab4c2;
  border: 1px solid rgba(124, 134, 150, 0.3);
}
.conf-source { font-size: 10px; color: var(--text-faint); }
.impact-verify {
  font-size: 11px;
  color: var(--text-faint);
  margin: 2px 0 6px 0;
  font-style: italic;
}
```

- [ ] **Step 4: Type-check + tests** — `cd web && npx tsc --noEmit && npm run test` (all green; no inspector unit test exists — tsc + manual structure is the gate, consistent with the repo).

- [ ] **Step 5: Commit**
```bash
git add web/src/ui/inspector.ts web/src/styles.css
git commit -m "feat(web): inspector shows verdict confidence + verifier note; label unscored"
```

---

## Self-Review

- Backend heuristic + per-verdict confidence + seed 1.0 + verifier-overwrite → Task 1. ✓
- Confidence-driven 2D alpha + globe grey-blend, with legacy fallback → Task 2. ✓
- Inspector confidence row + verifier line + unscored label fix → Task 3. ✓
- Tests: heuristic values, population per direction, verifier-overwrite (backend); alpha-ramp + fallback + globe-grey (frontend) → Tasks 1–2. ✓
- No contract change beyond populating `confidence` (typed optional in 2.2). ✓
- Placeholder scan: full code in every code step; no TBD. ✓
- Naming consistency: `_heuristic_confidence`, `confidence`, `conf`/`alpha`/`blend`, class names `impact-confidence`/`conf-badge`/`conf-source`/`impact-verify` consistent across backend, frontend, CSS. ✓
