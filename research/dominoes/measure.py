"""Stage: measure the pre-registered hypotheses (spec §5-§8, §13).

Exploration (2015-2021): per role x window the date-clustered mean of the primary ripple (K=5), with a
week-clustered t and a date-block bootstrap CI (robustness) and a two-way (date x linked firm) t (exploratory
diagnostic); H1 (same-day follow, t > 3), the H2 family (9 tests: Holm on two-sided p + |t| > 3, direction =
sign of the mean), H3 pre-trend (ok / flag / untestable), H5 first-domino drift, H6 hypothesis-generating splits,
the transmission coefficient gamma, robustness subsets 4/5/7/8/9 and the exploratory R10 (B's own co-shock).

Holdout (2022+, only after the exploration readout is committed): one-sided replication in the exploration
direction, the 50 bp floor, the placebo, H3 in both periods, holdout H5, robustness 6 and R10, and the §7
verdict. The placebo (§5, §13): in each of 500 permutations and per role, every real B with a finite holdout
outcome is relabelled to ONE random B' (with replacement) from its sector among firms usable at every date of
B's real events, excluding B and the shocked firms linked to B in that role; a relabelled pair that is a study
link is dropped (never redrawn); a placebo event counts only where the matching real event is finite. It is
evaluable only with >= 90% finite permutations and median coverage >= 50% of the real events; otherwise the test
cannot pass and the role is inconclusive (never "no ripple").

The power file is mandatory, pinned (method version, n_boot, seed, variant/rule) and must match the current
exploration events (fingerprint); its sha256 is recorded in the exploration results and re-checked before the
holdout is measured. Robustness variants (§8) carry no verdict.
"""
from __future__ import annotations

import hashlib
import json
import math

import numpy as np
import pandas as pd

from research.dominoes import build_events as be
from research.dominoes import build_shocks as bs
from research.dominoes import config, io, snapshot_links
from research.dominoes import power_check as pc
from research.dominoes import returns as rt
from research.dominoes import stats as st

PATH_SCHEMA = {"role": "str", "series": "str", "day": "int", "car": "float", "n": "int"}
WINDOW_KEYS = tuple(config.WINDOWS)
INCONCLUSIVE = ("inconclusive_underpowered", "inconclusive_unevaluable", "same_day_slow_inconclusive")


def _f(x) -> float:
    return float(x) if x is not None and np.isfinite(x) else math.nan


# ------------------------------------------------------------------------------------------- cells/tables
def _cell(df: pd.DataFrame, values: np.ndarray, boot_n: int | None, seed: int) -> dict:
    v = np.asarray(values, dtype=float)
    r = st.clustered_mean(v, df["date"].to_numpy())
    out = {"mean": _f(r["mean"]), "se": _f(r["se"]), "t": _f(r["t"]), "n": int(r["n"]), "g": int(r["g"]),
           "df": int(r["df"]), "p_two": _f(r["p_two"])}
    if "b_id" in df.columns:
        r2 = st.clustered_mean_2way(v, df["date"].to_numpy(), df["b_id"].to_numpy())
        out.update({"t_2w": _f(r2["t"]), "df_2w": int(r2["df"]), "p_one_pos_2w": _f(r2["p_one_pos"]),
                    "p_one_neg_2w": _f(r2["p_one_neg"]), "fallback_2w": bool(r2["fallback"])})
    if boot_n and "week" in df.columns:
        out["t_week"] = _f(st.clustered_mean(v, df["week"].to_numpy())["t"])
        lo, hi = st.block_bootstrap_ci(v, df["date"].to_numpy(), boot_n, seed) if r["n"] else (math.nan, math.nan)
        out["ci_boot"] = [_f(lo), _f(hi)]
    return out


def _values(df: pd.DataFrame, prefix: str, w: str, k: int, align: bool) -> np.ndarray:
    col = {"ripple": f"ripple_{w}_k{k}", "raw_b": f"raw_b_{w}", "raw_c": f"raw_c_{w}_k{k}"}[prefix]
    v = df[col].to_numpy(dtype=float).copy()
    if align:                                   # robustness 9 on exactly the ripple's sample
        v[~np.isfinite(df[f"ripple_{w}_k{k}"].to_numpy(dtype=float))] = np.nan
    return v


def role_window_table(ev: pd.DataFrame, k: int, boot_n: int | None, seed: int,
                      windows=WINDOW_KEYS, prefix: str = "ripple", align: bool = False) -> dict:
    table = {}
    for i, role in enumerate(config.ROLES):
        sub = ev[ev["role"] == role]
        table[role] = {w: _cell(sub, _values(sub, prefix, w, k, align), boot_n, seed + 10 * i + j)
                       for j, w in enumerate(windows)}
    return table


def pretrend_status(t: float) -> str:
    if t is None or not np.isfinite(t):
        return "untestable"
    return "ok" if abs(t) < config.PRETREND_T else "flag"


# ------------------------------------------------------------------------------------------- H6 / R8 / R10
def _rank_terciles(x: pd.Series) -> pd.Series:
    pct = x.rank(pct=True, method="average")
    return np.ceil(3 * pct).clip(1, 3)


def within_year_terciles(dates: pd.Series, dv: pd.Series) -> tuple[pd.Series, int]:
    """Terciles (1..3) of B's dollar volume formed within each event year; years with fewer than
    H6_MIN_YEAR_EVENTS finite values get none. Returns (tercile per row, number of excluded finite rows)."""
    dates = pd.to_datetime(pd.Series(dates).reset_index(drop=True))
    dv = pd.Series(dv).reset_index(drop=True).astype(float)
    out = pd.Series(np.nan, index=dv.index)
    excluded = 0
    for _, idx in dv[np.isfinite(dv)].groupby(dates.dt.year[np.isfinite(dv)]).groups.items():
        if len(idx) < config.H6_MIN_YEAR_EVENTS:
            excluded += len(idx)
            continue
        out.loc[idx] = _rank_terciles(dv.loc[idx])
    return out, excluded


def extractor_families(ev: pd.DataFrame) -> dict:
    """R8 subsets by extractor family ('llm', 'llm:<model>' -> llm; 'manual', 'manual:<x>' -> manual). Any
    other family is an unexpected study link and stops the computation; a link in both is in both subsets."""
    fams = ev["extractors"].fillna("").map(lambda s: {t.split(":")[0] for t in str(s).split("|")})
    other = sorted({f for fs in fams for f in fs} - {"llm", "manual"})
    if other:
        n = int(sum(bool(fs - {"llm", "manual"}) for fs in fams))
        raise ValueError(f"R8: {n} event(s) carry extractor families outside llm/manual: {other}")
    llm = fams.map(lambda fs: "llm" in fs).to_numpy()
    manual = fams.map(lambda fs: "manual" in fs).to_numpy()
    return {"llm": llm, "manual": manual, "overlap": int((llm & manual).sum())}


def b_coshock(ev: pd.DataFrame, shocks: pd.DataFrame) -> np.ndarray:
    """Exploratory R10: True where B itself had a shock (same rule) on a day in [e-10, e]."""
    by_firm = {fid: np.sort(g["e_idx"].to_numpy(dtype=np.int64)) for fid, g in shocks.groupby("firm_id")}
    e = ev["e_idx"].to_numpy(dtype=np.int64)
    out = np.zeros(len(ev), dtype=bool)
    for i, (b, ei) in enumerate(zip(ev["b_id"], e)):
        s = by_firm.get(b)
        if s is not None:
            j = np.searchsorted(s, ei - config.COSHOCK_LOOKBACK, side="left")
            out[i] = j < len(s) and s[j] <= ei
    return out


def _r10(ev: pd.DataFrame, shocks: pd.DataFrame) -> dict:
    flag = b_coshock(ev, shocks) if len(ev) else np.zeros(0, dtype=bool)
    share = {r: (_f(flag[(ev["role"] == r).to_numpy()].mean()) if (ev["role"] == r).any() else math.nan)
             for r in config.ROLES}
    return {"label": "exploratory (not pre-registered): events whose B had its own shock in [e-10, e] removed "
                     "(B shocks before 2015 are not stored, so the first days of 2015 are slightly undercounted)",
            "share_coshock": share,
            "table": role_window_table(ev[~flag], config.K_PRIMARY, None, 0, config.SLOW_WINDOWS)}


def _splits(ev: pd.DataFrame) -> dict:
    absz = ev["z_a"].abs()
    buckets = {
        "shock_sign": {"up": ev["s"] == 1, "down": ev["s"] == -1},
        "shock_size": {"3to4_sigma": absz < 4, "ge4_sigma": absz >= 4},
        "abnormal_volume": {"gt2x": ev["abn_vol_a"] > 2, "le2x": ev["abn_vol_a"] <= 2},
        "link_weight": {"weighted": ev["weight"].notna(), "unweighted": ev["weight"].isna()},
    }
    weighted = ev["weight"].notna()
    if weighted.sum() >= 30:
        t = _rank_terciles(ev.loc[weighted, "weight"])
        for k in (1, 2, 3):
            buckets["link_weight"][f"weight_tercile_{k}"] = ev.index.isin(t[t == k].index)
    terc, excluded = within_year_terciles(ev["date"], ev["dv_b"])
    buckets["b_size"] = {f"dv_tercile_{k}_within_year": (terc == k).to_numpy() for k in (1, 2, 3)}
    out: dict = {"_notes": {"b_size_excluded_events": excluded,
                            "b_size_rule": f"terciles of B's [-80,-21] dollar volume within each event year with "
                                           f">= {config.H6_MIN_YEAR_EVENTS} events"}}
    for split, parts in buckets.items():
        out[split] = {}
        for name, mask in parts.items():
            sub = ev[np.asarray(mask, dtype=bool)]
            out[split][name] = role_window_table(sub, config.K_PRIMARY, None, 0, config.SLOW_WINDOWS)
    return out


def _gamma(ev: pd.DataFrame, shocks: pd.DataFrame) -> dict:
    ar_a = ev["shock_id"].map(shocks.set_index("shock_id")["ar"]).to_numpy(dtype=float)
    s = ev["s"].to_numpy(dtype=float)
    out = {}
    for role in config.ROLES:
        mask = (ev["role"] == role).to_numpy()
        out[role] = {}
        for w in ("d0",) + config.SLOW_WINDOWS:
            y = s[mask] * ev.loc[mask, f"ripple_{w}_k{config.K_PRIMARY}"].to_numpy(dtype=float)  # unsigned
            r = st.clustered_ols(y, ar_a[mask], ev.loc[mask, "date"].to_numpy())
            out[role][w] = {k: (_f(v) if isinstance(v, float) else v) for k, v in r.items()}
    return out


def _robustness_exploration(ev: pd.DataFrame, shocks: pd.DataFrame) -> dict:
    d = ev["date"]
    covid = (d >= pd.Timestamp(config.COVID_EXCLUDE[0])) & (d <= pd.Timestamp(config.COVID_EXCLUDE[1]))
    ind = ev["sector_etf_z"].abs() >= 2
    fam = extractor_families(ev)
    slow = config.SLOW_WINDOWS
    return {
        "r4_ex_covid": role_window_table(ev[~covid], config.K_PRIMARY, None, 0, slow),
        "r5_ex_industry_shocks": role_window_table(ev[~ind.fillna(False)], config.K_PRIMARY, None, 0, slow),
        "r7_k3": role_window_table(ev, 3, None, 0, slow),
        "r7_k10": role_window_table(ev, 10, None, 0, slow),
        "r8_llm_only": role_window_table(ev[fam["llm"]], config.K_PRIMARY, None, 0, slow),
        "r8_manual_only": role_window_table(ev[fam["manual"]], config.K_PRIMARY, None, 0, slow),
        "r8_overlap_events": fam["overlap"],
        "r9_raw_moves": {
            "linked_b": role_window_table(ev, config.K_PRIMARY, None, 0, ("d0",) + slow, "raw_b", align=True),
            "controls": role_window_table(ev, config.K_PRIMARY, None, 0, ("d0",) + slow, "raw_c", align=True),
        },
        "r10_ex_b_coshock": _r10(ev, shocks),
    }


def _embargo_counts(ev: pd.DataFrame, horizon_row: int | None) -> dict | None:
    if horizon_row is None:
        return None
    e = ev["e_idx"].to_numpy(dtype=np.int64)
    out = {}
    for r in config.ROLES:
        m = (ev["role"] == r).to_numpy()
        out[r] = {w: int((m & (e + hi > horizon_row)).sum()) for w, (_, hi) in config.WINDOWS.items()}
    return out


# ------------------------------------------------------------------------------------------- exploration
def analyze_exploration(events: pd.DataFrame, shocks: pd.DataFrame, boot_n: int | None = None,
                        seed: int | None = None, horizon_row: int | None = None) -> dict:
    boot_n = config.BOOT_N if boot_n is None else boot_n
    seed = config.SEED if seed is None else seed
    ev = events[events["period"] == "exploration"].reset_index(drop=True)
    table = role_window_table(ev, config.K_PRIMARY, boot_n, seed)
    H1 = {}
    for role in config.ROLES:
        c = table[role]["d0"]
        H1[role] = {"mean": c["mean"], "t": c["t"],
                    "pass_exploration": bool(np.isfinite(c["t"]) and c["mean"] > 0 and c["t"] > config.T_BAR)}
    keys = [(r, w) for r in config.ROLES for w in config.SLOW_WINDOWS]
    rejected = st.holm([table[r][w]["p_two"] for r, w in keys], config.FAMILY_ALPHA)
    H2 = {}
    for (r, w), rej in zip(keys, rejected):
        c = table[r][w]
        finite = np.isfinite(c["t"])
        passed = bool(rej and finite and abs(c["t"]) > config.T_BAR)
        t2 = c.get("t_2w", math.nan)
        H2[f"{r}|{w}"] = {"mean": c["mean"], "t": c["t"], "p_two": c["p_two"], "holm": bool(rej),
                          "direction": int(np.sign(c["mean"])) if np.isfinite(c["mean"]) else 0,
                          "pass_exploration": passed, "t_2w": t2,
                          "flag_2w": bool(passed and not (np.isfinite(t2) and abs(t2) > config.T_BAR))}
    H3 = {}
    for role in config.ROLES:
        t = table[role]["pre"]["t"]
        applies = H1[role]["pass_exploration"] or any(H2[f"{role}|{w}"]["pass_exploration"]
                                                      for w in config.SLOW_WINDOWS)
        H3[role] = {"t_pre": t, "status_exploration": pretrend_status(t), "applies": bool(applies)}
    sh = shocks[shocks["period"] == "exploration"]
    H5 = {w: _cell(sh, sh[f"cara_{w}"].to_numpy(dtype=float), None, 0) for w in config.SLOW_WINDOWS}
    counts = {"events": {r: int((ev["role"] == r).sum()) for r in config.ROLES},
              "event_dates": int(ev["date"].nunique()), "shocks": int(len(sh))}
    emb = _embargo_counts(ev, horizon_row)
    if emb is not None:
        counts["embargoed"] = emb
    return {"period": "exploration", "table": table, "H1": H1, "H2": H2, "H3": H3, "H5": H5,
            "H6": _splits(ev), "gamma": _gamma(ev, shocks), "robustness": _robustness_exploration(ev, shocks),
            "counts": counts}


# ------------------------------------------------------------------------------------------- placebo
def placebo_setup(panel: rt.Panel, model: rt.AbnormalModel, shocks: pd.DataFrame, links: pd.DataFrame,
                  firms: pd.DataFrame, events_h: pd.DataFrame, role: str, window: str) -> dict:
    col = f"ripple_{window}_k{config.K_PRIMARY}"
    ev = events_h[(events_h["period"] == "holdout") & (events_h["role"] == role)]
    real = ev[np.isfinite(ev[col].to_numpy(dtype=float))]
    f = firms.set_index("firm_id")
    sec_of = f["sector"].to_dict()
    col_of = {fid: panel.col[s] for fid, s in f["symbol"].items() if s in panel.col}
    members: dict[str, list] = {}
    for fid in sorted(col_of):
        members.setdefault(sec_of.get(fid), []).append(fid)
    role_links = links[links["role"] == role]
    a_of_b = {b: set(g["a_id"]) for b, g in role_links.groupby("b_id")}
    pools: dict[str, list] = {}
    for b, g in real.groupby("b_id"):
        es = np.unique(g["e_idx"].to_numpy(dtype=np.int64))
        cand = [c for c in members.get(sec_of.get(b), []) if c != b and c not in a_of_b.get(b, set())]
        if cand and len(es):
            cc = np.array([col_of[c] for c in cand], dtype=np.int64)
            ee, ff = np.meshgrid(es, cc, indexing="xy")             # (len(cand), len(es))
            usable = (model.nest[ff, ee] >= config.MIN_EST_DAYS) & np.isfinite(model.coef[ff, ee, 0])
            dv = rt.dollar_volume(panel, ff.ravel(), ee.ravel()).reshape(ff.shape)
            ok = (usable & np.isfinite(dv) & (dv > 0)).all(axis=1)
            cand = [c for c, k in zip(cand, ok) if k]
        pools[b] = cand
    rl = role_links[role_links["b_id"].isin(pools)].sort_values(["b_id", "a_id"]).reset_index(drop=True)
    study = {frozenset(p) for p in links[["a_id", "b_id"]].itertuples(index=False)}
    return {"role": role, "window": window, "col": col, "pools": pools, "links": rl, "study": study,
            "real_ids": set(real["event_id"]), "real_count": int(len(real)), "a_ids": set(rl["a_id"])}


def placebo_links(setup: dict, rng: np.random.Generator) -> tuple[pd.DataFrame, dict]:
    """One permutation: each real B -> one B' drawn with replacement from its pool (sorted B order, one rng);
    relabelled pairs that are study links (or B' == A) are dropped, never redrawn."""
    pick = {}
    dropped_b = 0
    for b in sorted(setup["pools"]):
        pool = setup["pools"][b]
        if pool:
            pick[b] = pool[int(rng.integers(len(pool)))]
        else:
            dropped_b += 1
    rl = setup["links"]
    rl = rl[rl["b_id"].isin(pick)].copy()
    rl["real_b"] = rl["b_id"]
    rl["b_id"] = rl["real_b"].map(pick)
    keep = np.array([a != b and frozenset((a, b)) not in setup["study"] for a, b in zip(rl["a_id"], rl["b_id"])],
                    dtype=bool)
    n_links = len(setup["links"])
    out = rl[keep].drop_duplicates(["a_id", "b_id", "role"]).reset_index(drop=True)
    return out, {"dropped_link_share": (1 - len(out) / n_links) if n_links else math.nan, "dropped_b": dropped_b}


def placebo_distribution(panel, model, shocks, links, firms, pairs, events_h, role: str, window: str,
                         direction: int, n: int, seed: int) -> dict:
    setup = placebo_setup(panel, model, shocks, links, firms, events_h, role, window)
    rng = np.random.default_rng(seed)
    sh = shocks[(shocks["period"] == "holdout") & shocks["firm_id"].isin(setup["a_ids"])]
    vals, cover, dropped, dropped_b = [], [], [], []
    for _ in range(n):
        rl, info = placebo_links(setup, rng)
        dropped.append(info["dropped_link_share"])
        dropped_b.append(info["dropped_b"])
        if rl.empty:
            vals.append(math.nan)
            cover.append(0)
            continue
        ev, _ = be.build_events_for_links(panel, model, sh, rl.drop(columns=["real_b"]), firms, pairs, "holdout")
        if ev.empty:
            vals.append(math.nan)
            cover.append(0)
            continue
        real_b = ev.set_index(["a_id", "b_id"]).index.map(
            rl.set_index(["a_id", "b_id"])["real_b"].to_dict())
        real_eid = ev["shock_id"] + ">" + pd.Series(real_b, index=ev.index).astype(str) + ">" + ev["role"]
        x = ev[setup["col"]].to_numpy(dtype=float)
        keep = real_eid.isin(setup["real_ids"]).to_numpy() & np.isfinite(x)
        cover.append(int(keep.sum()))
        vals.append(direction * float(x[keep].mean()) if keep.any() else math.nan)
    sizes = [len(p) for p in setup["pools"].values()]
    return {"values": np.array(vals, dtype=float), "coverage": np.array(cover, dtype=float),
            "pool_median": float(np.median(sizes)) if sizes else math.nan, "pool_min": int(min(sizes)) if sizes else 0,
            "dropped_link_share": _f(np.nanmean(dropped)) if dropped else math.nan,
            "dropped_b": int(np.max(dropped_b)) if dropped_b else 0, "real_count": setup["real_count"]}


def placebo_summary(values: np.ndarray, coverage: np.ndarray, real_count: int, real_se: float, n: int) -> dict:
    values = np.asarray(values, dtype=float)
    fin = np.isfinite(values)
    n_fin = int(fin.sum())
    need = math.ceil(config.PLACEBO_MIN_FINITE * n)
    med_cov = float(np.median(coverage)) if len(coverage) else 0.0
    out = {"run": True, "n": int(n), "n_finite": n_fin, "median_coverage": med_cov, "real_count": int(real_count),
           "p50": _f(np.median(values[fin])) if n_fin else math.nan,
           "p95": _f(np.percentile(values[fin], 95)) if n_fin else math.nan,
           "sd": _f(np.std(values[fin], ddof=1)) if n_fin > 1 else math.nan}
    out["sd_ratio"] = _f(out["sd"] / real_se) if np.isfinite(out["sd"]) and real_se and real_se > 0 else math.nan
    out["sd_ratio_flag"] = bool(np.isfinite(out["sd_ratio"]) and out["sd_ratio"] < config.PLACEBO_SD_RATIO_FLAG)
    if n_fin < need:
        out.update({"evaluable": False, "reason": f"placebo not evaluable: {n_fin} of {n} permutations finite"})
    elif real_count and med_cov < config.PLACEBO_MIN_COVERAGE * real_count:
        out.update({"evaluable": False, "reason": f"placebo not evaluable: median coverage {med_cov:.0f} of "
                                                  f"{real_count} real events"})
    else:
        out.update({"evaluable": True, "reason": ""})
    return out


# ------------------------------------------------------------------------------------------- verdict
def _validate_power(power: dict | None) -> None:
    if not isinstance(power, dict) or not isinstance(power.get("roles"), dict):
        raise ValueError("the §7 verdict needs the power check (spec §5): power file missing or malformed")
    for r in config.ROLES:
        u = power["roles"].get(r, {}).get("underpowered")
        if not isinstance(u, bool):
            raise ValueError(f"power file has no boolean 'underpowered' for role {r!r}")


def verdict(expl: dict, hold: dict, power: dict | None) -> dict:
    _validate_power(power)
    by_role, details = {}, {}
    for role in config.ROLES:
        keys = [f"{role}|{w}" for w in config.SLOW_WINDOWS]
        h2_pass = any(hold["H2"][k].get("pass", False) for k in keys)
        unevaluable = any(expl["H2"][k].get("pass_exploration", False) and hold["H2"][k].get("unevaluable", False)
                          for k in keys)
        under = power["roles"][role]["underpowered"]
        h1 = bool(hold["H1"][role].get("pass", False))
        if h2_pass:
            state = "slow_ripple"
        elif (under or unevaluable) and h1:
            state = "same_day_slow_inconclusive"
        elif under:
            state = "inconclusive_underpowered"
        elif unevaluable:
            state = "inconclusive_unevaluable"
        elif h1:
            state = "same_day_only"
        else:
            state = "no_ripple"
        by_role[role] = state
        details[role] = {
            "exploration_candidate": bool(expl["H1"][role].get("pass_exploration", False)
                                          or any(expl["H2"][k].get("pass_exploration", False) for k in keys)),
            "underpowered": under, "unevaluable": unevaluable,
            "borderline": bool(power["roles"][role].get("borderline", False)),
            "borderline_reasons": list(power["roles"][role].get("borderline_reasons", [])),
            "h3": (hold.get("H3", {}).get(role, {}) or {}).get("status"),
        }
    states = list(by_role.values())
    inconc = [s in INCONCLUSIVE for s in states]
    if "slow_ripple" in states:
        overall = "slow_ripple"
    elif any(s in ("same_day_only", "same_day_slow_inconclusive") for s in states):
        overall = "same_day_some_inconclusive" if any(inconc) else "same_day_only"
    elif all(s.startswith("inconclusive") for s in states):
        overall = "inconclusive"
    elif any(inconc):
        overall = "no_ripple_powered_roles"
    else:
        overall = "no_ripple"
    return {"overall": overall, "by_role": by_role, "details": details}


# ------------------------------------------------------------------------------------------- holdout
def analyze_holdout(panel, model, shocks, links, firms, pairs, events_h, expl: dict, *, power: dict | None,
                    primary: bool = True, placebo_n: int | None = None, boot_n: int | None = None,
                    seed: int | None = None) -> dict:
    io.require_holdout_unlocked()
    if primary:
        _validate_power(power)
        placebo_n = config.PLACEBO_N if placebo_n is None else placebo_n
        if placebo_n != config.PLACEBO_N:
            raise ValueError(f"the primary holdout run needs exactly {config.PLACEBO_N} placebo permutations "
                             f"(spec §5); got {placebo_n}")
    boot_n = config.BOOT_N if boot_n is None else boot_n
    seed = config.SEED if seed is None else seed
    ev = events_h[events_h["period"] == "holdout"].reset_index(drop=True)
    table = role_window_table(ev, config.K_PRIMARY, boot_n, seed + 7)
    H1 = {}
    for role in config.ROLES:
        c, ex = table[role]["d0"], expl["H1"][role]
        p1 = st.one_sided_p(c["t"], c["df"], +1)
        H1[role] = {"mean": c["mean"], "t": c["t"], "p_one": _f(p1), "pass_exploration": ex["pass_exploration"],
                    "pass": bool(ex["pass_exploration"] and np.isfinite(c["mean"]) and c["mean"] > 0
                                 and np.isfinite(p1) and p1 < config.HOLDOUT_ALPHA)}
    H2 = {}
    for idx, (key, ex) in enumerate(expl["H2"].items()):
        r, w = key.split("|")
        d = ex["direction"] or 1
        c = table[r][w]
        p1 = st.one_sided_p(c["t"], c["df"], d)
        replicates = bool(np.isfinite(p1) and p1 < config.HOLDOUT_ALPHA)
        dmean = d * c["mean"] if np.isfinite(c["mean"]) else math.nan
        floor_ok = bool(np.isfinite(dmean) and dmean >= config.FLOOR)
        unevaluable = bool(ex["pass_exploration"] and not np.isfinite(p1))
        placebo: dict = {"run": False, "reason": "not an exploration candidate"}
        beats: bool | None = False
        if ex["pass_exploration"]:
            if primary:
                dist = placebo_distribution(panel, model, shocks, links, firms, pairs, events_h, r, w, d,
                                            placebo_n, seed + 1000 + idx)
                placebo = placebo_summary(dist["values"], dist["coverage"], dist["real_count"], c["se"], placebo_n)
                placebo.update({k: dist[k] for k in ("pool_median", "pool_min", "dropped_link_share", "dropped_b")})
                beats = bool(placebo["evaluable"] and np.isfinite(placebo["p95"]) and dmean > placebo["p95"])
                unevaluable = unevaluable or not placebo["evaluable"]
            else:
                beats, placebo = None, {"run": False, "reason": "robustness variant: no placebo (§8)"}
        passed = bool(ex["pass_exploration"] and replicates and floor_ok and (beats is True if primary else True))
        p2 = c.get("p_one_pos_2w" if d > 0 else "p_one_neg_2w", math.nan)
        H2[key] = {"mean": c["mean"], "direction_mean": _f(dmean), "t": c["t"], "p_one": _f(p1), "direction": d,
                   "pass_exploration": ex["pass_exploration"], "replicates": replicates, "floor_ok": floor_ok,
                   "beats_placebo": beats, "placebo": placebo, "unevaluable": bool(unevaluable and not passed),
                   "pass": passed, "p_one_2w": p2,
                   "flag_2w": bool(passed and not (np.isfinite(p2) and p2 < config.HOLDOUT_ALPHA))}
    H3 = {}
    for role in config.ROLES:
        t = table[role]["pre"]["t"]
        s_h = pretrend_status(t)
        s_x = expl["H3"][role]["status_exploration"]
        status = "flag" if "flag" in (s_h, s_x) else ("untestable" if "untestable" in (s_h, s_x) else "ok")
        applies = bool(H1[role]["pass"] or any(H2[f"{role}|{w}"]["pass"] for w in config.SLOW_WINDOWS)
                       or expl["H3"][role].get("applies", False))
        H3[role] = {"t_pre": t, "status_holdout": s_h, "status_exploration": s_x, "status": status,
                    "applies": applies}
    sh = shocks[shocks["period"] == "holdout"]
    H5 = {}
    if len(sh):
        cols = np.array([panel.col[s] for s in sh["symbol"]])
        e, s = sh["e_idx"].to_numpy(dtype=np.int64), sh["s"].to_numpy(dtype=float)
        for w in config.SLOW_WINDOWS:
            a, b = config.WINDOWS[w]
            H5[w] = _cell(sh, s * model.car(cols, e, a, b), None, 0)
    late = ev[ev["date"] >= pd.Timestamp(config.HOLDOUT_LATE_START)]
    res = {"period": "holdout", "table": table, "H1": H1, "H2": H2, "H3": H3, "H5": H5,
           "robustness": {"r6_holdout_2024_on": role_window_table(late, config.K_PRIMARY, None, 0,
                                                                  config.SLOW_WINDOWS),
                          "r10_ex_b_coshock": _r10(ev, shocks)},          # late-2021 B shocks count too
           "counts": {"events": {r: int((ev["role"] == r).sum()) for r in config.ROLES},
                      "event_dates": int(ev["date"].nunique()), "shocks": int(len(sh))}}
    res["verdict"] = verdict(expl, res, power) if primary else None
    if not primary:
        res["verdict_note"] = "robustness variant (§8): reported, never changes the verdict"
    return res


# ------------------------------------------------------------------------------------------- CAR paths
def compute_paths(panel, model, events, controls, shocks, period: str, k: int = config.K_PRIMARY) -> pd.DataFrame:
    """Average signed CAR paths, day -5..+60, on the w60 ripple sample: events with a finite ripple_w60_kK and,
    for each, the first-K controls whose [+1,+60] window is complete; A from shocks with a complete [+1,+60]
    window (exploration: finite, embargoed cara_w60)."""
    lo, hi = config.PATH_DAYS
    days = np.arange(lo, hi + 1)
    rows: list = []

    def add(role: str, series: str, daily: np.ndarray) -> None:
        if daily.size == 0:
            return
        with np.errstate(invalid="ignore"):
            mean = np.nanmean(np.where(np.isfinite(daily), daily, np.nan), axis=0)
        cnt = np.isfinite(daily).sum(axis=0)
        car = np.cumsum(np.nan_to_num(mean))
        rows.extend((role, series, int(d), float(c), int(n)) for d, c, n in zip(days, car, cnt))

    w_lo, w_hi = config.WINDOWS["w60"]
    sh = shocks[shocks["period"] == period]
    if len(sh):
        cols = np.array([panel.col[s] for s in sh["symbol"]])
        e = sh["e_idx"].to_numpy(dtype=np.int64)
        if period == "exploration":
            keep = np.isfinite(sh["cara_w60"].to_numpy(dtype=float))
        else:
            keep = np.isfinite(model.car(cols, e, w_lo, w_hi))
        if keep.any():
            add("all", "A", model.path(cols[keep], e[keep], lo, hi) * sh["s"].to_numpy(dtype=float)[keep][:, None])
    ev = events[(events["period"] == period)
                & np.isfinite(events[f"ripple_w60_k{k}"].to_numpy(dtype=float))]
    for role in config.ROLES:
        sub = ev[ev["role"] == role].reset_index(drop=True)
        if sub.empty:
            continue
        e, s = sub["e_idx"].to_numpy(dtype=np.int64), sub["s"].to_numpy(dtype=float)
        b = model.path(np.array([panel.col[x] for x in sub["b_symbol"]]), e, lo, hi) * s[:, None]
        ctl = controls[controls["event_id"].isin(set(sub["event_id"])) & (controls["rank"] <= k)]
        pos = {eid: i for i, eid in enumerate(sub["event_id"])}
        ci = ctl["event_id"].map(pos).to_numpy()
        ccols = np.array([panel.col[x] for x in ctl["c_symbol"]], dtype=np.int64)
        complete = np.isfinite(model.car(ccols, e[ci], w_lo, w_hi))
        ci, ccols = ci[complete], ccols[complete]
        cp = model.path(ccols, e[ci], lo, hi) * s[ci][:, None]
        sums = np.zeros((len(sub), len(days)))
        cnts = np.zeros((len(sub), len(days)))
        np.add.at(sums, ci, np.nan_to_num(cp))
        np.add.at(cnts, ci, np.isfinite(cp))
        cavg = np.where(cnts > 0, sums / np.maximum(cnts, 1), np.nan)
        add(role, "B", b)
        add(role, "controls", cavg)
        add(role, "ripple", b - cavg)
    df = pd.DataFrame(rows, columns=list(PATH_SCHEMA))
    df["day"] = df["day"].astype("int64")
    df["n"] = df["n"].astype("int64")
    return df


# ------------------------------------------------------------------------------------------- stage
def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_power(variant: str, rule: str, events_exploration: pd.DataFrame) -> tuple[dict, str]:
    """The power file this run depends on: required, pinned, and fresh against the exploration events."""
    sfx = config.variant_suffix(variant, rule)
    path = io.store_dir() / f"power{sfx}.json"
    if not path.exists():
        raise FileNotFoundError(f"power{sfx}.json missing: the power check runs first (spec §5)")
    power = json.loads(path.read_text(encoding="utf-8"))
    meta = power.get("meta") or {}
    want = {"method_version": config.POWER_METHOD_VERSION, "n_boot": config.POWER_BOOT_N, "seed": config.SEED,
            "variant": variant, "rule": rule}
    for k, v in want.items():
        if meta.get(k) != v:
            raise ValueError(f"power{sfx}.json was not produced by the registered power check: {k}={meta.get(k)!r}, "
                             f"expected {v!r}")
    if meta.get("events_fingerprint") != pc.events_fingerprint(events_exploration):
        raise ValueError(f"power{sfx}.json is stale: it does not match the current exploration events")
    _validate_power(power)
    return power, _sha256(path)


def run(period: str = "exploration", variant: str = "market", rule: str = "sigma3",
        boot_n: int | None = None, placebo_n: int | None = None,
        panel: rt.Panel | None = None, model: rt.AbnormalModel | None = None) -> dict:
    if period not in ("exploration", "holdout"):
        raise ValueError(f"unknown period {period!r}")
    if period == "holdout":
        io.require_holdout_unlocked()
    primary = (variant, rule) == config.PRIMARY_RUN
    sfx = config.variant_suffix(variant, rule)
    store = io.store_dir()
    events_x = io.read_frame(f"events_exploration{sfx}", be.event_schema())
    power, power_sha = load_power(variant, rule, events_x)
    events = events_x if period == "exploration" else io.read_frame(f"events_{period}{sfx}", be.event_schema())
    controls = io.read_frame(f"controls_{period}{sfx}", be.CONTROL_SCHEMA)
    shocks = io.read_frame(f"shocks{sfx}", bs.SHOCK_SCHEMA)
    if panel is None or model is None:
        from research.dominoes import context
        panel, model = context.load(variant)
    be.check_calendar(panel, events, "events")
    be.check_calendar(panel, shocks, "shocks")
    if period == "exploration":
        res = analyze_exploration(events, shocks, boot_n, horizon_row=config.exploration_last_row(panel.dates))
    else:
        expl = json.loads((store / f"results_exploration{sfx}.json").read_text(encoding="utf-8"))
        if expl.get("power_sha256") != power_sha:
            raise ValueError("the power file changed after the exploration was measured: refusing the holdout")
        res = analyze_holdout(panel, model, shocks, io.read_frame("links", snapshot_links.LINK_SCHEMA),
                              io.read_frame("firms", snapshot_links.FIRM_SCHEMA),
                              io.read_frame("any_edge_pairs", snapshot_links.PAIR_SCHEMA),
                              events, expl, power=power, primary=primary, placebo_n=placebo_n, boot_n=boot_n)
    res.update({"variant": variant, "rule": rule, "power_sha256": power_sha})
    (store / f"results_{period}{sfx}.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    io.write_frame(f"paths_{period}{sfx}", compute_paths(panel, model, events, controls, shocks, period),
                   PATH_SCHEMA)
    return res
