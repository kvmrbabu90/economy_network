# So What? V2 · P6 — Seed Injection for Batch Precompute (Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let `precompute` pass the ingest-resolved `seed_node_id` into the impact engine so each event traces from a guaranteed seed, instead of the engine re-extracting seeds from the bare headline (which fails ~96% of the time because `_resolve_entity` is Company-only).

**Architecture:** Add `_score_seed_node` + an optional `seed_hint_id` param to `run_impact_stream`/`run_impact`; inject the hint as a hop-0 seed after the engine's own seed assembly and before the no-seeds bail-out. `precompute` passes `ev["seed_node_id"]`. Default `None` ⇒ on-demand path unchanged.

**Tech Stack:** Python 3.x, pytest. Run with `python -B`. Design: `docs/superpowers/specs/2026-07-01-sowhat-v2-p6-seed-injection-design.md`.

---

### Task 1: `_score_seed_node` + `seed_hint_id` param + injection (`api/impact.py`)

**Files:**
- Modify: `api/impact.py`
- Test: `tests/test_impact_stream.py` (append)

Context (read `api/impact.py` around lines 979–1170 before editing):
- `run_impact_stream(text, *, conn, provider=None, max_hops=None, refine=True, verify=True)` is the generator. `run_impact(...)` (near line 1405) drains it.
- Seeds are assembled in Steps 3–5: `resolved_seeds` (named entities via `_extract_named_entities` → `_resolve_entity`, **Company-only**) + optional `commodity_seed`, combined into `all_seeds` with `seen_ids` tracking. Then `if not all_seeds: yield error+done; return`. Then hop-0 init loops `all_seeds` into `impacts`/`frontier`, sets `primary_seed_id`, and yields the `seeds` event.
- `_node_summary(conn, node_id)` returns `{id, type, name, sector, industry, country}` or None.
- Each seed dict shape (see the commodity_seed block): `{node_id, name, type, direction, magnitude, reasoning, sector, country, is_named_entity}`.
- `_llm_call(prompt)` + `_parse_llm_json(raw)` are the LLM primitives (tests patch `_llm_call`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_impact_stream.py`; reuse the `conn` fixture + autouse `patch_llm_and_frontier`).

```python
def _first_company(conn):
    row = conn.execute("SELECT id, name, type FROM nodes WHERE type='Company' LIMIT 1").fetchone()
    return row


def test_seed_hint_injected_at_hop0(conn, monkeypatch):
    # A hint node the engine's own extraction would NOT seed (fake extraction
    # returns no entities; commodity seed picks a different first candidate).
    hint = _first_company(conn)
    monkeypatch.setattr(impact_mod, "_score_seed_node",
                        lambda text, name, ntype: {"direction": "negative", "magnitude": 0.7, "reasoning": "hint"})
    events = list(impact_mod.run_impact_stream("some headline", conn=conn, seed_hint_id=hint["id"]))
    seeds_ev = next(e for e in events if e["event"] == "seeds")
    seed_ids = {v["node_id"] for v in seeds_ev["seeds"]}
    assert hint["id"] in seed_ids                                  # guaranteed seed present
    hint_seed = next(v for v in seeds_ev["seeds"] if v["node_id"] == hint["id"])
    assert hint_seed["hop"] == 0 and hint_seed["direction"] == "negative"


def test_seed_hint_none_is_unchanged(conn):
    # Backward-compat: passing no hint == today's behavior for the same input.
    base = {v["node_id"] for v in
            next(e for e in impact_mod.run_impact_stream("global crude oil supply shock", conn=conn)
                 if e["event"] == "seeds")["seeds"]}
    withn = {v["node_id"] for v in
             next(e for e in impact_mod.run_impact_stream("global crude oil supply shock", conn=conn, seed_hint_id=None)
                  if e["event"] == "seeds")["seeds"]}
    assert base == withn


def test_seed_hint_scoring_failopen(conn, monkeypatch):
    # If scoring returns None AND the engine finds no other seed, it must still
    # produce the normal "no seeds" result (not crash). Force no LLM seeds + no score.
    monkeypatch.setattr(impact_mod, "_extract_named_entities", lambda text: [])
    monkeypatch.setattr(impact_mod, "_llm_call", lambda prompt: "")   # commodity seed returns nothing
    monkeypatch.setattr(impact_mod, "_score_seed_node", lambda *a, **k: None)
    hint = _first_company(conn)
    result = impact_mod.run_impact("x headline", conn=conn, seed_hint_id=hint["id"])
    assert result.get("impacts") == [] and "seed" in result        # graceful, no exception


def test_seed_hint_already_seeded_no_duplicate(conn, monkeypatch):
    # If extraction already resolves the hint id, passing it must not duplicate it.
    hint = _first_company(conn)
    monkeypatch.setattr(impact_mod, "_extract_named_entities",
                        lambda text: [{"company_name": hint["name"], "direction": "negative",
                                       "magnitude": 0.8, "reasoning": "t"}])
    calls = {"n": 0}
    monkeypatch.setattr(impact_mod, "_score_seed_node",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or {"direction": "negative", "magnitude": 0.5, "reasoning": "t"})
    events = list(impact_mod.run_impact_stream("x", conn=conn, seed_hint_id=hint["id"]))
    seeds = next(e for e in events if e["event"] == "seeds")["seeds"]
    assert sum(1 for v in seeds if v["node_id"] == hint["id"]) == 1  # exactly one
    assert calls["n"] == 0                                          # hint already seeded → no scoring call
```

Run: `python -B -m pytest tests/test_impact_stream.py -k seed_hint -v` → FAIL (unexpected `seed_hint_id` kwarg / no `_score_seed_node`).

- [ ] **Step 2: Add `_score_seed_node`** near the other seed helpers in `api/impact.py` (e.g. after `_node_summary`).

```python
_SEED_SCORE_PROMPT = (
    "You are scoring the impact of a news event on ONE specific entity.\n\n"
    "News: {news}\n\n"
    "Entity: {name} ({type})\n\n"
    "Does this news have a positive, negative, or no_effect impact on this entity, "
    "and how strong is it (0.0-1.0)? Reply with ONLY a JSON object:\n"
    '{{"direction": "positive|negative|no_effect", "magnitude": 0.0, "reasoning": "<one short clause>"}}'
)


def _score_seed_node(text: str, name: str, node_type: str) -> Optional[dict[str, Any]]:
    """One focused LLM call: direction/magnitude/reasoning of `text` on a known
    entity. Returns None if the call fails or yields no usable direction."""
    raw = _llm_call(_SEED_SCORE_PROMPT.format(news=text, name=name, type=node_type))
    obj = _parse_llm_json(raw)
    if not isinstance(obj, dict):
        return None
    direction = obj.get("direction")
    if direction not in ("positive", "negative", "no_effect"):
        return None
    mag = obj.get("magnitude")
    try:
        magnitude = max(0.0, min(1.0, float(mag)))
    except (TypeError, ValueError):
        magnitude = 0.5
    return {"direction": direction, "magnitude": magnitude, "reasoning": obj.get("reasoning") or ""}
```

- [ ] **Step 3: Add `seed_hint_id` to both signatures.**

`run_impact_stream`:
```python
def run_impact_stream(
    text: str, *, conn: sqlite3.Connection, provider: Optional[str] = None,
    max_hops: Optional[int] = None, refine: bool = True, verify: bool = True,
    seed_hint_id: Optional[str] = None,
):
```
`run_impact`:
```python
def run_impact(
    text: str, *, conn: sqlite3.Connection, provider: Optional[str] = None,
    max_hops: Optional[int] = None, refine: bool = True, verify: bool = True,
    seed_hint_id: Optional[str] = None,
) -> dict[str, Any]:
    ...
    for ev in run_impact_stream(text, conn=conn, provider=provider,
                                max_hops=max_hops, refine=refine, verify=verify,
                                seed_hint_id=seed_hint_id):
```

- [ ] **Step 4: Inject the hint** — in `run_impact_stream`, immediately AFTER Step 5 assembles `all_seeds` (after `if commodity_seed: all_seeds.append(commodity_seed)`) and BEFORE the `if not all_seeds:` bail-out:

```python
        # == Step 5b: inject the caller's known seed (batch precompute) =========
        # precompute passes the node ingestion already resolved. _resolve_entity is
        # Company-only, so the engine's own extraction often can't re-find it; the
        # hint guarantees the trace anchors on the right node. Authoritative → not
        # subject to the seed-directness verify gate.
        if seed_hint_id and seed_hint_id not in seen_ids:
            summ = _node_summary(conn, seed_hint_id)
            if summ:
                scored = _score_seed_node(text, summ["name"], summ["type"])
                if scored:
                    all_seeds.append({
                        "node_id": seed_hint_id, "name": summ["name"], "type": summ["type"],
                        "direction": scored["direction"], "magnitude": scored["magnitude"],
                        "reasoning": scored["reasoning"], "sector": summ.get("sector"),
                        "country": summ.get("country"), "is_named_entity": False,
                    })
                    seen_ids.add(seed_hint_id)
                    debug_log.append(f"seed_hint: injected {seed_hint_id} ({summ['name']}) "
                                     f"{scored['direction']} ({scored['magnitude']:.2f})")
                else:
                    debug_log.append(f"seed_hint: {seed_hint_id} scoring failed — skipped")
            else:
                debug_log.append(f"seed_hint: {seed_hint_id} did not resolve — skipped")
```

- [ ] **Step 5: Run tests** — `python -B -m pytest tests/test_impact_stream.py -v` → all pass (4 new + existing 6 unchanged).
- [ ] **Step 6: Commit** — `feat(v2): seed_hint_id injection in run_impact for batch precompute`.

---

### Task 2: `precompute` passes the hint (`pipeline/precompute_impacts.py`)

**Files:**
- Modify: `pipeline/precompute_impacts.py`
- Test: `tests/test_precompute_impacts.py` (append)

Context: the trace call is `r = _impact.run_impact(ev["headline"], conn=conn, provider=prov, max_hops=BATCH_MAX_HOPS, refine=False, verify=False)`. `queued_events` rows include `seed_node_id` (from P1 ingestion).

- [ ] **Step 1: Write the failing test** (append to `tests/test_precompute_impacts.py`):

```python
def test_precompute_passes_seed_hint(tmp_path, monkeypatch):
    db = _db_with_queued(tmp_path, 1)              # seeds an event e0 with seed_node_id 'cik:0'
    captured = {}
    def fake_run_impact(headline, **kwargs):
        captured.update(kwargs); captured["headline"] = headline
        return {"seeds": [{"node_id": "cik:0"}],
                "impacts": [{"node_id": "cik:0", "direction": "negative", "magnitude": 0.5, "hop": 0}]}
    monkeypatch.setattr(pc._impact, "run_impact", fake_run_impact)
    pc.run_precompute(db)
    assert captured.get("seed_hint_id") == "cik:0"     # ingest-resolved seed handed to the engine
```
(Confirm `_db_with_queued` sets `seed_node_id` = `cik:0` for e0; the P1/P2 fixture inserts `seed_node_id=f"cik:{i}"`. If the column name differs, read the fixture and match.)

Run: `python -B -m pytest tests/test_precompute_impacts.py -k seed_hint -v` → FAIL (`seed_hint_id` not in captured).

- [ ] **Step 2: Pass the hint** in `run_precompute`:
```python
                r = _impact.run_impact(ev["headline"], conn=conn, provider=prov,
                                       max_hops=BATCH_MAX_HOPS, refine=False, verify=False,
                                       seed_hint_id=ev.get("seed_node_id"))
```

- [ ] **Step 3: Run tests** — `python -B -m pytest tests/test_precompute_impacts.py -v` (all pass, existing unchanged).
- [ ] **Step 4: Commit** — `feat(v2): precompute passes ingest-resolved seed_node_id as seed hint`.

---

## Self-review checklist (after both tasks)
- `seed_hint_id=None` path is byte-for-byte unchanged (on-demand untouched); existing `test_impact_stream.py` green.
- Hint bypasses `_verify_seed_directness` (injected unconditionally when scored).
- Already-seeded hint → no duplicate, no wasted scoring call.
- Scoring fail-open → no crash, falls back to today's behavior.
- Full suite green: `python -B -m pytest tests/test_impact_stream.py tests/test_precompute_impacts.py tests/test_aggregate_impacts.py tests/test_events_store.py -q`.
