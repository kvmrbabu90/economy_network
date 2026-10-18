"""Shock detection (the first domino): |AR| >= 3 sigma with the event's own estimation window, the §13.2
dedup (any earlier shock within 10 trading days), period labels, signed first-domino CARs, and the
robustness-3 rules (4 sigma; each firm's top 1%)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.dominoes import build_shocks as bs
from research.dominoes import returns as rt
from tests import dominoes_synth as synth

H = 0.01                       # bounded uniform noise: no natural 3-sigma days
SIG = H / np.sqrt(3.0)         # its standard deviation
START = "2013-06-03"           # index ~413 is the first 2015 day; params exist from index 270
N_DAYS = 1000


def _setup(plants: dict[int, float]):
    rng = np.random.default_rng(11)
    noise = rng.uniform(-H, H, N_DAYS)
    for t, v in plants.items():
        noise[t] = v
    prices, _ = synth.synth_prices(n_days=N_DAYS, symbols=("AAA", "XLK"), seed=11, start=START,
                                   idio_override={"AAA": noise})
    panel = rt.build_panel(prices)
    model = rt.AbnormalModel(panel, "market")
    firms = pd.DataFrame({"firm_id": ["cik:A"], "ticker": ["AAA"], "symbol": ["AAA"],
                          "sector": ["Information Technology"]})
    return panel, model, firms


def _at(shocks: pd.DataFrame, e: int):
    rows = shocks[shocks.e_idx == e]
    return None if rows.empty else rows.iloc[0]


def test_planted_5_sigma_shock_is_detected_with_its_sign():
    panel, model, firms = _setup({800: -5 * SIG})
    shocks = bs.detect(panel, model, firms)
    row = _at(shocks, 800)
    assert row is not None and row["s"] == -1 and row["period"] == "exploration"
    assert -5.8 < row["z"] < -4.2
    assert set(bs.SHOCK_SCHEMA) <= set(shocks.columns)


def test_a_2_sigma_move_is_not_a_shock():
    panel, model, firms = _setup({700: 2 * SIG})
    assert _at(bs.detect(panel, model, firms), 700) is None


def test_dedup_drops_any_shock_within_10_days_of_an_earlier_one():
    panel, model, firms = _setup({500: 8 * SIG, 508: 8 * SIG, 516: 8 * SIG, 530: 8 * SIG})
    kept = set(bs.detect(panel, model, firms)["e_idx"])
    assert {500, 530} <= kept and not ({508, 516} & kept)   # 516 is within 10 of the dropped 508


def test_shocks_before_2015_are_not_events():
    panel, model, firms = _setup({300: 8 * SIG})
    assert panel.dates[300] < pd.Timestamp("2015-01-01")
    assert _at(bs.detect(panel, model, firms), 300) is None


def test_first_domino_cars_are_signed_by_the_shock():
    panel, model, firms = _setup({800: -5 * SIG})
    row = _at(bs.detect(panel, model, firms), 800)
    i = panel.col["AAA"]
    for w, (a, b) in (("cara_w5", (1, 5)), ("cara_w20", (1, 20)), ("cara_w60", (1, 60))):
        assert row[w] == pytest.approx(-1 * model.car(np.array([i]), np.array([800]), a, b)[0], abs=1e-12)


def test_abnormal_volume_and_sector_etf_z_are_recorded():
    panel, model, firms = _setup({800: -5 * SIG})
    row = _at(bs.detect(panel, model, firms), 800)
    i, k = panel.col["AAA"], panel.col["XLK"]
    assert row["abn_vol"] == pytest.approx(rt.abnormal_volume(panel, np.array([i]), np.array([800]))[0])
    etf_z = model.ar0(np.array([k]), np.array([800]))[0] / model.sigma[k, 800]
    assert row["sector_etf_z"] == pytest.approx(etf_z)


def test_sigma4_rule_raises_the_threshold():
    panel, model, firms = _setup({600: 3.5 * SIG})
    assert _at(bs.detect(panel, model, firms, rule="sigma3"), 600) is not None
    assert _at(bs.detect(panel, model, firms, rule="sigma4"), 600) is None


def test_top1pct_rule_uses_each_firms_own_99th_percentile():
    panel, model, firms = _setup({500: 8 * SIG, 650: 6 * SIG, 900: -7 * SIG})
    i = panel.col["AAA"]
    ar = model.path(np.full(panel.r.shape[0], i), np.arange(panel.r.shape[0]), 0, 0)[:, 0]
    study = np.asarray(panel.dates >= pd.Timestamp("2015-01-01")) & np.isfinite(ar)
    thr = np.percentile(np.abs(ar[study]), 99)
    got = set(bs.detect(panel, model, firms, rule="top1pct")["e_idx"])
    expected = {t for t in np.nonzero(study & (np.abs(ar) >= thr))[0]}
    assert {500, 650, 900} <= got and got <= expected
