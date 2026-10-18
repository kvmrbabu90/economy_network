"""Stage: build (shock at A, linked firm B, role) events with matched look-alike controls and signed
outcomes (spec §4, §13.4-§13.6).

Controls for an event: firms in B's sector that are not A, not B, and have NO edge of any type, extractor
or threshold to A (the any-edge pair set); with a valid estimation window and >= 20 days of dollar volume
at the event; ranked by |log DV(C) - log DV(B)| with ties broken by ticker; up to 10 kept. An event needs
B to have a valid estimation window and dollar volume, and at least 2 eligible controls.

Per window w: raw_b = s * CAR_B; for K in (3, 5, 10) using the first K ranked controls, dropping any
without a complete window: raw_c = s * mean CAR_C, ripple = s * (CAR_B - mean CAR_C), nctl = controls
used. A window with fewer than 2 complete controls (or no complete CAR_B) has ripple = NaN.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from research.dominoes import build_shocks as bs
from research.dominoes import config, io, snapshot_links
from research.dominoes import returns as rt

EVENT_BASE_SCHEMA = {
    "event_id": "str", "shock_id": "str", "date": "date", "week": "str", "period": "str", "e_idx": "int",
    "a_id": "str", "b_id": "str", "a_symbol": "str", "b_symbol": "str", "b_sector": "str", "role": "str",
    "extractors": "str", "weight": "float", "s": "int", "z_a": "float", "abn_vol_a": "float",
    "sector_etf_z": "float", "dv_b": "float",
}
CONTROL_SCHEMA = {"event_id": "str", "rank": "int", "c_id": "str", "c_symbol": "str"}


def outcome_columns() -> list[str]:
    cols = []
    for w in config.WINDOWS:
        cols.append(f"raw_b_{w}")
        for k in config.K_VARIANTS:
            cols += [f"raw_c_{w}_k{k}", f"ripple_{w}_k{k}", f"nctl_{w}_k{k}"]
    return cols


def event_schema() -> dict[str, str]:
    schema = dict(EVENT_BASE_SCHEMA)
    for c in outcome_columns():
        schema[c] = "int" if c.startswith("nctl_") else "float"
    return schema


def rank_controls(dv_b: float, cand_ids: list, cand_dv: np.ndarray, cand_tickers: list,
                  k: int = config.K_MAX) -> list:
    dist = np.abs(np.log(np.asarray(cand_dv, dtype=float)) - np.log(dv_b))
    order = sorted(range(len(cand_ids)), key=lambda j: (dist[j], cand_tickers[j]))
    return [cand_ids[j] for j in order[:k]]


def _empty(schema: dict[str, str]) -> pd.DataFrame:
    dtype = {"str": "str", "float": "float64", "int": "int64", "date": "datetime64[ns]", "bool": "bool"}
    return pd.DataFrame({c: pd.Series(dtype=dtype[k]) for c, k in schema.items()})


def build_events_for_links(panel: rt.Panel, model: rt.AbnormalModel, shocks: pd.DataFrame,
                           links: pd.DataFrame, firms: pd.DataFrame, any_edge_pairs: pd.DataFrame,
                           period: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    if period == "holdout":
        io.require_holdout_unlocked()
    sh = shocks[shocks["period"] == period]
    f = firms.set_index("firm_id")
    sym_of, tick_of, sec_of = f["symbol"].to_dict(), f["ticker"].to_dict(), f["sector"].to_dict()
    col_of = {fid: panel.col[s] for fid, s in sym_of.items() if s in panel.col}
    linked: dict[str, set] = {}
    for x, y in any_edge_pairs[["x_id", "y_id"]].itertuples(index=False):
        linked.setdefault(x, set()).add(y)
        linked.setdefault(y, set()).add(x)
    members: dict[str, list] = {}
    for fid in sorted(col_of):
        if sec_of.get(fid):
            members.setdefault(sec_of[fid], []).append(fid)
    links_by_a = {a: g for a, g in links.groupby("a_id")}

    e_vals = np.unique(sh["e_idx"].to_numpy(dtype=np.int64)) if len(sh) else np.array([], dtype=np.int64)
    fids = sorted(col_of)
    pos_f = {fid: j for j, fid in enumerate(fids)}
    pos_e = {int(e): k for k, e in enumerate(e_vals)}
    if len(e_vals) and fids:
        ee, ff = np.meshgrid(e_vals, np.array([col_of[x] for x in fids]), indexing="ij")
        DV = rt.dollar_volume(panel, ff.ravel(), ee.ravel()).reshape(len(e_vals), len(fids))
    else:
        DV = np.empty((0, 0))

    def usable(fid: str, e: int) -> bool:
        c = col_of[fid]
        return model.nest[c, e] >= config.MIN_EST_DAYS and np.isfinite(model.coef[c, e, 0])

    recs, ctl_recs, ctl_cols = [], [], []
    for shk in sh.itertuples(index=False):
        a = shk.firm_id
        if a not in links_by_a:
            continue
        e = int(shk.e_idx)
        ke = pos_e[e]
        excluded = linked.get(a, set()) | {a}
        for ln in links_by_a[a].itertuples(index=False):
            b = ln.b_id
            if b not in col_of or not usable(b, e):
                continue
            dv_b = DV[ke, pos_f[b]]
            if not (np.isfinite(dv_b) and dv_b > 0):
                continue
            cands = [c for c in members.get(sec_of.get(b), []) if c != b and c not in excluded and usable(c, e)]
            cdv = np.array([DV[ke, pos_f[c]] for c in cands], dtype=float)
            keep = np.isfinite(cdv) & (cdv > 0)
            cands = [c for c, k in zip(cands, keep) if k]
            cdv = cdv[keep]
            if len(cands) < config.MIN_CONTROLS:
                continue
            ranked = rank_controls(dv_b, cands, cdv, [tick_of[c] for c in cands])
            eid = f"{shk.shock_id}>{b}>{ln.role}"
            recs.append({
                "event_id": eid, "shock_id": shk.shock_id, "date": pd.Timestamp(shk.date),
                "week": pd.Timestamp(shk.date).strftime("%G-W%V"), "period": period, "e_idx": e,
                "a_id": a, "b_id": b, "a_symbol": sym_of[a], "b_symbol": sym_of[b], "b_sector": sec_of.get(b, ""),
                "role": ln.role, "extractors": ln.extractors, "weight": float(ln.weight), "s": int(shk.s),
                "z_a": float(shk.z), "abn_vol_a": float(shk.abn_vol), "sector_etf_z": float(shk.sector_etf_z),
                "dv_b": float(dv_b), "_b_col": col_of[b]})
            ctl_cols.append([col_of[c] for c in ranked] + [-1] * (config.K_MAX - len(ranked)))
            ctl_recs += [{"event_id": eid, "rank": r, "c_id": c, "c_symbol": sym_of[c]}
                         for r, c in enumerate(ranked, start=1)]

    if not recs:
        return _empty(event_schema()), _empty(CONTROL_SCHEMA)
    ev = pd.DataFrame(recs)
    n = len(ev)
    bcols = ev["_b_col"].to_numpy(dtype=np.int64)
    evs = ev["e_idx"].to_numpy(dtype=np.int64)
    s = ev["s"].to_numpy(dtype=float)
    C = np.array(ctl_cols, dtype=np.int64)
    pad = C < 0
    flat_cols = np.where(pad, 0, C).ravel()
    flat_e = np.repeat(evs, config.K_MAX)
    for w, (lo, hi) in config.WINDOWS.items():
        car_b = model.car(bcols, evs, lo, hi)
        car_c = model.car(flat_cols, flat_e, lo, hi).reshape(n, config.K_MAX)
        car_c[pad] = np.nan
        ev[f"raw_b_{w}"] = s * car_b
        for k in config.K_VARIANTS:
            sub = car_c[:, :k]
            n_ok = np.isfinite(sub).sum(axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                mean_c = np.where(n_ok > 0, np.nansum(sub, axis=1) / np.maximum(n_ok, 1), np.nan)
            ripple = s * (car_b - mean_c)
            ripple[(n_ok < config.MIN_CONTROLS) | ~np.isfinite(car_b)] = np.nan
            ev[f"raw_c_{w}_k{k}"] = s * mean_c
            ev[f"ripple_{w}_k{k}"] = ripple
            ev[f"nctl_{w}_k{k}"] = n_ok.astype("int64")
    ev["e_idx"] = ev["e_idx"].astype("int64")
    ev["s"] = ev["s"].astype("int64")
    ev = ev.drop(columns=["_b_col"])[list(event_schema())]
    ctl = pd.DataFrame(ctl_recs)
    ctl["rank"] = ctl["rank"].astype("int64")
    return ev.reset_index(drop=True), ctl[list(CONTROL_SCHEMA)]


def build(period: str, variant: str = "market", rule: str = "sigma3",
          panel: rt.Panel | None = None, model: rt.AbnormalModel | None = None) -> dict:
    """Stage entry: read shocks/links/firms/pairs from the store, write events_<period> + controls_<period>."""
    if period not in ("exploration", "holdout"):
        raise ValueError(f"unknown period {period!r}")
    if period == "holdout":
        io.require_holdout_unlocked()
    sfx = config.variant_suffix(variant, rule)
    shocks = io.read_frame(f"shocks{sfx}", bs.SHOCK_SCHEMA)
    firms = io.read_frame("firms", snapshot_links.FIRM_SCHEMA)
    links = io.read_frame("links", snapshot_links.LINK_SCHEMA)
    pairs = io.read_frame("any_edge_pairs", snapshot_links.PAIR_SCHEMA)
    if panel is None or model is None:
        from research.dominoes import context
        panel, model = context.load(variant)
    ev, ctl = build_events_for_links(panel, model, shocks, links, firms, pairs, period)
    io.write_frame(f"events_{period}{sfx}", ev, event_schema())
    io.write_frame(f"controls_{period}{sfx}", ctl, CONTROL_SCHEMA)
    return {"events": len(ev), "controls": len(ctl), "shocks_in_period": int((shocks["period"] == period).sum())}
