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


def test_parse_yf_frame_can_read_the_unadjusted_close_column():
    idx = pd.to_datetime(["2020-01-02", "2020-01-03"])
    cols = pd.MultiIndex.from_tuples([("AAA", "Adj Close"), ("AAA", "Close"), ("AAA", "Volume")])
    raw = pd.DataFrame([[9.0, 10.0, 100.0], [9.9, 11.0, 200.0]], index=idx, columns=cols)
    out = fp.parse_yf_frame(raw, ["AAA"], close_col="Close")
    assert list(out["AAA"]["close"]) == [10.0, 11.0]
    assert list(fp.parse_yf_frame(raw, ["AAA"], close_col="Adj Close")["AAA"]["close"]) == [9.0, 9.9]


def test_raw_snapshot_is_separate_pinned_to_the_price_manifest_and_never_refetched(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        fp.fetch_raw(["AAA"], downloader=lambda s, a, b: {})      # needs the adjusted snapshot's manifest first

    def adj(symbols, start, end):
        return {s: pd.DataFrame({"date": pd.to_datetime(["2020-01-02", "2020-01-03"]),
                                 "close": [1.0, 1.1], "volume": [10.0, 20.0]}) for s in symbols}

    fp.fetch(["AAA", "BBB"], "2013-09-01", "2026-10-17", downloader=adj)
    before = (tmp_path / "prices.parquet").read_bytes()
    calls, spans = [], []

    def raw(symbols, start, end):
        calls.append(list(symbols))
        spans.append((start, end))
        return {s: pd.DataFrame({"date": pd.to_datetime(["2020-01-02", "2020-01-03"]),
                                 "close": [2.0, 2.2], "volume": [10.0, 20.0]}) for s in symbols}

    r1 = fp.fetch_raw(["AAA", "BBB"], downloader=raw)
    assert r1["fetched"] == 2 and spans == [("2013-09-01", "2026-10-17")]
    r2 = fp.fetch_raw(["AAA", "BBB"], downloader=raw)
    assert r2["skipped_cached"] == 2 and len(calls) == 1
    assert (tmp_path / "prices.parquet").read_bytes() == before        # adjusted snapshot untouched
    got = fp.load_prices_raw()
    assert set(got.columns) >= {"symbol", "date", "close_raw", "volume"} and got["close_raw"].max() == 2.2


def test_raw_sanity_counts_volume_mismatches_and_bad_adjustment_ratios():
    d = pd.to_datetime(["2020-01-02", "2020-01-03", "2020-01-06"])
    prices = pd.DataFrame({"symbol": ["A"] * 3 + ["B"] * 3, "date": list(d) * 2,
                           "close": [0.9, 0.95, 1.0, 2.0, 2.0, 2.0], "volume": [5.0, 5.0, 5.0, 7.0, 7.0, 7.0]})
    raw = pd.DataFrame({"symbol": ["A"] * 3 + ["B"] * 3, "date": list(d) * 2,
                        "close_raw": [1.0, 1.0, 1.0, 1.0, 1.0, 1.0], "volume": [5.0, 5.0, 5.0, 7.0, 14.0, 7.0]})
    s = fp.raw_sanity(prices, raw)
    assert s["volume_mismatch_rows"] == 1 and s["volume_mismatch_symbols"] == ["B"]
    assert s["ratio_violation_symbols"] == ["B"]          # adjusted close above the raw close
    assert s["symbols_missing_raw"] == []


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
