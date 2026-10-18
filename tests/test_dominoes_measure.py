"""Measurement end-to-end on synthetic worlds: a planted supplier ripple is recovered at the planted size and
nothing else passes; a null world passes nothing; a planted pre-trend trips H3; a planted holdout world
passes the full replication + floor + placebo chain; and the holdout stays locked without the marker."""
from __future__ import annotations

import json
import subprocess

import numpy as np
import pytest

from research.dominoes import build_events as be
from research.dominoes import build_shocks as bs
from research.dominoes import config
from research.dominoes import measure as ms
from research.dominoes import returns as rt
from tests import dominoes_synth as synth


def _pipeline(world, period="exploration"):
    prices, firms, links, pairs = world
    panel = rt.build_panel(prices)
    model = rt.AbnormalModel(panel, "market")
    shocks = bs.detect(panel, model, firms)
    ev, ctl = be.build_events_for_links(panel, model, shocks, links, firms, pairs, period)
    return panel, model, shocks, links, firms, pairs, ev, ctl


@pytest.fixture(scope="module")
def planted():
    p = _pipeline(synth.synth_world(seed=1, plant_w20=0.008))
    return p, ms.analyze_exploration(p[6], p[2], boot_n=200)


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
                                      "r8_llm_only", "r8_manual_only", "r9_raw_moves"}
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
    assert res["H3"]["supplier"]["ok_exploration"] is False


def test_holdout_chain_replication_floor_and_placebo(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    (tmp_path / config.HOLDOUT_MARKER).write_text(head, encoding="utf-8")
    panel, model, shocks, links, firms, pairs, ev_x, _ = _pipeline(
        synth.synth_world(seed=4, plant_w20=0.012, n_days=2800), "exploration")
    expl = ms.analyze_exploration(ev_x, shocks, boot_n=200)
    ev_h, _ = be.build_events_for_links(panel, model, shocks, links, firms, pairs, "holdout")
    hold = ms.analyze_holdout(panel, model, shocks, links, firms, pairs, ev_h, expl, placebo_n=20, boot_n=200)
    h = hold["H2"]["supplier|w20"]
    assert h["replicates"] and h["floor_ok"] and h["beats_placebo"] and h["pass"]
    assert not hold["H2"]["customer|w20"]["pass"]
    assert hold["verdict"]["overall"] == "slow_ripple"


def test_verdict_follows_spec_section_7():
    def ex(h1=False, h2=False):
        return {"H1": {r: {"pass_exploration": h1} for r in config.ROLES},
                "H2": {f"{r}|{w}": {"pass_exploration": h2} for r in config.ROLES for w in config.SLOW_WINDOWS}}

    def ho(h1=False, h2=False):
        return {"H1": {r: {"pass": h1} for r in config.ROLES},
                "H2": {f"{r}|{w}": {"pass": h2 and r == "supplier" and w == "w20"}
                       for r in config.ROLES for w in config.SLOW_WINDOWS}}

    ok = {"roles": {r: {"underpowered": False} for r in config.ROLES}}
    thin = {"roles": {"supplier": {"underpowered": True}, "customer": {"underpowered": False},
                      "competitor": {"underpowered": False}}}
    assert ms.verdict(ex(h2=True), ho(h2=True), ok)["overall"] == "slow_ripple"
    assert ms.verdict(ex(h1=True), ho(h1=True), ok)["overall"] == "same_day_only"
    assert ms.verdict(ex(), ho(), ok)["overall"] == "no_ripple"
    v = ms.verdict(ex(), ho(), thin)
    assert v["by_role"]["supplier"] == "inconclusive_underpowered" and v["overall"] == "no_ripple_powered_roles"


def test_holdout_measurement_requires_the_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    with pytest.raises(PermissionError):
        ms.run(period="holdout")
    with pytest.raises(PermissionError):
        ms.analyze_holdout(None, None, None, None, None, None, None, {}, placebo_n=1)
