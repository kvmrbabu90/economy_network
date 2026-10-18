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
            for _ in range(5):
                base = true_mean + day_fx[g] + rng.normal(0, 0.04)
                rows.append({"role": role, "date": d, "ripple_w5_k5": base * 0.5,
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
