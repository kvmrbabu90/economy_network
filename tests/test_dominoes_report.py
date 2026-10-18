"""Readout, figures and CLI on a synthetic research store: the CLI runs every stage against the store, the
exploration readout carries the verdict table and every H1-H6 section, the CAR-path figure is a real PNG,
the holdout stays locked until `unlock` names a commit holding the readout, and the full gated protocol
(exploration -> unlock -> holdout) ends in a holdout readout with the §7 verdict."""
from __future__ import annotations

import json
import subprocess

import pytest

from research.dominoes import config, io, run
from research.dominoes import fetch_prices as fp
from research.dominoes import snapshot_links as sl
from tests import dominoes_synth as synth

SPEC = "docs/superpowers/specs/2026-10-18-domino-study-design.md"


def _seed_store(world) -> None:
    prices, firms, links, pairs = world
    io.write_frame("prices", prices, fp.PRICE_SCHEMA)
    io.write_frame("firms", firms.assign(name=firms["ticker"]), sl.FIRM_SCHEMA)
    io.write_frame("links", links, sl.LINK_SCHEMA)
    io.write_frame("any_edge_pairs", pairs, sl.PAIR_SCHEMA)


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = text.find("\n## ", start + len(heading))
    return text[start:] if nxt < 0 else text[start:nxt]


@pytest.fixture(scope="module")
def explored(tmp_path_factory):
    store = tmp_path_factory.mktemp("store")
    out = tmp_path_factory.mktemp("docs")
    mp = pytest.MonkeyPatch()
    mp.setenv("DOMINO_STORE", str(store))
    _seed_store(synth.synth_world(seed=1, plant_w20=0.008))
    run.main(["shocks"])
    run.main(["shocks", "--rule", "sigma4"])
    run.main(["events", "--period", "exploration"])
    run.main(["events", "--period", "exploration", "--rule", "sigma4"])
    run.main(["power", "--boot-n", "100"])
    run.main(["measure", "--period", "exploration", "--boot-n", "100"])
    run.main(["measure", "--period", "exploration", "--rule", "sigma4", "--boot-n", "100"])
    run.main(["report", "--period", "exploration", "--out", str(out)])
    yield store, out
    mp.undo()


def test_cli_writes_every_stage_artifact(explored):
    store, out = explored
    for name in ("shocks.parquet", "shocks__market_sigma4.parquet", "events_exploration.parquet",
                 "controls_exploration.parquet", "power.json", "results_exploration.json",
                 "results_exploration__market_sigma4.json", "paths_exploration.parquet"):
        assert (store / name).exists(), name
    assert (out / "readout-exploration.md").exists()


def test_readout_has_verdict_table_and_every_hypothesis_section(explored):
    _, out = explored
    text = (out / "readout-exploration.md").read_text(encoding="utf-8")
    for heading in ("## Plain-language summary", "## Verdict", "## Power check", "## H1", "## H2", "## H3",
                    "## H4", "## H5", "## H6", "## Transmission coefficient", "## Robustness", "## Figures"):
        assert heading in text, heading
    verdict = _section(text, "## Verdict")
    assert all(f"| {r} |" in verdict for r in config.ROLES)
    h2 = _section(text, "## H2")
    sup = [ln for ln in h2.splitlines() if ln.startswith("| supplier | [+1,+20] |")]
    assert len(sup) == 1 and "PASS" in sup[0]
    assert not any("PASS" in ln for ln in h2.splitlines() if ln.startswith(("| customer |", "| competitor |")))
    assert "4σ" in _section(text, "## Robustness")
    assert "LOCKED" in text


def test_power_section_reports_mdes_not_means(explored):
    store, out = explored
    power = json.loads((store / "power.json").read_text(encoding="utf-8"))
    sec = _section((out / "readout-exploration.md").read_text(encoding="utf-8"), "## Power check")
    mde = power["roles"]["supplier"]["w20"]["mde_bootstrap"]
    assert f"{mde * 100:.2f}%" in sec


def test_figure_is_a_png(explored):
    _, out = explored
    png = out / "figures" / "paths-exploration.png"
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "figures/paths-exploration.png" in (out / "readout-exploration.md").read_text(encoding="utf-8")


def test_holdout_stages_are_locked_without_the_marker(explored):
    for argv in (["events", "--period", "holdout"], ["measure", "--period", "holdout"],
                 ["report", "--period", "holdout"]):
        with pytest.raises(PermissionError):
            run.main(argv)


def test_unlock_requires_a_commit_that_holds_the_readout(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    with pytest.raises(ValueError):
        run.main(["unlock", "--commit", "deadbeefdeadbeef"])
    with pytest.raises(ValueError):                       # HEAD does not hold a committed readout
        run.main(["unlock", "--commit", head])
    assert not io.holdout_unlocked()
    run.main(["unlock", "--commit", head, "--require-path", SPEC])
    assert io.holdout_unlocked()


def test_full_gated_protocol_ends_in_a_holdout_verdict(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path / "store"))
    out = tmp_path / "docs"
    _seed_store(synth.synth_world(seed=4, plant_w20=0.012, n_days=2800))
    run.main(["shocks"])
    run.main(["events", "--period", "exploration"])
    run.main(["power", "--boot-n", "100"])
    run.main(["measure", "--period", "exploration", "--boot-n", "100"])
    run.main(["report", "--period", "exploration", "--out", str(out)])
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    run.main(["unlock", "--commit", head, "--require-path", SPEC])
    run.main(["events", "--period", "holdout"])
    run.main(["measure", "--period", "holdout", "--boot-n", "100", "--placebo-n", "20"])
    run.main(["report", "--period", "holdout", "--out", str(out)])
    text = (out / "readout-holdout.md").read_text(encoding="utf-8")
    verdict = _section(text, "## Verdict")
    assert "slow ripple" in verdict.lower()
    chain = _section(text, "## H2")
    sup = [ln for ln in chain.splitlines() if ln.startswith("| supplier | [+1,+20] |")]
    assert len(sup) == 1 and "PASS" in sup[0]
    assert (out / "figures" / "paths-holdout.png").exists()
