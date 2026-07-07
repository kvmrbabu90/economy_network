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


def measure_seeds(conn, cands: list[dict], n: int) -> dict:
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
        if qm.primary_match(llm_primary, det_primary):
            prim_hits += 1
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=15, help="events to sample for seed agreement")
    ap.add_argument("--keep", type=float, default=ing.INGEST_MATERIALITY_KEEP)
    ap.add_argument("--drop", type=float, default=ing.INGEST_MATERIALITY_DROP)
    args = ap.parse_args()

    _install_llm_cache()
    conn = store.connect(store.default_db_path())
    print("Fetching live GKG candidates (LLM-free cascade)…")
    cands = ing.fetch_gkg_bulk(conn)
    print(f"  {len(cands)} candidates with priors\n")

    mat = measure_materiality(cands, args.keep, args.drop)
    print("== MATERIALITY (rule vs LLM gate) ==")
    print(f"  bands: {mat['bands']}   thresholds keep>={args.keep} drop<{args.drop}")
    print(f"  agreement:     {mat['agreement']:.3f}")
    print(f"  false_drop:    {mat['false_drop']}  ({mat['false_drop_rate']:.3f})  <- recall loss")
    print(f"  false_keep:    {mat['false_keep']}  ({mat['false_keep_rate']:.3f})")
    for h in mat["false_drop_examples"]:
        print(f"    dropped-but-material: {h}")

    seeds = measure_seeds(conn, cands, args.seeds)
    print("\n== SEEDS (deterministic seed_ids vs LLM extraction) ==")
    print(f"  n={seeds['n']}  jaccard={seeds['jaccard']:.3f}  "
          f"primary_match={seeds['primary_match_rate']:.3f}  "
          f"direction_agreement={seeds['direction_agreement']:.3f} (over {seeds['dir_shared']} shared)")

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
