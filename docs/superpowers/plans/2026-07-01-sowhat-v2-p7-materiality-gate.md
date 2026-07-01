# So What? V2 · P7 — Materiality Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add an LLM materiality pre-filter to ingestion so only concrete, deterministic market-moving / business-impact events get queued for the impact trace. (Hourly cadence is a separate ops/config step, not in this plan.)

**Architecture:** One batched `_claude_call` over the fresh, resolved, deduped candidates in `run_ingest`, before `rank`/`cap`. Fail-open, toggleable.

**Tech Stack:** Python, pytest. Run `python -B`. Design: `docs/superpowers/specs/2026-07-01-sowhat-v2-p7-materiality-gate-hourly-design.md`.

---

### Task 1: `_materiality_filter` + wire into `run_ingest`

**Files:**
- Modify: `pipeline/ingest_news.py`
- Test: `tests/test_ingest_fetchers.py` (append; it already imports `pipeline.ingest_news as ing` and monkeypatches `ing._claude_call`)

Context (read `pipeline/ingest_news.py` first): `_claude_call` + `_parse_llm_json` are imported from `api.impact` (line ~138) and monkeypatched in tests. `extract_rss_events` (line ~249) shows the batch-prompt + tolerant-index parse idiom to mirror. `run_ingest` (line ~282) does: fetch → resolve (gate unresolvable) → `fresh = dedupe(resolved, conn)` → `ranked = cap(rank(fresh, conn))` → `insert_event`. Candidate dicts have `headline`, `seed_entity`, `seed_node_id`, `source`, `category`, `url`, `published_at`, `id`.

- [ ] **Step 1: Write failing tests** (append to `tests/test_ingest_fetchers.py`)

```python
def _cand(i, headline, entity="Acme"):
    return {"headline": headline, "seed_entity": entity, "seed_node_id": f"cik:{i}",
            "source": "SEC 8-K", "url": f"u/{i}", "category": "m&a", "published_at": "2026-07-01", "id": f"e{i}"}


def test_materiality_gate_keeps_only_material(monkeypatch):
    cands = [_cand(1, "Acme acquires rival for $5B"), _cand(2, "Why Acme stock could rise"),
             _cand(3, "Acme wins $2B defense contract")]
    # LLM marks 1 and 3 material, 2 not.
    monkeypatch.setattr(ing, "_claude_call", lambda p: json.dumps([
        {"index": 1, "material": True}, {"index": 2, "material": False}, {"index": 3, "material": True}]))
    out = ing._materiality_filter(cands)
    assert [c["id"] for c in out] == ["e1", "e3"]          # order preserved, non-material dropped


def test_materiality_gate_failopen(monkeypatch):
    cands = [_cand(1, "x"), _cand(2, "y")]
    monkeypatch.setattr(ing, "_claude_call", lambda p: "")   # garbage/empty → keep all
    assert ing._materiality_filter(cands) == cands


def test_materiality_gate_toggle_off(monkeypatch):
    cands = [_cand(1, "x")]
    def boom(p):
        raise AssertionError("gate must not call the LLM when disabled")
    monkeypatch.setattr(ing, "_claude_call", boom)
    monkeypatch.setenv("INGEST_MATERIALITY_GATE", "0")
    assert ing._materiality_filter(cands) == cands


def test_materiality_gate_empty(monkeypatch):
    def boom(p):
        raise AssertionError("no call on empty input")
    monkeypatch.setattr(ing, "_claude_call", boom)
    assert ing._materiality_filter([]) == []
```

Run: `python -B -m pytest tests/test_ingest_fetchers.py -k materiality -v` → FAIL (no `_materiality_filter`). (Ensure `import json` is present in the test file — it is.)

- [ ] **Step 2: Implement** — add near `_RSS_EXTRACT_PROMPT`/`extract_rss_events` in `pipeline/ingest_news.py`:

```python
_MATERIALITY_PROMPT = """You are a markets analyst gatekeeping a supply-chain impact graph.
For EACH numbered item decide: is it a CONCRETE, DETERMINISTIC market-moving / business-impact
event with a clear directional effect on a company, commodity, or region?

KEEP (material=true): M&A, contracts won/lost, output/production cuts, regulatory
approval/ban/recall, tariffs/sanctions, earnings or guidance surprises, supply disruptions,
plant/mine closures, defaults, large capex/JV, major executive departures.

DROP (material=false): opinion / analysis / "how/why" explainers, price-move-only stories
("stock rises 3%"), analyst rating or price-target changes, rumor / "could/may/reportedly",
routine product launches, and celebrity/sports/lifestyle.

ITEMS (numbered):
{items}

Return ONLY a JSON array (no prose):
[{{"index": <n>, "material": true|false}}]
"""


def _materiality_filter(cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only candidates the LLM judges to be concrete, deterministic market-moving /
    business-impact events. One batched call. Fail-open (empty/garbled → keep all).
    Disabled when INGEST_MATERIALITY_GATE='0'."""
    if not cands:
        return cands
    if os.environ.get("INGEST_MATERIALITY_GATE", "1") == "0":
        return cands
    block = "\n".join(f"{i+1}. {c.get('headline','')} — {c.get('seed_entity','')}"
                      for i, c in enumerate(cands))
    parsed = _parse_llm_json(_claude_call(_MATERIALITY_PROMPT.format(items=block)))
    if not isinstance(parsed, list):
        log.warning("materiality gate: unparseable LLM output — keeping all %d", len(cands))
        return cands
    keep_idx = set()
    for h in parsed:
        if isinstance(h, dict) and h.get("material") is True:
            i = h.get("index")
            if isinstance(i, int) and 1 <= i <= len(cands):
                keep_idx.add(i - 1)
    if not keep_idx:
        # A parsed-but-empty keep-set is ambiguous (all-noise vs. a bad response).
        # Fail-open to avoid silently zeroing a cycle.
        log.warning("materiality gate: LLM kept 0/%d — keeping all (fail-open)", len(cands))
        return cands
    return [c for i, c in enumerate(cands) if i in keep_idx]
```
(Confirm `os`, `Any`, `log`, `_claude_call`, `_parse_llm_json` are already imported — they are.)

- [ ] **Step 3: Wire into `run_ingest`** — between `fresh = dedupe(...)` and `ranked = cap(...)`:
```python
        fresh = dedupe(resolved, conn)
        material = _materiality_filter(fresh)
        ranked = cap(rank(material, conn))
        for c in ranked:
            insert_event(conn, {**c, "status": c["status"]})
        summary = {"fetched": len(cands), "resolved": len(resolved), "fresh": len(fresh),
                   "material": len(material),
                   "queued": sum(1 for c in ranked if c["status"] == "queued"),
                   "skipped": sum(1 for c in ranked if c["status"] == "skipped")}
```

- [ ] **Step 4: Run tests** — `python -B -m pytest tests/test_ingest_fetchers.py tests/test_ingest_news.py -v` (new pass; existing unchanged). If a `run_ingest` test asserts an exact summary dict, update it to include `material`.
- [ ] **Step 5: `.env.example`** — append under the So What? V2 block:
```
INGEST_MATERIALITY_GATE=1     # 1 = LLM gate keeps only deterministic market-moving news; 0 = off
# For hourly cadence, lower the precompute wallclock so a cycle can't overrun the hour:
# PRECOMPUTE_WALLCLOCK_S=3000
```
- [ ] **Step 6: Commit** — `feat(v2): LLM materiality gate — queue only deterministic market-moving news`.

---

## Self-review checklist
- Gate is one batched call; fail-open on empty/garbled/parsed-empty; no call when disabled or input empty.
- Order preserved; only `material=true` in-range indices kept.
- `run_ingest` summary includes `material`; dropped candidates never `insert_event`'d.
- Existing ingest tests green (update any exact-summary assertion to include `material`).
