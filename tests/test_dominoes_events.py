"""Events + matched controls (spec §4, §13.4-§13.6): role triples per shock, controls from B's sector that are
never A, never B and never linked to A by ANY edge, ranked by nearest log dollar volume (ties by ticker),
signed outcomes per window and K, window-completeness, and the holdout gate."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.dominoes import build_events as be
from research.dominoes import build_shocks as bs
from research.dominoes import returns as rt
from tests import dominoes_synth as synth

H, N, E = 0.01, 1000, 600
SIG = H / np.sqrt(3.0)
FIN = ("BBB", "CC1", "CC2", "CC3", "CC4", "CC5", "CC6", "DD1")
IT = ("AAA", "FF1", "GG1", "GG2")


def _world(drop_ctl_days=False, without=()):
    rng = np.random.default_rng(21)
    noise_a = rng.uniform(-H, H, N)
    noise_a[E] = 6 * SIG                                         # A's shock on day E
    syms = tuple(s for s in ("AAA", "BBB", "CC1", "CC2", "CC3", "CC4", "CC5", "CC6", "FF1", "GG1", "GG2")
                 if s not in without)
    prices, _ = synth.synth_prices(n_days=N, symbols=syms, seed=21, start="2013-06-03",
                                   idio_override={"AAA": noise_a})
    clone = prices[prices.symbol == "BBB"].copy()
    clone["symbol"] = "DD1"                                       # identical DV to B: would rank first
    prices = pd.concat([prices, clone], ignore_index=True)
    if drop_ctl_days:                                             # CC* lose days E+10..E+12 (inside w20 only)
        days = sorted(prices.date.unique())[E + 10:E + 13]
        prices = prices[~(prices.symbol.str.startswith("CC") & prices.date.isin(days))]
    panel = rt.build_panel(prices)
    model = rt.AbnormalModel(panel, "market")
    present = [s for s in FIN + IT if s in panel.col]
    firms = pd.DataFrame({"firm_id": [f"cik:{s}" for s in present], "ticker": present, "symbol": present,
                          "sector": ["Financials" if s in FIN else "Information Technology" for s in present]})
    links = pd.DataFrame({
        "a_id": ["cik:AAA", "cik:BBB", "cik:AAA", "cik:FF1"],
        "b_id": ["cik:BBB", "cik:AAA", "cik:FF1", "cik:AAA"],
        "role": ["customer", "supplier", "competitor", "competitor"],
        "extractors": ["llm:claude-cli"] * 4, "weight": [0.3, 0.3, np.nan, np.nan]})
    pairs = pd.DataFrame({"x_id": ["cik:AAA", "cik:AAA", "cik:AAA"], "y_id": ["cik:BBB", "cik:DD1", "cik:FF1"]})
    shocks = bs.detect(panel, model, firms)
    shocks = shocks[(shocks.firm_id == "cik:AAA") & (shocks.e_idx == E)]
    assert len(shocks) == 1
    return panel, model, firms, links, pairs, shocks


def _event(ev, role):
    return ev[ev.role == role].iloc[0]


def test_rank_controls_nearest_log_dollar_volume_with_ticker_ties():
    order = be.rank_controls(100.0, ["z", "a", "m"], np.array([200.0, 200.0, 150.0]), ["ZZZ", "AAB", "MMM"], k=10)
    assert order == ["m", "a", "z"]
    assert be.rank_controls(100.0, ["z", "a", "m"], np.array([200.0, 200.0, 150.0]), ["ZZZ", "AAB", "MMM"], k=2) == ["m", "a"]


def test_one_event_per_link_of_the_shocked_firm_with_its_role():
    panel, model, firms, links, pairs, shocks = _world()
    ev, _ = be.build_events_for_links(panel, model, shocks, links, firms, pairs, "exploration")
    assert {(r.b_id, r.role) for r in ev.itertuples()} == {("cik:BBB", "customer"), ("cik:FF1", "competitor")}
    row = _event(ev, "customer")
    assert row["s"] == 1 and row["weight"] == pytest.approx(0.3) and row["period"] == "exploration"
    assert row["week"] == panel.dates[E].strftime("%G-W%V")


def test_controls_are_same_sector_never_a_b_or_linked_to_a_and_ranked_by_dollar_volume():
    panel, model, firms, links, pairs, shocks = _world()
    ev, ctl = be.build_events_for_links(panel, model, shocks, links, firms, pairs, "exploration")
    cust = _event(ev, "customer")
    got = ctl[ctl.event_id == cust["event_id"]].sort_values("rank")
    assert set(got.c_id) == {f"cik:CC{k}" for k in range(1, 7)}   # never AAA, BBB or the linked clone DD1
    dv = {s: rt.dollar_volume(panel, np.array([panel.col[s]]), np.array([E]))[0] for s in FIN}
    expected = sorted((f"CC{k}" for k in range(1, 7)), key=lambda s: (abs(np.log(dv[s]) - np.log(dv["BBB"])), s))
    assert list(got.c_id) == [f"cik:{s}" for s in expected]
    comp = _event(ev, "competitor")
    assert set(ctl[ctl.event_id == comp["event_id"]].c_id) == {"cik:GG1", "cik:GG2"}


def test_signed_ripple_raw_moves_and_control_counts_per_k():
    panel, model, firms, links, pairs, shocks = _world()
    ev, ctl = be.build_events_for_links(panel, model, shocks, links, firms, pairs, "exploration")
    cust = _event(ev, "customer")
    ranked = list(ctl[ctl.event_id == cust["event_id"]].sort_values("rank").c_id.str.slice(4))
    b = panel.col["BBB"]
    car_b = model.car(np.array([b]), np.array([E]), 1, 20)[0]
    cars = model.car(np.array([panel.col[s] for s in ranked]), np.full(len(ranked), E), 1, 20)
    for k, n_used in ((3, 3), (5, 5), (10, 6)):
        mean_c = cars[:k].mean()
        assert cust[f"nctl_w20_k{k}"] == n_used
        assert cust[f"ripple_w20_k{k}"] == pytest.approx(car_b - mean_c, abs=1e-12)     # s = +1
        assert cust[f"raw_c_w20_k{k}"] == pytest.approx(mean_c, abs=1e-12)
    assert cust["raw_b_w20"] == pytest.approx(car_b, abs=1e-12)
    pre_b = model.car(np.array([b]), np.array([E]), -5, -1)[0]
    assert cust["raw_b_pre"] == pytest.approx(pre_b, abs=1e-12)


def test_window_with_too_few_complete_controls_is_dropped_for_that_window_only():
    panel, model, firms, links, pairs, shocks = _world(drop_ctl_days=True)
    ev, _ = be.build_events_for_links(panel, model, shocks, links, firms, pairs, "exploration")
    cust = _event(ev, "customer")
    assert cust["nctl_w20_k5"] == 0 and np.isnan(cust["ripple_w20_k5"])
    assert cust["nctl_w5_k5"] == 5 and np.isfinite(cust["ripple_w5_k5"])


def test_event_with_fewer_than_two_eligible_controls_is_excluded():
    panel, model, firms, links, pairs, shocks = _world(without=("GG2",))
    ev, _ = be.build_events_for_links(panel, model, shocks, links, firms, pairs, "exploration")
    assert set(ev.role) == {"customer"}                         # competitor had only GG1 left


def test_holdout_events_require_the_unlock_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    with pytest.raises(PermissionError):
        be.build("holdout")
