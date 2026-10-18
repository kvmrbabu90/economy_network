"""Returns panel + abnormal-return models: data-error rule, rolling estimation on exactly [e-270, e-21]
with >=200 valid days, O(1) window CARs that equal direct sums, and the robustness variants."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.dominoes import returns as rt
from tests import dominoes_synth as synth


def test_clean_bad_prints_drops_reverting_spikes_and_keeps_real_jumps():
    r = np.array([[0.01], [0.80], [1 / 1.80 - 1], [0.00], [0.60], [0.01], [-0.60], [1.5], [0.0]])
    cleaned, n = rt.clean_bad_prints(r)
    assert n == 2
    assert np.isnan(cleaned[1, 0]) and np.isnan(cleaned[2, 0])          # +80% then back: bad print
    assert cleaned[4, 0] == pytest.approx(0.60)                            # +60% that holds: real
    assert np.isnan(cleaned[6, 0]) and np.isnan(cleaned[7, 0])           # -60% then back: bad print


def test_rolling_ols_matches_direct_fit_on_the_estimation_window():
    rng = np.random.default_rng(1)
    T = 700
    x = rng.normal(0, 0.01, T)
    y = 0.001 + 1.3 * x + rng.normal(0, 0.02, T)
    y[rng.choice(T, 40, replace=False)] = np.nan
    coef, sigma, n = rt.rolling_ols(y, x[:, None])
    for e in (270, 401, 699):
        a, b = e - 270, e - 21
        xs, ys = x[a:b + 1], y[a:b + 1]
        ok = np.isfinite(ys)
        slope, icpt = np.polyfit(xs[ok], ys[ok], 1)
        resid = ys[ok] - (icpt + slope * xs[ok])
        assert n[e] == ok.sum()
        assert coef[e, 0] == pytest.approx(icpt, abs=1e-10)
        assert coef[e, 1] == pytest.approx(slope, abs=1e-10)
        assert sigma[e] == pytest.approx(np.sqrt((resid ** 2).sum() / (ok.sum() - 2)), abs=1e-10)
    assert np.isnan(coef[269]).all() and np.isnan(sigma[269])


def test_rolling_ols_needs_200_valid_days():
    rng = np.random.default_rng(2)
    T = 400
    x = rng.normal(0, 0.01, T)
    y = 0.5 * x + rng.normal(0, 0.01, T)
    y[129:189] = np.nan                                  # window for e=399 is rows 129..378 -> n=190
    coef, sigma, n = rt.rolling_ols(y, x[:, None])
    assert n[399] == 190 and np.isnan(coef[399]).all() and np.isnan(sigma[399])


def test_rolling_ols_multifactor_matches_lstsq():
    rng = np.random.default_rng(3)
    T = 600
    X = rng.normal(0, 0.01, (T, 3))
    y = 0.0002 + X @ np.array([0.9, -0.4, 0.2]) + rng.normal(0, 0.01, T)
    coef, sigma, n = rt.rolling_ols(y, X)
    e = 500
    Z = np.column_stack([np.ones(250), X[e - 270:e - 20]])
    ref, *_ = np.linalg.lstsq(Z, y[e - 270:e - 20], rcond=None)
    assert np.allclose(coef[e], ref, atol=1e-10)


def _market_setup(drop_date_index=None, n_days=700):
    prices, _ = synth.synth_prices(n_days=n_days, symbols=("AAA",))
    if drop_date_index is not None:
        d = prices.loc[prices.symbol == "AAA", "date"].iloc[drop_date_index]
        prices = prices[~((prices.symbol == "AAA") & (prices.date == d))]
    panel = rt.build_panel(prices)
    return panel, rt.AbnormalModel(panel, "market")


def test_window_car_equals_direct_sum_of_daily_abnormal_returns():
    panel, model = _market_setup()
    i, s, e = panel.col["AAA"], panel.col["SPY"], 450
    a0, b0 = model.coef[i, e]
    direct = sum(panel.r[t, i] - (a0 + b0 * panel.r[t, s]) for t in range(e + 1, e + 21))
    assert model.car(np.array([i]), np.array([e]), 1, 20)[0] == pytest.approx(direct, abs=1e-12)
    pre = sum(panel.r[t, i] - (a0 + b0 * panel.r[t, s]) for t in range(e - 5, e))
    assert model.car(np.array([i]), np.array([e]), -5, -1)[0] == pytest.approx(pre, abs=1e-12)


def test_window_car_is_nan_when_a_day_is_missing_or_past_the_data():
    panel, model = _market_setup(drop_date_index=455)
    i = panel.col["AAA"]
    assert np.isnan(model.car(np.array([i]), np.array([450]), 1, 20)[0])     # missing day inside
    assert np.isfinite(model.car(np.array([i]), np.array([450]), 0, 0)[0])   # day 0 itself fine
    assert np.isnan(model.car(np.array([i]), np.array([690]), 1, 20)[0])     # runs past the data


def test_path_and_ar0_agree_with_the_day_by_day_definition():
    panel, model = _market_setup()
    i, s, e = panel.col["AAA"], panel.col["SPY"], 500
    a0, b0 = model.coef[i, e]
    path = model.path(np.array([i]), np.array([e]), -5, 60)
    assert path.shape == (1, 66)
    for d in (-5, 0, 7, 60):
        assert path[0, d + 5] == pytest.approx(panel.r[e + d, i] - (a0 + b0 * panel.r[e + d, s]), abs=1e-12)
    assert model.ar0(np.array([i]), np.array([e]))[0] == pytest.approx(path[0, 5], abs=1e-15)
    assert model.sigma[i, e] > 0 and model.nest[i, e] >= 200


def test_dollar_volume_and_abnormal_volume_use_minus80_to_minus21():
    panel, _ = _market_setup()
    i, e = panel.col["AAA"], 500
    win = slice(e - 80, e - 20)
    dv = rt.dollar_volume(panel, np.array([i]), np.array([e]))[0]
    assert dv == pytest.approx(np.mean(panel.close[win, i] * panel.volume[win, i]))
    av = rt.abnormal_volume(panel, np.array([i]), np.array([e]))[0]
    assert av == pytest.approx(panel.volume[e, i] / np.mean(panel.volume[win, i]))
    panel.volume[e - 80:e - 39, i] = np.nan               # 41 of the 60 window days blanked -> 19 valid
    assert np.isnan(rt.dollar_volume(panel, np.array([i]), np.array([e]))[0])


def test_sector_etf_variant_is_plain_excess_return():
    prices, _ = synth.synth_prices(n_days=700, symbols=("AAA", "XLK"), seed=4)
    panel = rt.build_panel(prices)
    model = rt.AbnormalModel(panel, "sector_etf", etf_of={"AAA": "XLK"})
    i, k, e = panel.col["AAA"], panel.col["XLK"], 480
    d = panel.r[:, i] - panel.r[:, k]
    assert model.ar0(np.array([i]), np.array([e]))[0] == pytest.approx(d[e], abs=1e-15)
    assert model.car(np.array([i]), np.array([e]), 1, 20)[0] == pytest.approx(d[e + 1:e + 21].sum(), abs=1e-12)
    assert model.sigma[i, e] == pytest.approx(np.std(d[e - 270:e - 20], ddof=1), abs=1e-12)


def test_ff6_variant_regresses_excess_returns_on_six_factors():
    rng = np.random.default_rng(5)
    prices, _ = synth.synth_prices(n_days=650, symbols=(), seed=5)
    dates = pd.bdate_range("2014-01-02", periods=650)
    f = rng.normal(0, 0.008, (650, 6))
    rf = np.full(650, 0.0001)
    y = rf + 0.0002 + f @ np.array([1.1, 0.3, -0.2, 0.1, 0.05, -0.15]) + rng.normal(0, 0.01, 650)
    close = 100 * np.cumprod(1 + y)
    prices = pd.concat([prices, pd.DataFrame({"symbol": "AAA", "date": dates, "close": close,
                                              "volume": 1e6})], ignore_index=True)
    factors = pd.DataFrame({"date": dates, "mkt_rf": f[:, 0], "smb": f[:, 1], "hml": f[:, 2],
                            "rmw": f[:, 3], "cma": f[:, 4], "rf": rf, "mom": f[:, 5]})
    panel = rt.build_panel(prices)
    model = rt.AbnormalModel(panel, "ff6", factors=factors)
    i, e = panel.col["AAA"], 600
    yy = panel.r[e - 270:e - 20, i] - rf[e - 270:e - 20]
    Z = np.column_stack([np.ones(250), f[e - 270:e - 20]])
    ref, *_ = np.linalg.lstsq(Z, yy, rcond=None)
    assert np.allclose(model.coef[i, e], ref, atol=1e-9)
