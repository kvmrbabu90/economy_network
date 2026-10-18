"""The holdout gate (spec §12, §13.11 + Task-10 fix): the holdout opens only when the marker names a commit that
exists, is an ancestor of HEAD, and contains the exploration readout whose integrity table (sha256 of the
exploration results and power files) matches the research store. Exercised in a throwaway git repository."""
from __future__ import annotations

import hashlib
import subprocess

import pytest

from research.dominoes import config, io, report, run


def _git(repo, *args) -> str:
    r = subprocess.run(["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args], cwd=repo,
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    rp = tmp_path / "repo"
    rp.mkdir()
    _git(rp, "init", "-q", "-b", "main")
    (rp / "README.md").write_text("x\n", encoding="utf-8")
    _git(rp, "add", "README.md")
    _git(rp, "commit", "-q", "-m", "init")
    store = tmp_path / "store"
    store.mkdir()
    (store / "results_exploration.json").write_text('{"H2": {}}', encoding="utf-8")
    (store / "power.json").write_text('{"roles": {}}', encoding="utf-8")
    monkeypatch.setattr(io, "REPO_ROOT", rp)
    monkeypatch.setenv("DOMINO_STORE", str(store))
    io.clear_gate_cache()
    return rp, store


def _commit_readout(rp, text: str) -> str:
    path = rp / config.READOUT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(rp, "add", config.READOUT_PATH)
    _git(rp, "commit", "-q", "-m", "readout")
    return _git(rp, "rev-parse", "HEAD")


def _readout(store) -> str:
    return "# readout\n\n" + "\n".join(report.integrity_lines(store)) + "\n"


def test_integrity_lines_hash_every_results_and_power_file(repo):
    _, store = repo
    (store / "power__market_sigma4.json").write_text("{}", encoding="utf-8")
    lines = "\n".join(report.integrity_lines(store))
    for name in ("results_exploration.json", "power.json", "power__market_sigma4.json"):
        assert f"`{name}` | {hashlib.sha256((store / name).read_bytes()).hexdigest()} |" in lines


def test_unlock_opens_the_holdout_only_for_a_committed_matching_readout(repo):
    rp, store = repo
    sha = _commit_readout(rp, _readout(store))
    run.main(["unlock", "--commit", sha])
    assert io.holdout_unlocked()
    (store / "results_exploration.json").write_text('{"H2": {"changed": 1}}', encoding="utf-8")
    io.clear_gate_cache()
    ok, reason = io.gate_status()
    assert not ok and "results_exploration.json" in reason


def test_a_stub_readout_without_the_integrity_table_never_unlocks(repo):
    rp, store = repo
    sha = _commit_readout(rp, "# readout\nnothing here\n")
    with pytest.raises(ValueError, match="integrity"):
        run.main(["unlock", "--commit", sha])
    assert not (store / config.HOLDOUT_MARKER).exists() and not io.holdout_unlocked()


def test_unlock_refuses_unknown_commits_and_commits_without_the_readout(repo):
    rp, store = repo
    with pytest.raises(ValueError):
        run.main(["unlock", "--commit", "deadbeefdeadbeef"])
    head = _git(rp, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="readout"):
        run.main(["unlock", "--commit", head])
    assert not io.holdout_unlocked()


def test_the_readout_commit_must_be_an_ancestor_of_head(repo):
    rp, store = repo
    _git(rp, "checkout", "-q", "-b", "side")
    sha = _commit_readout(rp, _readout(store))
    _git(rp, "checkout", "-q", "main")
    (store / config.HOLDOUT_MARKER).write_text(sha, encoding="utf-8")
    io.clear_gate_cache()
    ok, reason = io.gate_status()
    assert not ok and "ancestor" in reason


def test_a_results_or_power_file_added_after_the_commit_closes_the_gate(repo):
    rp, store = repo
    sha = _commit_readout(rp, _readout(store))
    run.main(["unlock", "--commit", sha])
    assert io.holdout_unlocked()
    (store / "results_exploration__market_sigma4.json").write_text("{}", encoding="utf-8")   # explored after unlock
    io.clear_gate_cache()
    ok, reason = io.gate_status()
    assert not ok and "results_exploration__market_sigma4.json" in reason


def test_the_gate_cache_notices_head_moving_off_the_readout(repo):
    rp, store = repo
    sha = _commit_readout(rp, _readout(store))
    run.main(["unlock", "--commit", sha])
    assert io.holdout_unlocked()
    _git(rp, "checkout", "-q", "-b", "elsewhere", "HEAD~1")             # HEAD no longer descends from the readout
    assert not io.holdout_unlocked()                                     # no clear_gate_cache() on purpose


def test_cli_no_longer_accepts_knobs_that_could_bypass_the_registered_run():
    for flag in (["--require-path", "x"], ["--boot-n", "5"], ["--placebo-n", "5"]):
        with pytest.raises(SystemExit):
            run.main(["power", *flag])
