"""CLI for the domino study: python -B -m research.dominoes.run <stage> [options].

Stages, in protocol order (spec §10, plan Task 11):
  snapshot   production DB (read-only) -> firms, links, any_edge_pairs
  prices     yfinance -> prices (adjusted: returns) + prices_raw (unadjusted close: dollar volume), both cached
             and never re-fetched; writes raw_sanity.json
  factors    Ken French FF5 + momentum -> factors (robustness 2)
  shocks     -> shocks[<suffix>]                       (--variant/--rule, or --all-variants)
  events     -> events_<period>, controls_<period>     (holdout: needs `unlock` first)
  power      exploration only -> power[<suffix>].json  (run ONCE, before `measure`; never regenerated after)
  measure    -> results_<period>.json, paths_<period>  (holdout: needs `unlock` first)
  report     -> docs/research/domino-study/readout-<period>.md + figures (+ integrity table in exploration)
  unlock     open the holdout: the commit must hold the exploration readout whose integrity table matches the
             store's exploration results and power files

The registered constants (bootstrap draws, placebo permutations, seeds) are not CLI options: the primary run
uses exactly the pre-registered values.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys

from research.dominoes import build_events as be
from research.dominoes import build_shocks as bs
from research.dominoes import config, context, fetch_factors, fetch_prices, io, measure
from research.dominoes import power_check, report, snapshot_links

STAGES = ("snapshot", "prices", "factors", "shocks", "events", "power", "measure", "report", "unlock")


def _runs(args) -> list[tuple[str, str]]:
    if args.all_variants:
        return [config.PRIMARY_RUN] + [(v, r) for v, r, _ in config.ROBUSTNESS_RUNS]
    return [(args.variant, args.rule)]


class _Contexts:
    """Panel + model per model variant, built once per CLI call."""

    def __init__(self) -> None:
        self._cache: dict = {}

    def get(self, variant: str):
        if variant not in self._cache:
            self._cache[variant] = context.load(variant)
        return self._cache[variant]


def stage_unlock(commit: str) -> dict:
    """Write the holdout marker only if the gate then reads open; otherwise remove it and say why."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit or ""):
        raise ValueError(f"not a commit hash: {commit!r}")
    sha, _, reason = io.committed_integrity(commit)
    if reason:
        raise ValueError(reason)
    store = io.store_dir()
    store.mkdir(parents=True, exist_ok=True)
    marker = store / config.HOLDOUT_MARKER
    marker.write_text(sha, encoding="utf-8")
    io.clear_gate_cache()
    ok, why = io.gate_status()
    if not ok:
        marker.unlink()
        io.clear_gate_cache()
        raise ValueError(f"holdout stays locked: {why}")
    return {"unlocked_by": sha, "readout": config.READOUT_PATH}


def stage_prices(end: str | None) -> dict:
    firms = io.read_frame("firms", snapshot_links.FIRM_SCHEMA)
    symbols = [config.MARKET_TICKER, *config.SECTOR_ETFS, *sorted(firms["symbol"])]
    out = {"adjusted": fetch_prices.fetch(symbols, config.PRICE_START, end or dt.date.today().isoformat()),
           "raw": fetch_prices.fetch_raw(symbols)}
    sanity = fetch_prices.raw_sanity(fetch_prices.load_prices(), fetch_prices.load_prices_raw())
    io.write_json(io.store_dir() / "raw_sanity.json", sanity)
    out["raw_sanity"] = {k: v for k, v in sanity.items() if not isinstance(v, list)}
    return out


def main(argv: list[str] | None = None) -> dict:
    p = argparse.ArgumentParser(prog="research.dominoes.run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("stage", choices=STAGES)
    p.add_argument("--period", choices=("exploration", "holdout"), default="exploration")
    p.add_argument("--variant", choices=config.MODEL_VARIANTS, default=config.PRIMARY_RUN[0])
    p.add_argument("--rule", choices=bs.RULES, default=config.PRIMARY_RUN[1])
    p.add_argument("--all-variants", action="store_true", help="primary run plus robustness runs 1-3")
    p.add_argument("--end", help="prices: last date (exclusive), default today; pinned by the first fetch")
    p.add_argument("--out", help="report: output directory (default docs/research/domino-study)")
    p.add_argument("--commit", help="unlock: commit hash of the committed exploration readout")
    args = p.parse_args(argv)

    if args.period == "holdout" and args.stage in ("events", "measure", "report"):
        io.require_holdout_unlocked()                     # refuse before touching any holdout data
    ctx = _Contexts()
    out: dict = {"stage": args.stage}

    if args.stage == "snapshot":
        out.update(snapshot_links.snapshot())
    elif args.stage == "prices":
        out.update(stage_prices(args.end))
    elif args.stage == "factors":
        out["rows"] = fetch_factors.fetch_factors()
    elif args.stage == "shocks":
        firms = io.read_frame("firms", snapshot_links.FIRM_SCHEMA)
        for variant, rule in _runs(args):
            panel, model = ctx.get(variant)
            shocks = bs.detect(panel, model, firms, rule=rule)
            io.write_frame(f"shocks{config.variant_suffix(variant, rule)}", shocks, bs.SHOCK_SCHEMA)
            out[f"{variant}/{rule}"] = shocks["period"].value_counts().to_dict()
    elif args.stage == "events":
        for variant, rule in _runs(args):
            panel, model = ctx.get(variant)
            out[f"{variant}/{rule}"] = be.build(args.period, variant, rule, panel, model)
    elif args.stage == "power":
        for variant, rule in _runs(args):
            res = power_check.run("exploration", variant, rule)
            out[f"{variant}/{rule}"] = {r: {"mde_w20": res["roles"][r]["w20"]["mde_bootstrap"],
                                           "underpowered": res["roles"][r]["underpowered"],
                                           "borderline": res["roles"][r]["borderline"]}
                                       for r in config.ROLES}
    elif args.stage == "measure":
        for variant, rule in _runs(args):
            panel, model = ctx.get(variant)
            res = measure.run(args.period, variant, rule, panel=panel, model=model)
            v = res.get("verdict")
            out[f"{variant}/{rule}"] = v["overall"] if v else f"{args.period} measured"
    elif args.stage == "report":
        out.update(report.run(args.period, args.out))
    elif args.stage == "unlock":
        out.update(stage_unlock(args.commit))
    return out


if __name__ == "__main__":
    try:
        print(json.dumps(main(), indent=2, default=str))
    except (PermissionError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
