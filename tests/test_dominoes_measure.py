"""Measurement end-to-end on synthetic worlds: a planted supplier ripple is recovered at the planted size and
nothing else passes; a null world passes nothing; a planted pre-trend trips H3; a planted holdout world passes
the full replication + floor + placebo chain; the holdout stays locked without the marker; and the Task-10 review
fixes hold (verdict composition, power binding, placebo design and evaluability, R8/R9/R10, H3 states, H6
terciles, CAR paths, and exploration outputs that never read a 2022+ return)."""
from __future__ import annotations

import itertools
import json
import math

import numpy as np
import pandas as pd
import pytest

from research.dominoes import build_events as be
from research.dominoes import build_shocks as bs
from research.dominoes import config, io
from research.dominoes import measure as ms
from research.dominoes import power_check as pc
from research.dominoes import returns as rt
from tests import dominoes_synth as synth


def _pipeline(world, period="exploration"):
    prices, firms, links, pairs = world
    panel = rt.build_panel(prices)
    model = rt.AbnormalModel(panel, "market")
    shocks = bs.detect(panel, model, firms)
    ev, ctl = be.build_events_for_links(panel, model, shocks, links, firms, pairs, period)
    return panel, model, shocks, links, firms, pairs, ev, ctl


def _power_ok(under=False, borderline=()):
    return {"roles": {r: {"underpowered": under if isinstance(under, bool) else r in under,
                          "borderline": r in borderline, "borderline_reasons": ["x"] if r in borderline else []}
                      for r in config.ROLES}}


@pytest.fixture
def unlocked(monkeypatch):
    """The real gate needs a committed readout whose hashes match the store (tested in test_dominoes_gate)."""
    monkeypatch.setattr(io, "holdout_unlocked", lambda: True)


@pytest.fixture(scope="module")
def planted():
    p = _pipeline(synth.synth_world(seed=1, plant_w20=0.008))
    return p, ms.analyze_exploration(p[6], p[2], boot_n=200)


@pytest.fixture(scope="module")
def holdout_world():
    mp = pytest.MonkeyPatch()
    mp.setattr(io, "holdout_unlocked", lambda: True)          # only while building; tests unlock themselves
    try:
        panel, model, shocks, links, firms, pairs, ev_x, _ = _pipeline(
            synth.synth_world(seed=4, plant_w20=0.012, n_days=2800), "exploration")
        expl = ms.analyze_exploration(ev_x, shocks, boot_n=200)
        ev_h, _ = be.build_events_for_links(panel, model, shocks, links, firms, pairs, "holdout")
    finally:
        mp.undo()
    return panel, model, shocks, links, firms, pairs, ev_h, expl


# --- exploration ------------------------------------------------------------------------------------------
def test_planted_supplier_ripple_is_recovered_and_nothing_else_passes(planted):
    _, res = planted
    h = res["H2"]["supplier|w20"]
    assert h["pass_exploration"] and 0.006 <= h["mean"] <= 0.010 and h["direction"] == 1
    assert not any(res["H2"][f"{r}|{w}"]["pass_exploration"]
                   for r in ("customer", "competitor") for w in config.SLOW_WINDOWS)
    assert not any(res["H1"][r]["pass_exploration"] for r in config.ROLES)   # nothing planted on day 0


def test_results_carry_every_section_and_serialize(planted):
    _, res = planted
    for key in ("table", "H1", "H2", "H3", "H5", "H6", "gamma", "robustness", "counts"):
        assert key in res
    assert set(res["robustness"]) >= {"r4_ex_covid", "r5_ex_industry_shocks", "r7_k3", "r7_k10",
                                      "r8_llm_only", "r8_manual_only", "r9_raw_moves", "r10_ex_b_coshock"}
    g = res["gamma"]["supplier"]["w20"]
    assert g["slope"] > 0 and g["t"] > 3
    json.dumps(res)


def test_null_world_passes_nothing():
    p = _pipeline(synth.synth_world(seed=2))
    res = ms.analyze_exploration(p[6], p[2], boot_n=200)
    assert not any(v["pass_exploration"] for v in res["H2"].values())
    assert not any(v["pass_exploration"] for v in res["H1"].values())


def test_planted_pre_trend_is_flagged_by_h3():
    p = _pipeline(synth.synth_world(seed=3, plant_w20=0.008, plant_pre=0.008))
    res = ms.analyze_exploration(p[6], p[2], boot_n=200)
    assert res["H2"]["supplier|w20"]["pass_exploration"]
    assert res["H3"]["supplier"]["status_exploration"] == "flag" and res["H3"]["supplier"]["applies"]


def test_untestable_pre_trend_is_never_ok():
    ev = pd.DataFrame({"role": ["supplier"] * 3, "date": pd.to_datetime(["2016-01-04"] * 3),
                       "ripple_pre_k5": [0.01, 0.02, np.nan]})
    assert ms.pretrend_status(ms._cell(ev, ev["ripple_pre_k5"].to_numpy(), None, 0)["t"]) == "untestable"
    assert ms.pretrend_status(1.5) == "ok" and ms.pretrend_status(-2.5) == "flag"


def test_two_way_diagnostic_is_reported_and_flags_a_weak_pass(planted):
    _, res = planted
    c = res["table"]["supplier"]["w20"]
    assert {"t_2w", "df_2w", "p_one_pos_2w"} <= set(c)
    h = res["H2"]["supplier|w20"]
    assert h["flag_2w"] == (not (abs(c["t_2w"]) > config.T_BAR))


def test_r9_raw_moves_add_up_to_the_ripple_on_the_same_sample(planted):
    _, res = planted
    r9 = res["robustness"]["r9_raw_moves"]
    for role in config.ROLES:
        for w in ("d0",) + config.SLOW_WINDOWS:
            b, c, rip = r9["linked_b"][role][w], r9["controls"][role][w], res["table"][role][w]
            assert b["n"] == c["n"] == rip["n"]
            if rip["n"]:
                assert b["mean"] - c["mean"] == pytest.approx(rip["mean"], abs=1e-12)


def test_r8_requires_known_extractor_families_and_reports_overlap():
    ev = pd.DataFrame({"extractors": ["llm:claude-cli", "manual:curation", "llm|manual:x", "llm:a"]})
    fam = ms.extractor_families(ev)
    assert list(fam["llm"]) == [True, False, True, True] and list(fam["manual"]) == [False, True, True, False]
    assert fam["overlap"] == 1
    with pytest.raises(ValueError, match="rule"):
        ms.extractor_families(pd.DataFrame({"extractors": ["llm:a", "rule"]}))


def test_h6_b_size_terciles_are_within_event_year_and_skip_thin_years():
    rng = np.random.default_rng(0)
    d = list(pd.to_datetime(["2016-03-01"] * 60)) + list(pd.to_datetime(["2019-03-01"] * 60)) \
        + list(pd.to_datetime(["2020-03-01"] * 10))
    dv = np.r_[rng.uniform(1, 2, 60), rng.uniform(100, 200, 60), rng.uniform(1, 2, 10)]
    t, excluded = ms.within_year_terciles(pd.Series(d), pd.Series(dv))
    assert excluded == 10 and t.iloc[120:].isna().all()
    for lo, hi in ((0, 60), (60, 120)):                     # each year split evenly despite the 100x level gap
        assert sorted(t.iloc[lo:hi].value_counts().tolist()) == [20, 20, 20]


def test_coshock_exclusion_r10_reports_the_share_of_events_whose_b_also_shocked(planted):
    p, res = planted
    r10 = res["robustness"]["r10_ex_b_coshock"]
    assert set(r10["share_coshock"]) == set(config.ROLES)
    assert all(0 <= r10["share_coshock"][r] <= 1 for r in config.ROLES)
    ev, shocks = p[6], p[2]
    flag = ms.b_coshock(ev, shocks)
    for r in config.ROLES:
        assert r10["share_coshock"][r] == pytest.approx(flag[(ev.role == r).to_numpy()].mean())
    b_shocks = shocks.set_index(["firm_id", "e_idx"]).index
    i = int(np.argmax(flag))                               # spot-check one flagged event by hand
    e, b = int(ev["e_idx"].iloc[i]), ev["b_id"].iloc[i]
    assert any((b, t) in b_shocks for t in range(e - config.COSHOCK_LOOKBACK, e + 1))


def test_exploration_outputs_never_read_returns_dated_2022_or_later():
    from tests.test_dominoes_events import _perturb_from
    prices, firms, links, pairs = synth.synth_world(seed=6, plant_w20=0.008)
    outs = []
    for p in (prices, _perturb_from(prices, "2022-01-03")):
        panel, model, shocks, _, _, _, ev, ctl = _pipeline((p, firms, links, pairs))
        res = ms.analyze_exploration(ev, shocks, boot_n=100,
                                     horizon_row=config.exploration_last_row(panel.dates))
        paths = ms.compute_paths(panel, model, ev, ctl, shocks, "exploration")
        outs.append((json.dumps(res, sort_keys=True), paths, pc.power(ev, n_boot=100)))
    assert outs[0][0] == outs[1][0]
    pd.testing.assert_frame_equal(outs[0][1], outs[1][1])
    assert json.dumps(outs[0][2], sort_keys=True) == json.dumps(outs[1][2], sort_keys=True)


def test_paths_use_only_events_with_a_complete_60_day_ripple(planted):
    p, _ = planted
    panel, model, shocks, _, _, _, ev, ctl = p
    paths = ms.compute_paths(panel, model, ev, ctl, shocks, "exploration")
    for role in config.ROLES:
        rip = paths[(paths.role == role) & (paths.series == "ripple") & (paths.day == 60)]
        assert int(rip["n"].iloc[0]) == int(ev.loc[ev.role == role, "ripple_w60_k5"].notna().sum())
    a = paths[(paths.role == "all") & (paths.series == "A") & (paths.day == 60)]
    assert int(a["n"].iloc[0]) == int(shocks.loc[shocks.period == "exploration", "cara_w60"].notna().sum())


# --- verdict ------------------------------------------------------------------------------------------------
def _ex(h1=(), h2=()):
    return {"H1": {r: {"pass_exploration": r in h1} for r in config.ROLES},
            "H2": {f"{r}|{w}": {"pass_exploration": r in h2 and w == "w20"} for r in config.ROLES
                   for w in config.SLOW_WINDOWS}}


def _ho(h1=(), h2=(), uneval=()):
    return {"H1": {r: {"pass": r in h1} for r in config.ROLES},
            "H2": {f"{r}|{w}": {"pass": r in h2 and w == "w20", "unevaluable": r in uneval and w == "w20"}
                   for r in config.ROLES for w in config.SLOW_WINDOWS}}


def test_verdict_follows_spec_section_7():
    assert ms.verdict(_ex(h2=["supplier"]), _ho(h2=["supplier"]), _power_ok())["overall"] == "slow_ripple"
    assert ms.verdict(_ex(h1=["supplier"]), _ho(h1=["supplier"]), _power_ok())["overall"] == "same_day_only"
    assert ms.verdict(_ex(), _ho(), _power_ok())["overall"] == "no_ripple"
    v = ms.verdict(_ex(), _ho(), _power_ok(under=["supplier"]))
    assert v["by_role"]["supplier"] == "inconclusive_underpowered" and v["overall"] == "no_ripple_powered_roles"
    v = ms.verdict(_ex(), _ho(), _power_ok(under=True))
    assert v["overall"] == "inconclusive"
    v = ms.verdict(_ex(h1=["supplier"]), _ho(h1=["supplier"]), _power_ok(under=["supplier"]))
    assert v["by_role"]["supplier"] == "same_day_slow_inconclusive" and v["overall"] == "same_day_some_inconclusive"
    v = ms.verdict(_ex(h2=["customer"]), _ho(uneval=["customer"]), _power_ok())
    assert v["by_role"]["customer"] == "inconclusive_unevaluable" and v["overall"] == "no_ripple_powered_roles"
    v = ms.verdict(_ex(h2=["supplier"]), _ho(h2=["supplier"]), _power_ok(under=["supplier"]))
    assert v["by_role"]["supplier"] == "slow_ripple"                          # a pass outranks low power


def test_verdict_invariants_over_every_combination():
    roles = config.ROLES
    for h2, h1, under in itertools.product(itertools.product([0, 1], repeat=3), repeat=3):
        h2r = [r for r, x in zip(roles, h2) if x]
        h1r = [r for r, x in zip(roles, h1) if x]
        ur = [r for r, x in zip(roles, under) if x]
        v = ms.verdict(_ex(h1=h1r, h2=h2r), _ho(h1=h1r, h2=h2r), _power_ok(under=ur))
        assert (v["overall"] == "slow_ripple") == bool(h2r)
        if v["overall"] in ("no_ripple", "same_day_only"):
            assert not ur
        if v["overall"] == "inconclusive":
            assert all(s.startswith("inconclusive") for s in v["by_role"].values())
        if v["overall"] == "no_ripple_powered_roles":
            assert ur and not h1r and len(ur) < 3
        for r in ur:
            assert v["by_role"][r] in ("slow_ripple", "same_day_slow_inconclusive", "inconclusive_underpowered")


def test_verdict_refuses_missing_or_malformed_power():
    with pytest.raises(ValueError):
        ms.verdict(_ex(), _ho(), None)
    with pytest.raises(ValueError):
        ms.verdict(_ex(), _ho(), {"roles": {"supplier": {"underpowered": False}}})


# --- holdout --------------------------------------------------------------------------------------------------
def test_holdout_chain_replication_floor_and_placebo(holdout_world, monkeypatch):
    panel, model, shocks, links, firms, pairs, ev_h, expl = holdout_world
    monkeypatch.setattr(io, "holdout_unlocked", lambda: True)
    monkeypatch.setattr(config, "PLACEBO_N", 20)
    hold = ms.analyze_holdout(panel, model, shocks, links, firms, pairs, ev_h, expl, power=_power_ok(),
                              boot_n=200)
    h = hold["H2"]["supplier|w20"]
    assert h["replicates"] and h["floor_ok"] and h["beats_placebo"] and h["pass"]
    assert h["placebo"]["evaluable"] and h["placebo"]["n_finite"] == 20
    assert not hold["H2"]["customer|w20"]["pass"]
    assert hold["verdict"]["overall"] == "slow_ripple"
    assert {"H5", "robustness"} <= set(hold) and "r10_ex_b_coshock" in hold["robustness"]
    assert np.isfinite(hold["H5"]["w20"]["mean"])


def test_primary_holdout_refuses_any_placebo_count_but_the_registered_one(holdout_world, monkeypatch):
    panel, model, shocks, links, firms, pairs, ev_h, expl = holdout_world
    monkeypatch.setattr(io, "holdout_unlocked", lambda: True)
    with pytest.raises(ValueError, match="placebo"):
        ms.analyze_holdout(panel, model, shocks, links, firms, pairs, ev_h, expl, power=_power_ok(),
                           placebo_n=20, boot_n=50)


def test_placebo_relabels_each_b_once_within_sector_and_never_onto_a_real_link(holdout_world, monkeypatch):
    panel, model, shocks, links, firms, pairs, ev_h, _ = holdout_world
    monkeypatch.setattr(io, "holdout_unlocked", lambda: True)
    setup = ms.placebo_setup(panel, model, shocks, links, firms, ev_h, "competitor", "w20")
    study = {frozenset(p) for p in links[["a_id", "b_id"]].itertuples(index=False)}
    sec = firms.set_index("firm_id")["sector"].to_dict()
    rng = np.random.default_rng(3)
    for _ in range(5):
        rl, info = ms.placebo_links(setup, rng)
        assert rl.groupby("real_b")["b_id"].nunique().max() == 1                # one B' per real B
        assert not any(frozenset((a, b)) in study for a, b in zip(rl.a_id, rl.b_id))
        assert all(sec[b] == sec[rb] for b, rb in zip(rl.b_id, rl.real_b))
        assert not (rl.b_id == rl.a_id).any() and not (rl.b_id == rl.real_b).any()
        assert 0 <= info["dropped_link_share"] <= 1


def test_placebo_is_deterministic_for_a_seed(holdout_world, monkeypatch):
    panel, model, shocks, links, firms, pairs, ev_h, _ = holdout_world
    monkeypatch.setattr(io, "holdout_unlocked", lambda: True)
    a = ms.placebo_distribution(panel, model, shocks, links, firms, pairs, ev_h, "supplier", "w20", 1, 4, 11)
    b = ms.placebo_distribution(panel, model, shocks, links, firms, pairs, ev_h, "supplier", "w20", 1, 4, 11)
    assert np.array_equal(a["values"], b["values"], equal_nan=True)


def test_placebo_summary_requires_finite_runs_and_coverage():
    ok = ms.placebo_summary(np.r_[np.full(19, 0.001), 0.002], np.full(20, 80), real_count=100, real_se=0.001,
                            n=20)
    assert ok["evaluable"]
    few = ms.placebo_summary(np.r_[np.full(17, 0.001), [np.nan] * 3], np.full(20, 80), real_count=100,
                             real_se=0.001, n=20)
    assert not few["evaluable"] and "17 of 20" in few["reason"]
    thin = ms.placebo_summary(np.full(20, 0.001), np.full(20, 30), real_count=100, real_se=0.001, n=20)
    assert not thin["evaluable"] and "coverage" in thin["reason"]


def test_unevaluable_placebo_makes_the_role_inconclusive_not_no_ripple(holdout_world, monkeypatch):
    panel, model, shocks, links, firms, pairs, ev_h, expl = holdout_world
    monkeypatch.setattr(io, "holdout_unlocked", lambda: True)
    monkeypatch.setattr(config, "PLACEBO_N", 20)
    monkeypatch.setattr(ms, "placebo_distribution",
                        lambda *a, **k: {"values": np.full(20, np.nan), "coverage": np.zeros(20),
                                         "pool_median": 0.0, "pool_min": 0, "dropped_link_share": 1.0,
                                         "dropped_b": 0, "real_count": 10})
    hold = ms.analyze_holdout(panel, model, shocks, links, firms, pairs, ev_h, expl, power=_power_ok(),
                              boot_n=50)
    h = hold["H2"]["supplier|w20"]
    assert h["beats_placebo"] is False and h["unevaluable"] and not h["pass"]
    assert hold["verdict"]["by_role"]["supplier"] == "inconclusive_unevaluable"


def test_holdout_r10_counts_b_shocks_from_late_2021(holdout_world, monkeypatch):
    panel, model, shocks, links, firms, pairs, ev_h, expl = holdout_world
    early = ev_h[ev_h["date"] < pd.Timestamp("2022-01-15")]
    assert len(early)
    flags_all = ms.b_coshock(early, shocks)
    flags_hold = ms.b_coshock(early, shocks[shocks["period"] == "holdout"])
    assert flags_all.sum() >= flags_hold.sum()
    monkeypatch.setattr(io, "holdout_unlocked", lambda: True)
    monkeypatch.setattr(ms, "placebo_distribution",
                        lambda *a, **k: {"values": np.full(config.PLACEBO_N, 0.0), "coverage": np.full(config.PLACEBO_N, 1e9),
                                         "pool_median": 5.0, "pool_min": 1, "dropped_link_share": 0.0,
                                         "dropped_b": 0, "real_count": 1})
    hold = ms.analyze_holdout(panel, model, shocks, links, firms, pairs, ev_h, expl, power=_power_ok(), boot_n=50)
    ev = ev_h[ev_h["period"] == "holdout"]
    want = ms.b_coshock(ev, shocks)
    got = hold["robustness"]["r10_ex_b_coshock"]["share_coshock"]["supplier"]
    assert got == pytest.approx(want[(ev.role == "supplier").to_numpy()].mean())


def test_holdout_measurement_requires_the_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    with pytest.raises(PermissionError):
        ms.run(period="holdout")
    with pytest.raises(PermissionError):
        ms.analyze_holdout(None, None, None, None, None, None, None, {}, power=_power_ok())


# --- run(): power binding ---------------------------------------------------------------------------------
@pytest.fixture
def small_store(tmp_path, monkeypatch):
    from research.dominoes import fetch_prices as fp
    from research.dominoes import snapshot_links as sl
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    monkeypatch.setattr(config, "POWER_BOOT_N", 60)
    monkeypatch.setattr(config, "BOOT_N", 60)
    prices, firms, links, pairs = synth.synth_world(seed=8, n_firms=40, n_days=2200)
    io.write_frame("prices", prices, fp.PRICE_SCHEMA)
    io.write_frame("prices_raw", prices.rename(columns={"close": "close_raw"}), fp.RAW_SCHEMA)
    io.write_frame("firms", firms.assign(name=firms["ticker"]), sl.FIRM_SCHEMA)
    io.write_frame("links", links, sl.LINK_SCHEMA)
    io.write_frame("any_edge_pairs", pairs, sl.PAIR_SCHEMA)
    from research.dominoes import context
    panel, model = context.load("market")
    io.write_frame("shocks", bs.detect(panel, model, firms), bs.SHOCK_SCHEMA)
    be.build("exploration", panel=panel, model=model)
    return tmp_path, panel, model


def test_measure_requires_a_fresh_pinned_power_file(small_store, monkeypatch):
    store, panel, model = small_store
    with pytest.raises(FileNotFoundError, match="power"):
        ms.run("exploration", panel=panel, model=model)
    pc.run()
    power = json.loads((store / "power.json").read_text(encoding="utf-8"))
    bad = dict(power, meta=dict(power["meta"], n_boot=7))
    (store / "power.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="n_boot"):
        ms.run("exploration", panel=panel, model=model)
    stale = dict(power, meta=dict(power["meta"], events_fingerprint="0" * 64))
    (store / "power.json").write_text(json.dumps(stale), encoding="utf-8")
    with pytest.raises(ValueError, match="stale"):
        ms.run("exploration", panel=panel, model=model)
    (store / "power.json").write_text(json.dumps(power), encoding="utf-8")
    res = ms.run("exploration", panel=panel, model=model)
    import hashlib
    assert res["power_sha256"] == hashlib.sha256((store / "power.json").read_bytes()).hexdigest()
    assert res["counts"]["embargoed"]["supplier"]["w60"] > 0


def test_robustness_variants_carry_no_verdict(small_store, monkeypatch):
    store, panel, model = small_store
    io.write_frame("shocks__market_sigma4", bs.detect(panel, model,
                   io.read_frame("firms", __import__("research.dominoes.snapshot_links",
                                                     fromlist=["FIRM_SCHEMA"]).FIRM_SCHEMA), rule="sigma4"),
                   bs.SHOCK_SCHEMA)
    be.build("exploration", rule="sigma4", panel=panel, model=model)
    pc.run(rule="sigma4")
    res = ms.run("exploration", rule="sigma4", panel=panel, model=model)
    assert res.get("verdict") is None and math.isfinite(res["counts"]["event_dates"])
