"""Power check (spec §5): demean, inject a synthetic slow ripple of size delta spread over [+1, +20],
bootstrap event dates, count detections under the exploration rule, report the MDE at 80% power — and never
reveal the observed means."""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from research.dominoes import config
from research.dominoes import power_check as pc


def _events(seed=0, small_role=False):
    rng = np.random.default_rng(seed)
    rows = []
    dates = pd.bdate_range("2015-01-02", periods=300)
    for role, true_mean in (("supplier", 0.004), ("customer", -0.002), ("competitor", 0.001)):
        day_fx = rng.normal(0, 0.01, len(dates))
        for g, d in enumerate(dates):
            for j in range(5):
                base = true_mean + day_fx[g] + rng.normal(0, 0.04)
                rows.append({"role": role, "date": d, "b_id": f"cik:B{(g + j) % 40}", "ripple_w5_k5": base * 0.5,
                             "ripple_w20_k5": base, "ripple_w60_k5": base * 1.5})
    df = pd.DataFrame(rows)
    if small_role:   # a thin role: 20 events on 10 dates, noisy
        df = df[~((df.role == "customer") & (df.date > dates[9]))]
        df = df[~((df.role == "customer") & (df.groupby(["role", "date"]).cumcount() >= 2))]
    return df


def test_no_detection_at_zero_and_detection_rises_with_delta():
    res = pc.power(_events(), n_boot=600, seed=1)
    sup = res["roles"]["supplier"]["w20"]
    rates = [r for _, r in sup["curve"]]
    assert rates[0] <= 0.02                     # demeaned: the true +0.4% mean must NOT count as power
    assert all(b >= a - 0.01 for a, b in zip(rates, rates[1:]))
    assert rates[-1] >= 0.99


def test_bootstrap_mde_is_close_to_the_analytic_mde():
    res = pc.power(_events(), n_boot=800, seed=2)
    for role in ("supplier", "customer", "competitor"):
        w = res["roles"][role]["w20"]
        assert w["mde_bootstrap"] == pytest.approx(w["mde_analytic"], rel=0.25)
        assert w["t_threshold"] >= config.T_BAR


def test_thin_role_is_flagged_underpowered():
    res = pc.power(_events(small_role=True), n_boot=400, seed=3)
    assert res["roles"]["customer"]["underpowered"] is True
    assert res["roles"]["supplier"]["underpowered"] is False


def test_output_never_contains_an_observed_mean():
    ev = _events()
    res = pc.power(ev, n_boot=300, seed=4)
    observed = [ev.loc[ev.role == r, f"ripple_{w}_k5"].mean() for r in config.ROLES for w in config.SLOW_WINDOWS]
    floats = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                assert "mean" not in str(k).lower()
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
        elif isinstance(x, float):
            floats.append(x)

    walk(json.loads(json.dumps(res)))
    for m in observed:
        assert all(abs(f - m) > 1e-9 for f in floats)


def test_power_refuses_the_holdout(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    with pytest.raises(ValueError):
        pc.run(period="holdout")


# --- review fixes (Task 10) -------------------------------------------------------------------------------
def test_replicates_carry_the_small_sample_factor_on_both_mean_and_se():
    rng = np.random.default_rng(3)
    cl = np.repeat(np.arange(10), 3)
    v = rng.normal(0, 0.02, 30)
    m_c, se_c = pc._replicates(v, cl, 400, 5)
    m_u, se_u = pc._replicates(v, cl, 400, 5, small_sample=False)
    c = np.sqrt(10 / 9)
    assert np.allclose(m_c, c * m_u) and np.allclose(se_c, c * se_u)


def test_a_replicate_with_zero_se_never_counts_as_a_detection():
    # two dates: a draw that picks the same date twice has se = 0 (t = inf under the old code)
    w = pc._window_power(np.array([0.01, -0.01, 0.03, 0.05]), np.array(["d1", "d1", "d2", "d2"]),
                         np.array(["b1", "b2", "b1", "b2"]), 1.0, 500, 1)
    assert w["curve"][0][1] == 0.0


def test_ucb80_mde_is_never_below_the_bootstrap_mde_and_concentration_is_reported():
    res = pc.power(_events(), n_boot=300, seed=6)
    for role in config.ROLES:
        w = res["roles"][role]["w20"]
        assert w["mde_ucb80"] >= w["mde_bootstrap"]
        c = w["concentration"]
        assert c["top5_share_events"] == pytest.approx(25 / 1500) and c["g_eff_events"] == pytest.approx(300)
        assert 0 < c["top5_share_variance"] < 1 and 1 < c["g_eff_variance"] <= 300
        assert np.isfinite(w["mde_twoway"]) and w["mde_twoway"] > 0


def test_projected_holdout_power_uses_only_the_calendar():
    ev = _events()
    cal = pd.bdate_range("2013-09-02", "2026-10-16")
    res = pc.power(ev, n_boot=300, seed=7, calendar=cal)
    w = res["roles"]["supplier"]["w20"]
    proj = w["holdout_projection"]
    from scipy import stats as sps
    se_h = w["se"] * np.sqrt(proj["t_explore_days"] / proj["t_holdout_days"])
    g_h = w["g"] * proj["t_holdout_days"] / proj["t_explore_days"]
    want = (max(config.FLOOR, sps.t.ppf(0.95, g_h - 1) * se_h) + sps.norm.ppf(0.8) * se_h) / 1.0
    assert proj["mde_holdout_floor"] == pytest.approx(want, rel=1e-6)
    assert proj["mde_joint"] >= w["mde_bootstrap"] and 0 <= proj["joint_power_at_limit"] <= 1
    assert proj["t_explore_days"] < len(pd.bdate_range("2015-01-01", "2021-12-31"))   # embargoed days excluded


def test_borderline_is_descriptive_and_needs_an_explicit_reason():
    lim = config.MDE_LIMIT
    ok = {"mde_bootstrap": 0.010, "mde_ucb80": 0.012, "mde_twoway": 0.011, "holdout_projection": {"mde_joint": 0.011}}
    assert pc.borderline_reasons(ok, underpowered=False) == []
    ucb = dict(ok, mde_ucb80=0.016)
    assert pc.borderline_reasons(ucb, underpowered=False) == [f"80% upper-bound MDE 1.60% > {lim * 100:.1f}%"]
    joint = dict(ok, holdout_projection={"mde_joint": 0.017})
    assert "full-rule (exploration x holdout) MDE 1.70%" in pc.borderline_reasons(joint, underpowered=False)[0]
    assert pc.borderline_reasons(ucb, underpowered=True) == []          # underpowered already says it all


def test_power_file_records_its_method_inputs_and_an_events_fingerprint():
    ev = _events()
    res = pc.power(ev, n_boot=200, seed=8)
    meta = res["meta"]
    assert meta["method_version"] == config.POWER_METHOD_VERSION and meta["n_boot"] == 200 and meta["seed"] == 8
    assert meta["events_fingerprint"] == pc.events_fingerprint(ev)
    changed = ev.copy()
    changed.loc[changed.index[0], "ripple_w60_k5"] += 1e-9
    assert pc.events_fingerprint(changed) != meta["events_fingerprint"]


def test_power_stage_refuses_to_overwrite_once_exploration_results_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    (tmp_path / "results_exploration.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PermissionError, match="already measured"):
        pc.run()
