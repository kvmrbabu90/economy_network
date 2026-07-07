"""A/B quality harness — measures how faithfully the deterministic scaffolding
reproduces the LLM path, so "minimize LLM" can be held to a measured quality bar.

    python -B scripts/ab_quality.py [--seeds N] [--keep K] [--drop D]

Measures two stages where the deterministic path diverges from the LLM path:

  1. MATERIALITY — the rule's bands (auto-keep / auto-drop / judge) vs the LLM
     gate's verdict on the SAME candidates. Reports false-drop (recall loss — a
     material event never traced, the dangerous one) and false-keep (a trace
     wasted on noise).
  2. SEEDS — the deterministic seed_ids vs LLM entity extraction: set Jaccard,
     primary-seed match, and seed-direction agreement (batched score vs
     extraction) over the overlap.

LLM responses are cached to scripts/.ab_cache.json (keyed by prompt sha1), so
re-runs and threshold sweeps (--keep/--drop) are free and deterministic — only
the band classification changes, not the (cached) LLM verdicts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from schema import store                       # noqa: E402
from pipeline import ingest_news as ing        # noqa: E402
from pipeline import quality_metrics as qm     # noqa: E402
from api import impact as impact_mod           # noqa: E402

_CACHE_PATH = REPO / "scripts" / ".ab_cache.json"


def _install_llm_cache() -> dict:
    """Wrap the two LLM entry points with an on-disk prompt->response cache so the
    measurement is repeatable and cheap. Returns the cache dict."""
    cache: dict = json.loads(_CACHE_PATH.read_text()) if _CACHE_PATH.exists() else {}
    orig_impact = impact_mod._llm_call
    orig_ing = ing._claude_call

    def _wrap(orig):
        def cached(prompt: str) -> str:
            key = hashlib.sha1(prompt.encode("utf-8")).hexdigest()
            if key in cache:
                return cache[key]
            r = orig(prompt)
            cache[key] = r
            _CACHE_PATH.write_text(json.dumps(cache))
            return r
        return cached

    impact_mod._llm_call = _wrap(orig_impact)
    ing._claude_call = _wrap(orig_ing)
    return cache


def measure_materiality(cands: list[dict], keep_thr: float, drop_thr: float) -> dict:
    items = [(c["id"], c.get("_prior", 0.0)) for c in cands]
    bands = qm.classify_materiality(items, keep_thr, drop_thr, ing._MATERIALITY_AUTOKEEP)
    llm_kept = {c["id"] for c in ing._materiality_filter(cands)}      # LLM = reference
    all_ids = {c["id"] for c in cands}
    rule_keep = set(bands["auto_keep"]) | (set(bands["judge"]) & llm_kept)
    conf = qm.materiality_confusion(rule_keep, llm_kept, all_ids)
    conf["bands"] = {k: len(v) for k, v in bands.items()}
    # Enrich false-drops with headlines — these are the events we'd wrongly lose.
    by_id = {c["id"]: c for c in cands}
    conf["false_drop_examples"] = [by_id[i].get("headline", "")[:70] for i in conf["false_drop_ids"]]
    return conf


def _name(conn, nid: str) -> str:
    s = impact_mod._node_summary(conn, nid)
    return s["name"] if s else nid


def measure_seeds(conn, cands: list[dict], n: int, dump: bool = False) -> dict:
    sample = [c for c in cands if c.get("seed_ids")][:n]
    jacc: list[float] = []
    prim_hits = 0
    dir_agree = dir_shared = 0
    for c in sample:
        det = json.loads(c["seed_ids"])
        det_primary = det[0] if det else None
        named = impact_mod._extract_named_entities(c["headline"])
        llm_ids: list[str] = []
        llm_dirs: dict[str, str] = {}
        llm_primary = None
        best_mag = -1.0
        for e in named:
            node = impact_mod._resolve_entity(conn, e["company_name"])
            if node:
                nid = node["id"]
                llm_ids.append(nid)
                llm_dirs[nid] = e["direction"]
                if e["magnitude"] > best_mag:
                    best_mag, llm_primary = e["magnitude"], nid
        jacc.append(qm.seed_jaccard(set(det), set(llm_ids)))
        matched = qm.primary_match(llm_primary, det_primary)
        if matched:
            prim_hits += 1
        if dump:
            print(f"  [{'OK ' if matched else 'DIFF'}] {c['headline'][:55]}")
            print(f"       det : {[_name(conn, x) for x in det]}")
            print(f"       llm : {[_name(conn, x) for x in llm_ids]} "
                  f"(raw: {[e['company_name'] for e in named]})")
        summaries = [s for nid in det if (s := impact_mod._node_summary(conn, nid))]
        det_dirs = {nid: sc["direction"]
                    for nid, sc in impact_mod._score_seed_set(c["headline"], summaries).items()}
        a, s = qm.direction_agreement(det_dirs, llm_dirs)
        dir_agree += a
        dir_shared += s
    k = len(sample) or 1
    return {
        "n": len(sample),
        "jaccard": statistics.mean(jacc) if jacc else 1.0,
        "primary_match_rate": prim_hits / k,
        "direction_agreement": dir_agree / dir_shared if dir_shared else 1.0,
        "dir_shared": dir_shared,
    }


def measure_endtoend(conn, cands: list[dict], n: int) -> dict:
    """The authoritative test: trace a sample BOTH ways and diff the impact MAPS.
      Config A (old): LLM entity extraction + primary GKG seed injected (seed_hint).
      Config B (new): deterministic known_seed_ids, no extraction.
    Reports node-set Jaccard and direction agreement of the resulting node_impact
    maps — this is what actually reaches the user, so it's the real 'quality' axis.
    Config: 1 hop (seed + first ring — where the seed difference shows), no
    refine/verify, no frontier sampling (deterministic A/B). Robust to timeouts."""
    import os as _os
    sample = [c for c in cands if c.get("seed_ids")][:n]
    node_jacc: list[float] = []
    dir_agree = dir_shared = 0
    ok = 0
    for c in sample:
        det = json.loads(c["seed_ids"])
        try:
            _os.environ["TRUST_KNOWN_SEEDS"] = "0"       # OLD: extraction + primary seed_hint
            a = impact_mod.run_impact(c["headline"], conn=conn, max_hops=1, refine=False,
                                      verify=False, seed_hint_id=det[0], context=c.get("gkg_context"))
            _os.environ["TRUST_KNOWN_SEEDS"] = "1"       # NEW: deterministic seed set
            b = impact_mod.run_impact(c["headline"], conn=conn, max_hops=1, refine=False,
                                      verify=False, known_seed_ids=det, context=c.get("gkg_context"))
        except Exception as exc:
            print(f"  [skip] {c['headline'][:45]}: {exc}")
            continue
        finally:
            _os.environ["TRUST_KNOWN_SEEDS"] = "1"
        a_map = {i["node_id"]: i["direction"] for i in (a.get("impacts") or [])}
        b_map = {i["node_id"]: i["direction"] for i in (b.get("impacts") or [])}
        j = qm.seed_jaccard(set(a_map), set(b_map))
        ag, sh = qm.direction_agreement(a_map, b_map)
        node_jacc.append(j)
        dir_agree += ag
        dir_shared += sh
        ok += 1
        print(f"  {c['headline'][:45]:47} A={len(a_map):3d} B={len(b_map):3d} "
              f"jaccard={j:.2f} dir_agree={ag}/{sh}")
    return {
        "n": ok,
        "node_jaccard": statistics.mean(node_jacc) if node_jacc else 1.0,
        "direction_agreement": dir_agree / dir_shared if dir_shared else 1.0,
        "dir_shared": dir_shared,
    }


def measure_noisefloor(conn, cands: list[dict], n: int) -> dict:
    """The LLM's OWN reproducibility floor: trace each event TWICE under the SAME
    (new) config and measure direction agreement. Any A/B direction agreement at or
    above this floor means the config difference is within LLM sampling noise — i.e.
    not a real quality change. MUST run with the cache OFF or the second run trivially
    returns the first's cached responses."""
    import os as _os
    _os.environ["TRUST_KNOWN_SEEDS"] = "1"
    sample = [c for c in cands if c.get("seed_ids")][:n]
    dir_agree = dir_shared = 0
    for c in sample:
        det = json.loads(c["seed_ids"])
        try:
            b1 = impact_mod.run_impact(c["headline"], conn=conn, max_hops=1, refine=False,
                                       verify=False, known_seed_ids=det, context=c.get("gkg_context"))
            b2 = impact_mod.run_impact(c["headline"], conn=conn, max_hops=1, refine=False,
                                       verify=False, known_seed_ids=det, context=c.get("gkg_context"))
        except Exception as exc:
            print(f"  [skip] {c['headline'][:45]}: {exc}")
            continue
        m1 = {i["node_id"]: i["direction"] for i in (b1.get("impacts") or [])}
        m2 = {i["node_id"]: i["direction"] for i in (b2.get("impacts") or [])}
        a, s = qm.direction_agreement(m1, m2)
        dir_agree += a
        dir_shared += s
        print(f"  {c['headline'][:45]:47} self dir_agree={a}/{s}")
    return {"direction_agreement": dir_agree / dir_shared if dir_shared else 1.0, "dir_shared": dir_shared}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=15, help="events to sample for seed agreement")
    # Production no longer auto-keeps by prior (measured unsafe); default the sweep
    # to "no prior auto-keep" (1e12) so the harness reflects production. Lower it to
    # explore hypothetical auto-keep thresholds.
    ap.add_argument("--keep", type=float, default=1e12)
    ap.add_argument("--drop", type=float, default=ing.INGEST_MATERIALITY_DROP)
    ap.add_argument("--dump", action="store_true", help="print per-event seed comparison")
    ap.add_argument("--e2e", type=int, default=0, help="end-to-end impact-map A/B over N events (slow)")
    ap.add_argument("--noisefloor", type=int, default=0,
                    help="LLM reproducibility floor: same config traced twice over N events (implies --nocache)")
    ap.add_argument("--nocache", action="store_true", help="do not cache LLM responses")
    args = ap.parse_args()

    if not (args.nocache or args.noisefloor):
        _install_llm_cache()
    conn = store.connect(store.default_db_path())
    print("Fetching live GKG candidates (LLM-free cascade)…")
    cands = ing.fetch_gkg_bulk(conn)
    print(f"  {len(cands)} candidates with priors\n")

    if args.noisefloor:
        print(f"== LLM NOISE FLOOR (same config traced twice), N={args.noisefloor} ==")
        nf = measure_noisefloor(conn, cands, args.noisefloor)
        print(f"  self direction_agreement={nf['direction_agreement']:.3f} (over {nf['dir_shared']} shared)")
        conn.close()
        return 0

    mat = measure_materiality(cands, args.keep, args.drop)
    print("== MATERIALITY (rule vs LLM gate) ==")
    print(f"  bands: {mat['bands']}   thresholds keep>={args.keep} drop<{args.drop}")
    print(f"  agreement:     {mat['agreement']:.3f}")
    print(f"  false_drop:    {mat['false_drop']}  ({mat['false_drop_rate']:.3f})  <- recall loss")
    print(f"  false_keep:    {mat['false_keep']}  ({mat['false_keep_rate']:.3f})")
    for h in mat["false_drop_examples"]:
        print(f"    dropped-but-material: {h}")

    if args.dump:
        print("\n== SEED COMPARISON (per event) ==")
    seeds = measure_seeds(conn, cands, args.seeds, dump=args.dump)
    print("\n== SEEDS (deterministic seed_ids vs LLM extraction) ==")
    print(f"  n={seeds['n']}  jaccard={seeds['jaccard']:.3f}  "
          f"primary_match={seeds['primary_match_rate']:.3f}  "
          f"direction_agreement={seeds['direction_agreement']:.3f} (over {seeds['dir_shared']} shared)")

    if args.e2e:
        print(f"\n== END-TO-END impact-map A/B (old LLM-extraction vs new deterministic), N={args.e2e} ==")
        e2e = measure_endtoend(conn, cands, args.e2e)
        print(f"  traced {e2e['n']}  node_jaccard={e2e['node_jaccard']:.3f}  "
              f"direction_agreement={e2e['direction_agreement']:.3f} (over {e2e['dir_shared']} shared)")

    retention = qm.retention_score(
        primary_match_rate=seeds["primary_match_rate"],
        direction_agreement=seeds["direction_agreement"],
        materiality_agreement=mat["agreement"],
    )
    print(f"\n== RETENTION SCORE: {retention:.3f} ==  (target >= 0.98)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
