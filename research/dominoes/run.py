"""CLI for the domino study: python -B -m research.dominoes.run <stage> [options].

Stages, in protocol order (spec §10, plan Task 11):
  snapshot   production DB (read-only) -> firms, links, any_edge_pairs
  prices     yfinance -> prices (study firms + SPY + sector ETFs; cached, never re-fetched)
  factors    Ken French FF5 + momentum -> factors (robustness 2)
  shocks     -> shocks[<suffix>]                       (--variant/--rule, or --all-variants)
  events     -> events_<period>, controls_<period>     (holdout: needs `unlock` first)
  power      exploration only -> power.json            (run and commit before `measure`)
  measure    -> results_<period>.json, paths_<period>  (holdout: needs `unlock` first)
  report     -> docs/research/domino-study/readout-<period>.md + figures
  unlock     record the commit that holds the committed exploration readout (opens the holdout)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
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


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=io.REPO_ROOT, capture_output=True, text=True)


def stage_unlock(commit: str, require_path: str = config.READOUT_PATH) -> dict:
    """Write the holdout marker, but only for a full commit hash that exists and contains `require_path`."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit or ""):
        raise ValueError(f"not a commit hash: {commit!r}")
    full = _git("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    if full.returncode != 0:
        raise ValueError(f"commit {commit} does not exist in this repository")
    sha = full.stdout.strip()
    if _git("cat-file", "-e", f"{sha}:{require_path}").returncode != 0:
        raise ValueError(f"commit {sha[:10]} does not contain {require_path}: commit the exploration readout first")
    store = io.store_dir()
    store.mkdir(parents=True, exist_ok=True)
    (store / config.HOLDOUT_MARKER).write_text(sha, encoding="utf-8")
    if not io.holdout_unlocked():
        raise RuntimeError("marker written but the holdout gate still reads locked")
    return {"unlocked_by": sha, "requires": require_path}


def main(argv: list[str] | None = None) -> dict:
    p = argparse.ArgumentParser(prog="research.dominoes.run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("stage", choices=STAGES)
    p.add_argument("--period", choices=("exploration", "holdout"), default="exploration")
    p.add_argument("--variant", choices=config.MODEL_VARIANTS, default=config.PRIMARY_RUN[0])
    p.add_argument("--rule", choices=bs.RULES, default=config.PRIMARY_RUN[1])
    p.add_argument("--all-variants", action="store_true", help="primary run plus robustness runs 1-3")
    p.add_argument("--end", help="prices: last date (exclusive), default today; pinned by the first fetch")
    p.add_argument("--boot-n", type=int, help="bootstrap draws (power default %d, measure default %d)"
                   % (config.POWER_BOOT_N, config.BOOT_N))
    p.add_argument("--placebo-n", type=int, default=config.PLACEBO_N,
                   help="holdout placebo permutations for the primary run (robustness runs use none)")
    p.add_argument("--out", help="report: output directory (default docs/research/domino-study)")
    p.add_argument("--commit", help="unlock: commit hash of the committed exploration readout")
    p.add_argument("--require-path", default=config.READOUT_PATH, help="unlock: file the commit must contain")
    args = p.parse_args(argv)

    if args.period == "holdout" and args.stage in ("events", "measure", "report"):
        io.require_holdout_unlocked()                     # refuse before touching any holdout data
    ctx = _Contexts()
    out: dict = {"stage": args.stage}

    if args.stage == "snapshot":
        out.update(snapshot_links.snapshot())
    elif args.stage == "prices":
        firms = io.read_frame("firms", snapshot_links.FIRM_SCHEMA)
        symbols = [config.MARKET_TICKER, *config.SECTOR_ETFS, *sorted(firms["symbol"])]
        out.update(fetch_prices.fetch(symbols, config.PRICE_START, args.end or dt.date.today().isoformat()))
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
            res = power_check.run("exploration", variant, rule, n_boot=args.boot_n or config.POWER_BOOT_N)
            out[f"{variant}/{rule}"] = {r: {"mde_w20": res["roles"][r]["w20"]["mde_bootstrap"],
                                           "underpowered": res["roles"][r]["underpowered"]}
                                       for r in config.ROLES}
    elif args.stage == "measure":
        for variant, rule in _runs(args):
            panel, model = ctx.get(variant)
            primary = (variant, rule) == config.PRIMARY_RUN
            res = measure.run(args.period, variant, rule, boot_n=args.boot_n or config.BOOT_N,
                              placebo_n=args.placebo_n if primary else 0, panel=panel, model=model)
            out[f"{variant}/{rule}"] = res.get("verdict", {}).get("overall", "exploration measured")
    elif args.stage == "report":
        out.update(report.run(args.period, args.out))
    elif args.stage == "unlock":
        out.update(stage_unlock(args.commit, args.require_path))
    return out


if __name__ == "__main__":
    try:
        print(json.dumps(main(), indent=2, default=str))
    except (PermissionError, ValueError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
