"""Stage: measure the pre-registered hypotheses (spec §5-§8, §13).

Exploration (2015-2021): per role x window the date-clustered mean of the primary ripple (K=5), with a
week-clustered t and a date-block bootstrap CI; H1 (same-day follow, t > 3), the H2 family (9 tests:
Holm on two-sided p + |t| > 3, direction = sign of the mean), H3 pre-trend, H5 first-domino drift,
H6 hypothesis-generating splits, the transmission coefficient gamma, and robustness subsets 4/5/7/8/9.

Holdout (2022+, only after the exploration readout is committed): one-sided replication in the
exploration direction, the 50 bp floor, the placebo (random same-sector B for the same shocked A, never
linked to A), H3 in both periods, robustness 6, and the §7 verdict.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

from research.dominoes import build_events as be
from research.dominoes import build_shocks as bs
from research.dominoes import config, io, snapshot_links
from research.dominoes import returns as rt
from research.dominoes import stats as st

PATH_SCHEMA = {"role": "str", "series": "str", "day": "int", "car": "float", "n": "int"}
WINDOW_KEYS = tuple(config.WINDOWS)


def _f(x) -> float:
    return float(x) if x is not None and np.isfinite(x) else math.nan


def _cell(df: pd.DataFrame, col: str, boot_n: int | None, seed: int) -> dict:
    v = df[col].to_numpy(dtype=float)
    r = st.clustered_mean(v, df["date"].to_numpy())
    out = {"mean": _f(r["mean"]), "se": _f(r["se"]), "t": _f(r["t"]), "n": int(r["n"]), "g": int(r["g"]),
           "df": int(r["df"]), "p_two": _f(r["p_two"])}
    if boot_n:
        out["t_week"] = _f(st.clustered_mean(v, df["week"].to_numpy())["t"])
        lo, hi = st.block_bootstrap_ci(v, df["date"].to_numpy(), boot_n, seed) if r["n"] else (math.nan, math.nan)
        out["ci_boot"] = [_f(lo), _f(hi)]
    return out


def role_window_table(ev: pd.DataFrame, k: int, boot_n: int | None, seed: int,
                      windows=WINDOW_KEYS, prefix: str = "ripple") -> dict:
    table = {}
    for i, role in enumerate(config.ROLES):
        sub = ev[ev["role"] == role]
        table[role] = {w: _cell(sub, f"{prefix}_{w}_k{k}" if prefix != "raw_b" else f"raw_b_{w}",
                                boot_n, seed + 10 * i + j) for j, w in enumerate(windows)}
    return table


def _splits(ev: pd.DataFrame) -> dict:
    absz = ev["z_a"].abs()
    buckets = {
        "shock_sign": {"up": ev["s"] == 1, "down": ev["s"] == -1},
        "shock_size": {"3to4_sigma": absz < 4, "ge4_sigma": absz >= 4},
        "abnormal_volume": {"gt2x": ev["abn_vol_a"] > 2, "le2x": ev["abn_vol_a"] <= 2},
        "link_weight": {"weighted": ev["weight"].notna(), "unweighted": ev["weight"].isna()},
    }
    weighted = ev[ev["weight"].notna()]
    if len(weighted) >= 30:
        q = pd.qcut(weighted["weight"], 3, labels=False, duplicates="drop")
        for t in sorted(q.dropna().unique()):
            buckets["link_weight"][f"weight_tercile_{int(t) + 1}"] = ev.index.isin(q[q == t].index)
    if ev["dv_b"].notna().sum() >= 3:
        q = pd.qcut(ev["dv_b"], 3, labels=False, duplicates="drop")
        buckets["b_size"] = {f"dv_tercile_{int(t) + 1}": (q == t) for t in sorted(q.dropna().unique())}
    out: dict = {}
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


def _robustness_exploration(ev: pd.DataFrame) -> dict:
    d = ev["date"]
    covid = (d >= pd.Timestamp(config.COVID_EXCLUDE[0])) & (d <= pd.Timestamp(config.COVID_EXCLUDE[1]))
    ind = ev["sector_etf_z"].abs() >= 2
    slow = config.SLOW_WINDOWS
    out = {
        "r4_ex_covid": role_window_table(ev[~covid], config.K_PRIMARY, None, 0, slow),
        "r5_ex_industry_shocks": role_window_table(ev[~ind.fillna(False)], config.K_PRIMARY, None, 0, slow),
        "r7_k3": role_window_table(ev, 3, None, 0, slow),
        "r7_k10": role_window_table(ev, 10, None, 0, slow),
        "r8_llm_only": role_window_table(ev[ev["extractors"].str.contains("llm:")], config.K_PRIMARY, None, 0, slow),
        "r8_manual_only": role_window_table(ev[ev["extractors"].str.contains("manual:")], config.K_PRIMARY,
                                            None, 0, slow),
        "r9_raw_moves": {
            "linked_b": role_window_table(ev, config.K_PRIMARY, None, 0, ("d0",) + slow, prefix="raw_b"),
            "controls": role_window_table(ev, config.K_PRIMARY, None, 0, ("d0",) + slow, prefix="raw_c"),
        },
    }
    return out


def analyze_exploration(events: pd.DataFrame, shocks: pd.DataFrame, boot_n: int = config.BOOT_N,
                        seed: int = config.SEED) -> dict:
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
        H2[f"{r}|{w}"] = {"mean": c["mean"], "t": c["t"], "p_two": c["p_two"], "holm": bool(rej),
                          "direction": int(np.sign(c["mean"])) if np.isfinite(c["mean"]) else 0,
                          "pass_exploration": bool(rej and finite and abs(c["t"]) > config.T_BAR)}
    H3 = {}
    for role in config.ROLES:
        t = table[role]["pre"]["t"]
        H3[role] = {"t_pre": t, "ok_exploration": bool(not np.isfinite(t) or abs(t) < config.PRETREND_T)}
    sh = shocks[shocks["period"] == "exploration"]
    H5 = {w: _cell(sh.assign(week=""), f"cara_{w}", None, 0) for w in config.SLOW_WINDOWS}
    return {
        "period": "exploration", "table": table, "H1": H1, "H2": H2, "H3": H3, "H5": H5,
        "H6": _splits(ev), "gamma": _gamma(ev, shocks), "robustness": _robustness_exploration(ev),
        "counts": {"events": {r: int((ev["role"] == r).sum()) for r in config.ROLES},
                   "event_dates": int(ev["date"].nunique()), "shocks": int(len(sh))},
    }


def placebo_distribution(panel: rt.Panel, model: rt.AbnormalModel, shocks: pd.DataFrame, links: pd.DataFrame,
                         firms: pd.DataFrame, pairs: pd.DataFrame, role: str, window: str, direction: int,
                         n: int, seed: int) -> np.ndarray:
    """Holdout null: keep each real link's shocked firm A and role, replace B by a random firm from B's
    sector that is not A and has no edge of any kind to A; rebuild events; record direction * mean ripple."""
    rng = np.random.default_rng(seed)
    real = links[links["role"] == role].reset_index(drop=True)
    if real.empty or n <= 0:
        return np.array([])
    f = firms.set_index("firm_id")
    sec_of = f["sector"].to_dict()
    in_panel = sorted(fid for fid, sym in f["symbol"].items() if sym in panel.col)
    members: dict[str, list] = {}
    for fid in in_panel:
        members.setdefault(sec_of.get(fid), []).append(fid)
    linked: dict[str, set] = {}
    for x, y in pairs[["x_id", "y_id"]].itertuples(index=False):
        linked.setdefault(x, set()).add(y)
        linked.setdefault(y, set()).add(x)
    cand = [[c for c in members.get(sec_of.get(row.b_id), []) if c != row.a_id and c not in linked.get(row.a_id, set())]
            for row in real.itertuples(index=False)]
    sh = shocks[(shocks["period"] == "holdout") & shocks["firm_id"].isin(set(real["a_id"]))]
    col = f"ripple_{window}_k{config.K_PRIMARY}"
    vals = []
    for _ in range(n):
        picks = [c[int(rng.integers(len(c)))] if c else None for c in cand]
        keep = [i for i, p in enumerate(picks) if p is not None]
        rl = real.loc[keep].copy()
        rl["b_id"] = [picks[i] for i in keep]
        rl = rl.drop_duplicates(["a_id", "b_id", "role"])
        ev, _ = be.build_events_for_links(panel, model, sh, rl, firms, pairs, "holdout")
        x = ev[col].to_numpy(dtype=float) if len(ev) else np.array([])
        vals.append(direction * float(np.nanmean(x)) if np.isfinite(x).any() else math.nan)
    return np.array(vals)


def verdict(expl: dict, hold: dict, power: dict | None = None) -> dict:
    by_role = {}
    for role in config.ROLES:
        under = bool(power and power["roles"][role]["underpowered"])
        if any(hold["H2"][f"{role}|{w}"]["pass"] for w in config.SLOW_WINDOWS):
            by_role[role] = "slow_ripple"
        elif hold["H1"][role]["pass"]:
            by_role[role] = "same_day_only"
        elif under:
            by_role[role] = "inconclusive_underpowered"
        else:
            by_role[role] = "no_ripple"
    vals = set(by_role.values())
    if "slow_ripple" in vals:
        overall = "slow_ripple"
    elif "same_day_only" in vals:
        overall = "same_day_only"
    elif "inconclusive_underpowered" in vals:
        overall = "no_ripple_powered_roles"
    else:
        overall = "no_ripple"
    return {"overall": overall, "by_role": by_role}


def analyze_holdout(panel, model, shocks, links, firms, pairs, events_h, expl: dict,
                    placebo_n: int = config.PLACEBO_N, boot_n: int = config.BOOT_N,
                    seed: int = config.SEED) -> dict:
    io.require_holdout_unlocked()
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
        floor_ok = bool(np.isfinite(c["mean"]) and d * c["mean"] >= config.FLOOR)
        placebo: dict = {"run": False}
        beats: bool | None = False
        if ex["pass_exploration"]:
            if placebo_n > 0:
                dist = placebo_distribution(panel, model, shocks, links, firms, pairs, r, w, d, placebo_n,
                                            seed + 1000 + idx)
                p95 = float(np.nanpercentile(dist, 95)) if np.isfinite(dist).any() else math.nan
                beats = bool(np.isfinite(p95) and d * c["mean"] > p95)
                placebo = {"run": True, "n": int(np.isfinite(dist).sum()), "p95": p95,
                           "p50": _f(np.nanmedian(dist)) if np.isfinite(dist).any() else math.nan}
            else:
                beats, placebo = None, {"run": False, "reason": "placebo skipped (robustness variant)"}
        passed = bool(ex["pass_exploration"] and replicates and floor_ok and (beats is None or beats))
        H2[key] = {"mean": c["mean"], "t": c["t"], "p_one": _f(p1), "direction": d,
                   "pass_exploration": ex["pass_exploration"], "replicates": replicates, "floor_ok": floor_ok,
                   "beats_placebo": beats, "placebo": placebo, "pass": passed}
    H3 = {}
    for role in config.ROLES:
        t = table[role]["pre"]["t"]
        ok_h = bool(not np.isfinite(t) or abs(t) < config.PRETREND_T)
        H3[role] = {"t_pre": t, "ok_holdout": ok_h, "ok_exploration": expl["H3"][role]["ok_exploration"],
                    "ok": bool(ok_h and expl["H3"][role]["ok_exploration"])}
    late = ev[ev["date"] >= pd.Timestamp(config.HOLDOUT_LATE_START)]
    res = {"period": "holdout", "table": table, "H1": H1, "H2": H2, "H3": H3,
           "robustness": {"r6_holdout_2024_on": role_window_table(late, config.K_PRIMARY, None, 0,
                                                                  config.SLOW_WINDOWS)},
           "counts": {"events": {r: int((ev["role"] == r).sum()) for r in config.ROLES},
                      "event_dates": int(ev["date"].nunique())}}
    res["verdict"] = verdict(expl, res, None)
    return res


def compute_paths(panel, model, events, controls, shocks, period: str, k: int = config.K_PRIMARY) -> pd.DataFrame:
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

    sh = shocks[shocks["period"] == period]
    if len(sh):
        cols = np.array([panel.col[s] for s in sh["symbol"]])
        add("all", "A", model.path(cols, sh["e_idx"].to_numpy(), lo, hi) * sh["s"].to_numpy()[:, None])
    ev = events[events["period"] == period]
    for role in config.ROLES:
        sub = ev[ev["role"] == role].reset_index(drop=True)
        if sub.empty:
            continue
        e, s = sub["e_idx"].to_numpy(), sub["s"].to_numpy(dtype=float)
        b = model.path(np.array([panel.col[x] for x in sub["b_symbol"]]), e, lo, hi) * s[:, None]
        ctl = controls[controls["event_id"].isin(set(sub["event_id"])) & (controls["rank"] <= k)]
        pos = {eid: i for i, eid in enumerate(sub["event_id"])}
        ci = ctl["event_id"].map(pos).to_numpy()
        cp = model.path(np.array([panel.col[x] for x in ctl["c_symbol"]]), e[ci], lo, hi) * s[ci][:, None]
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


def run(period: str = "exploration", variant: str = "market", rule: str = "sigma3",
        boot_n: int = config.BOOT_N, placebo_n: int = config.PLACEBO_N) -> dict:
    if period not in ("exploration", "holdout"):
        raise ValueError(f"unknown period {period!r}")
    if period == "holdout":
        io.require_holdout_unlocked()
    from research.dominoes import context
    sfx = config.variant_suffix(variant, rule)
    store = io.store_dir()
    events = io.read_frame(f"events_{period}{sfx}", be.event_schema())
    controls = io.read_frame(f"controls_{period}{sfx}", be.CONTROL_SCHEMA)
    shocks = io.read_frame(f"shocks{sfx}", bs.SHOCK_SCHEMA)
    panel, model = context.load(variant)
    if period == "exploration":
        res = analyze_exploration(events, shocks, boot_n)
    else:
        expl = json.loads((store / f"results_exploration{sfx}.json").read_text(encoding="utf-8"))
        power_path = store / f"power{sfx}.json"
        power = json.loads(power_path.read_text(encoding="utf-8")) if power_path.exists() else None
        res = analyze_holdout(panel, model, shocks, io.read_frame("links", snapshot_links.LINK_SCHEMA),
                              io.read_frame("firms", snapshot_links.FIRM_SCHEMA),
                              io.read_frame("any_edge_pairs", snapshot_links.PAIR_SCHEMA),
                              events, expl, placebo_n=placebo_n, boot_n=boot_n)
        res["verdict"] = verdict(expl, res, power)
    res.update({"variant": variant, "rule": rule})
    (store / f"results_{period}{sfx}.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    io.write_frame(f"paths_{period}{sfx}", compute_paths(panel, model, events, controls, shocks, period),
                   PATH_SCHEMA)
    return res
