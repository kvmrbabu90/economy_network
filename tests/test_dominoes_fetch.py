"""Fetch stages: yfinance price snapshot (cached, never re-fetched, one fixed end date) and the Ken French
factor files (downloaded into their own directory, parsed as untrusted data). No network in tests."""
from __future__ import annotations

import io as _io
import json
import zipfile

import pandas as pd
import pytest

from research.dominoes import fetch_factors as ff
from research.dominoes import fetch_prices as fp


def test_parse_yf_frame_splits_tickers_and_drops_missing_closes():
    idx = pd.to_datetime(["2020-01-02", "2020-01-03"])
    cols = pd.MultiIndex.from_tuples([("AAA", "Close"), ("AAA", "Volume"), ("BBB", "Close"), ("BBB", "Volume")])
    raw = pd.DataFrame([[10.0, 100.0, float("nan"), float("nan")], [11.0, 200.0, 5.0, 50.0]], index=idx, columns=cols)
    out = fp.parse_yf_frame(raw, ["AAA", "BBB", "CCC"])
    assert list(out["AAA"]["close"]) == [10.0, 11.0] and list(out["AAA"]["volume"]) == [100.0, 200.0]
    assert list(out["BBB"]["close"]) == [5.0]
    assert "CCC" not in out


def test_fetch_caches_never_refetches_and_pins_the_end_date(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    calls, ends = [], []

    def fake(symbols, start, end):
        calls.append(list(symbols))
        ends.append(end)
        return {s: pd.DataFrame({"date": pd.to_datetime(["2020-01-02", "2020-01-03"]),
                                 "close": [1.0, 1.1], "volume": [10.0, 20.0]})
                for s in symbols if s != "DEAD"}

    r1 = fp.fetch(["AAA", "BBB", "DEAD"], "2013-09-01", "2026-10-18", downloader=fake, batch=2)
    assert r1 == {"requested": 3, "fetched": 2, "failed": 1, "skipped_cached": 0}
    assert sorted(sum(calls, [])) == ["AAA", "BBB", "DEAD"]

    calls.clear()
    r2 = fp.fetch(["AAA", "BBB", "DEAD"], "2013-09-01", "2026-10-18", downloader=fake)
    assert calls == [] and r2["skipped_cached"] == 3

    ends.clear()
    r3 = fp.fetch(["AAA", "CCC"], "2013-09-01", "2027-01-01", downloader=fake)
    assert calls == [["CCC"]] and r3["fetched"] == 1
    assert ends == ["2026-10-18"]                       # snapshot end stays pinned to the first fetch

    prices = fp.load_prices()
    assert set(prices["symbol"]) == {"AAA", "BBB", "CCC"} and len(prices) == 6
    man = json.loads((tmp_path / "fetch_manifest.json").read_text(encoding="utf-8"))
    assert man["end"] == "2026-10-18" and man["failed"] == ["DEAD"]


_FIVE = """This file was created by CMPT_ME_BEME_OP_INV_RETS_DAILY using the 202508 CRSP database.
The 1-month TBill return is from Ibbotson and Associates Inc.

,Mkt-RF,SMB,HML,RMW,CMA,RF
19630701,   -0.67,    0.00,   -0.32,   -0.01,    0.15,    0.012
19630702,    0.79,   -0.27,    0.27,   -0.07,   -0.19,    0.012

Copyright 2025 Eugene F. Fama and Kenneth R. French
"""

_MOM = """This file was created by CMPT_ME_PRIOR_RETS_DAILY using the 202508 CRSP database.

,Mom
19630701,   0.56
19630702,  -0.50
"""


def test_parse_french_csv_reads_decimal_factors():
    five = ff.parse_french_csv(_FIVE)
    assert list(five.columns) == ["date", "mkt_rf", "smb", "hml", "rmw", "cma", "rf"]
    assert len(five) == 2 and five["mkt_rf"].iloc[0] == pytest.approx(-0.0067) and five["rf"].iloc[0] == pytest.approx(0.00012)
    mom = ff.parse_french_csv(_MOM)
    assert list(mom.columns) == ["date", "mom"] and mom["mom"].iloc[1] == pytest.approx(-0.0050)


def _zip(name, text):
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


def test_fetch_factors_extracts_into_its_own_dir_merges_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    served = []

    def fetcher(url):
        served.append(url)
        return _zip("../evil/F-F_5.csv", _FIVE) if "5_Factors" in url else _zip("F-F_Mom.csv", _MOM)

    n = ff.fetch_factors(fetcher=fetcher)
    assert n == 2 and len(served) == 2
    f = pd.read_parquet(tmp_path / "factors.parquet")
    assert list(f.columns) == ["date", "mkt_rf", "smb", "hml", "rmw", "cma", "rf", "mom"]
    assert not any(p.name == "evil" for p in tmp_path.rglob("*"))   # zip-slip names flattened to basenames
    assert ff.fetch_factors(fetcher=fetcher) == 2 and len(served) == 2   # cached: no second download
