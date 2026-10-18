"""Readout, figures and CLI on a synthetic research store: the CLI runs every stage against the store, the
exploration readout carries the verdict table, every H1-H6 section and the integrity table, the CAR-path figure is
a real PNG, the holdout stays locked until `unlock` names a commit holding a matching readout, and the full gated
protocol (exploration -> commit readout -> unlock -> holdout) ends in a holdout readout with the §7 verdict."""
from __future__ import annotations

import json
import subprocess

import pytest

from research.dominoes import config, io, run
from research.dominoes import fetch_prices as fp
from research.dominoes import snapshot_links as sl
from tests import dominoes_synth as synth


def _seed_store(world) -> None:
    prices, firms, links, pairs = world
    io.write_frame("prices", prices, fp.PRICE_SCHEMA)
    io.write_frame("prices_raw", prices.rename(columns={"close": "close_raw"}), fp.RAW_SCHEMA)
    io.write_frame("firms", firms.assign(name=firms["ticker"]), sl.FIRM_SCHEMA)
    io.write_frame("links", links, sl.LINK_SCHEMA)
    io.write_frame("any_edge_pairs", pairs, sl.PAIR_SCHEMA)


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = text.find("\n## ", start + len(heading))
    return text[start:] if nxt < 0 else text[start:nxt]


def _fast(mp) -> None:
    mp.setattr(config, "POWER_BOOT_N", 100)
    mp.setattr(config, "BOOT_N", 100)
    mp.setattr(config, "PLACEBO_N", 20)


def _git(repo, *args) -> str:
    return subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture(scope="module")
def explored(tmp_path_factory):
    store = tmp_path_factory.mktemp("store")
    out = tmp_path_factory.mktemp("docs")
    mp = pytest.MonkeyPatch()
    mp.setenv("DOMINO_STORE", str(store))
    _fast(mp)
    io.clear_gate_cache()
    try:
        _seed_store(synth.synth_world(seed=1, plant_w20=0.008))
        run.main(["shocks"])
        run.main(["shocks", "--rule", "sigma4"])
        run.main(["events", "--period", "exploration"])
        run.main(["events", "--period", "exploration", "--rule", "sigma4"])
        run.main(["power"])
        run.main(["power", "--rule", "sigma4"])
        run.main(["measure", "--period", "exploration"])
        run.main(["measure", "--period", "exploration", "--rule", "sigma4"])
        run.main(["report", "--period", "exploration", "--out", str(out)])
    finally:
        mp.undo()
    return store, out


def test_cli_writes_every_stage_artifact(explored):
    store, out = explored
    for name in ("shocks.parquet", "shocks__market_sigma4.parquet", "events_exploration.parquet",
                 "controls_exploration.parquet", "power.json", "power__market_sigma4.json", "results_exploration.json",
                 "results_exploration__market_sigma4.json", "paths_exploration.parquet"):
        assert (store / name).exists(), name
    assert (out / "readout-exploration.md").exists()


def test_readout_has_verdict_table_every_hypothesis_section_and_integrity(explored):
    store, out = explored
    text = (out / "readout-exploration.md").read_text(encoding="utf-8")
    for heading in ("## Plain-language summary", "## Verdict", "## Power check", "## H1", "## H2", "## H3",
                    "## H4", "## H5", "## H6", "## Transmission coefficient", "## Robustness", "## Figures",
                    "## Integrity"):
        assert heading in text, heading
    verdict = _section(text, "## Verdict")
    assert all(f"| {r} |" in verdict for r in config.ROLES)
    h2 = _section(text, "## H2")
    sup = [ln for ln in h2.splitlines() if ln.startswith("| supplier | [+1,+20] |")]
    assert len(sup) == 1 and "PASS" in sup[0]
    assert not any("PASS" in ln for ln in h2.splitlines() if ln.startswith(("| customer |", "| competitor |")))
    assert "4σ" in _section(text, "## Robustness") and "R10" in _section(text, "## Robustness")
    assert "LOCKED" in text
    import hashlib
    integ = _section(text, "## Integrity")
    for name in ("results_exploration.json", "power.json"):
        assert hashlib.sha256((store / name).read_bytes()).hexdigest() in integ


def test_power_section_reports_mdes_not_means(explored):
    store, out = explored
    power = json.loads((store / "power.json").read_text(encoding="utf-8"))
    sec = _section((out / "readout-exploration.md").read_text(encoding="utf-8"), "## Power check")
    mde = power["roles"]["supplier"]["w20"]["mde_bootstrap"]
    assert f"{mde * 100:.2f}%" in sec and "Full-chain" in sec


def test_figure_is_a_png(explored):
    _, out = explored
    png = out / "figures" / "paths-exploration.png"
    assert png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "figures/paths-exploration.png" in (out / "readout-exploration.md").read_text(encoding="utf-8")


def test_holdout_stages_are_locked_without_the_marker(explored, monkeypatch):
    store, _ = explored
    monkeypatch.setenv("DOMINO_STORE", str(store))
    io.clear_gate_cache()
    for argv in (["events", "--period", "holdout"], ["measure", "--period", "holdout"],
                 ["report", "--period", "holdout"]):
        with pytest.raises(PermissionError):
            run.main(argv)


def test_power_cannot_be_regenerated_after_measuring(explored, monkeypatch):
    store, _ = explored
    monkeypatch.setenv("DOMINO_STORE", str(store))
    with pytest.raises(PermissionError, match="already measured"):
        run.main(["power"])


def test_every_markdown_table_row_has_as_many_cells_as_its_delimiter(explored):
    import re
    _, out = explored
    lines = (out / "readout-exploration.md").read_text(encoding="utf-8").splitlines()
    cells = lambda ln: len(re.split(r"(?<!\\)\|", ln.strip())) - 2      # noqa: E731
    for i, ln in enumerate(lines):
        if re.fullmatch(r"\|(---\|)+", ln.strip()):
            n = cells(ln)
            j = i - 1
            assert cells(lines[j]) == n, lines[j]
            j = i + 1
            while j < len(lines) and lines[j].startswith("|"):
                assert cells(lines[j]) == n, lines[j]
                j += 1


def test_overall_meaning_is_built_from_the_role_states():
    from research.dominoes import report
    v = {"overall": "same_day_some_inconclusive",
         "by_role": {"supplier": "same_day_slow_inconclusive", "customer": "no_ripple", "competitor": "no_ripple"}}
    text = report.overall_meaning(v)
    assert "supplier" in text and "same day" in text.lower()
    assert "customer, competitor" in text and "close the price-ripple thesis" in text.lower()
    assert "inconclusive" in text.lower()
    v2 = {"overall": "same_day_some_inconclusive",
          "by_role": {"supplier": "same_day_only", "customer": "no_ripple", "competitor": "inconclusive_underpowered"}}
    t2 = report.overall_meaning(v2)
    assert "customer" in t2 and "competitor" in t2 and "supplier" in t2


def test_holdout_summary_names_uncommitted_leads_without_claiming_them():
    from research.dominoes import report
    expl = {"H2": {f"{r}|{w}": {"pass_exploration": False} for r in config.ROLES for w in config.SLOW_WINDOWS}}
    table = {r: {w: {"mean": 0.0, "t": 0.1} for w in config.SLOW_WINDOWS} for r in config.ROLES}
    table["competitor"]["w60"] = {"mean": 0.0048, "t": 2.28}
    lines = report.uncommitted_leads(expl, {"table": table})
    assert len(lines) == 1 and "competitor [+1,+60]" in lines[0] and "cannot be claimed" in lines[0]
    assert "below the 0.5% floor" in lines[0]


def test_same_day_verdicts_carry_the_shared_news_caveat():
    from research.dominoes import report
    v = {"by_role": {"supplier": "same_day_only", "customer": "no_ripple", "competitor": "same_day_only"}}
    assert "sub-industry" in report.same_day_caveat(v)
    assert report.same_day_caveat({"by_role": {r: "no_ripple" for r in config.ROLES}}) == ""


def test_holdout_h3_text_names_the_period_that_flagged_and_marks_not_required():
    from research.dominoes import report
    h3 = {"t_pre": 0.30, "status": "flag", "status_exploration": "flag", "status_holdout": "ok", "applies": False}
    expl_t = 2.5
    s = report.h3_holdout_text(h3, expl_t)
    assert "exploration t 2.50" in s and "not required" in s


def test_full_gated_protocol_ends_in_a_holdout_verdict(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    monkeypatch.setattr(io, "REPO_ROOT", repo)
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path / "store"))
    _fast(monkeypatch)
    io.clear_gate_cache()
    out = repo / "docs" / "research" / "domino-study"
    _seed_store(synth.synth_world(seed=4, plant_w20=0.012, n_days=2800))
    run.main(["shocks"])
    run.main(["events", "--period", "exploration"])
    run.main(["power"])
    run.main(["measure", "--period", "exploration"])
    run.main(["report", "--period", "exploration", "--out", str(out)])
    with pytest.raises(PermissionError):
        run.main(["events", "--period", "holdout"])
    _git(repo, "add", "docs")
    _git(repo, "commit", "-q", "-m", "exploration readout")
    run.main(["unlock", "--commit", _git(repo, "rev-parse", "HEAD")])
    run.main(["events", "--period", "holdout"])
    run.main(["measure", "--period", "holdout"])
    run.main(["report", "--period", "holdout", "--out", str(out)])
    text = (out / "readout-holdout.md").read_text(encoding="utf-8")
    verdict = _section(text, "## Verdict")
    assert "slow ripple" in verdict.lower()
    chain = _section(text, "## H2")
    sup = [ln for ln in chain.splitlines() if ln.startswith("| supplier | [+1,+20] |")]
    assert len(sup) == 1 and "PASS" in sup[0]
    assert "Placebo diagnostics" in chain
    for ln in chain.splitlines():
        if ln.startswith(("| customer |", "| competitor |")):
            assert "not a candidate" in ln and "(raw)" in ln
    assert "two-way" in _section(text, "## H1")
    assert "H6-F1" in _section(text, "## H6")
    assert (out / "figures" / "paths-holdout.png").exists()
