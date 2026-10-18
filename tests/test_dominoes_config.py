"""Domino study config + IO: constants match the frozen spec, the holdout gate, schema-checked frames,
and a read-only production connection that never follows ECONGRAPH_DB (stale in some shells)."""
from __future__ import annotations

import sqlite3
import subprocess

import pandas as pd
import pytest

from research.dominoes import config, io


def test_normalize_sector_merges_healthcare_variant():
    assert config.normalize_sector("Healthcare") == "Health Care"
    assert config.normalize_sector("Health Care") == "Health Care"
    assert config.normalize_sector(" Information Technology ") == "Information Technology"


def test_periods_match_spec():
    assert config.period_of("2014-12-31") is None
    assert config.period_of("2015-01-01") == "exploration"
    assert config.period_of("2021-12-31") == "exploration"
    assert config.period_of("2022-01-01") == "holdout"


def test_windows_and_thresholds_match_spec():
    assert config.WINDOWS == {"pre": (-5, -1), "d0": (0, 0), "w5": (1, 5), "w20": (1, 20), "w60": (1, 60)}
    assert config.SLOW_WINDOWS == ("w5", "w20", "w60")
    assert config.EST_WINDOW == (-270, -21) and config.MIN_EST_DAYS == 200
    assert config.SHOCK_SIGMA == 3.0 and config.DEDUP_DAYS == 10
    assert config.DV_WINDOW == (-80, -21) and config.MIN_DV_DAYS == 20
    assert config.K_PRIMARY == 5 and config.K_VARIANTS == (3, 5, 10) and config.MIN_CONTROLS == 2
    assert config.T_BAR == 3.0 and config.FAMILY_ALPHA == 0.05 and config.HOLDOUT_ALPHA == 0.05
    assert config.FLOOR == 0.005 and config.MDE_LIMIT == 0.015
    assert config.PLACEBO_N == 500 and config.BOOT_N == 5000
    assert config.PRICE_START == "2013-09-01"
    assert config.ROLES == ("supplier", "customer", "competitor")
    assert config.DATA_ERROR_ABS == 0.50 and config.DATA_ERROR_REVERT == 0.05


def test_excluded_extractors():
    assert config.EXCLUDED_EXTRACTORS == {"wikidata", "inference:co-mention"}


def test_store_dir_default_and_override(tmp_path, monkeypatch):
    monkeypatch.delenv("DOMINO_STORE", raising=False)
    assert str(io.store_dir()).replace("\\", "/").endswith("Public/econgraph/research/dominoes")
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    assert io.store_dir() == tmp_path


def test_frame_roundtrip_validates_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    schema = {"ticker": "str", "close": "float"}
    io.write_frame("prices_test", pd.DataFrame({"ticker": ["AAA"], "close": [1.5]}), schema)
    back = io.read_frame("prices_test", schema)
    assert list(back.columns) == ["ticker", "close"] and back["close"].iloc[0] == 1.5
    with pytest.raises(ValueError, match="missing"):
        io.write_frame("bad", pd.DataFrame({"ticker": ["A"]}), schema)
    with pytest.raises(ValueError, match="close"):
        io.write_frame("bad2", pd.DataFrame({"ticker": ["A"], "close": ["x"]}), schema)


def test_prod_conn_is_read_only_and_ignores_econgraph_db(tmp_path, monkeypatch):
    db = tmp_path / "prod.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE t(x)")
    c.commit()
    c.close()
    monkeypatch.setenv("ECONGRAPH_DB", str(tmp_path / "stale.db"))
    monkeypatch.setenv("DOMINO_PROD_DB", str(db))
    conn = io.prod_conn()
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO t VALUES (1)")
    conn.close()
    assert not (tmp_path / "stale.db").exists()


def test_holdout_locked_without_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    assert io.holdout_unlocked() is False


def test_a_real_commit_alone_does_not_unlock_the_holdout(tmp_path, monkeypatch):
    # the positive path (committed readout whose integrity table matches the store) is in test_dominoes_gate
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    io.clear_gate_cache()
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    (tmp_path / config.HOLDOUT_MARKER).write_text("0" * 40, encoding="utf-8")
    assert io.holdout_unlocked() is False
    (tmp_path / config.HOLDOUT_MARKER).write_text(head + "\n", encoding="utf-8")
    assert io.holdout_unlocked() is False
