"""Stage: daily price snapshots from Yahoo Finance (yfinance).

- `prices.parquet` (+ fetch_manifest.json): total-return-adjusted close (splits AND dividends) + volume. All
  returns come from this snapshot.
- `prices_raw.parquet` (+ fetch_manifest_raw.json): the split-adjusted but NOT dividend-adjusted close
  (`auto_adjust=False` 'Close') and that download's volume. Dollar volume (control matching, H6 size terciles)
  comes only from this snapshot, because a dividend back-adjusted close moves with dividends paid after the
  event. It is a separate cache pinned to the adjusted snapshot's start/end, so the cached returns are never
  touched.

Start and end dates are pinned by the FIRST fetch; later calls only fetch symbols never attempted before and never
re-fetch anything cached (CLAUDE.md #6). Downloaders are injectable so tests never touch the network.
"""
from __future__ import annotations

import json
import time
from typing import Callable

import numpy as np
import pandas as pd

from research.dominoes import io

PRICE_SCHEMA = {"symbol": "str", "date": "date", "close": "float", "volume": "float"}
RAW_SCHEMA = {"symbol": "str", "date": "date", "close_raw": "float", "volume": "float"}
MANIFEST = "fetch_manifest.json"
RAW_MANIFEST = "fetch_manifest_raw.json"

Downloader = Callable[[list, str, str], dict]


def parse_yf_frame(raw: pd.DataFrame | None, symbols: list[str], close_col: str = "Close") -> dict[str, pd.DataFrame]:
    """yfinance `download(..., group_by='ticker')` output -> {symbol: frame(date, close, volume)}, reading the
    close from `close_col` ('Close' is adjusted under auto_adjust=True and raw under auto_adjust=False)."""
    out: dict[str, pd.DataFrame] = {}
    if raw is None or raw.empty:
        return out
    cols = raw.columns
    for s in symbols:
        if isinstance(cols, pd.MultiIndex):
            if s in cols.get_level_values(0):
                sub = raw[s]
            elif s in cols.get_level_values(1):
                sub = raw.xs(s, axis=1, level=1)
            else:
                continue
        elif len(symbols) == 1:
            sub = raw
        else:
            continue
        if close_col not in sub.columns:
            continue
        idx = pd.DatetimeIndex(sub.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        vol = sub["Volume"].astype(float).to_numpy() if "Volume" in sub.columns else float("nan")
        df = pd.DataFrame({"date": idx.normalize(), "close": sub[close_col].astype(float).to_numpy(), "volume": vol})
        df = df.dropna(subset=["close"]).reset_index(drop=True)
        if len(df):
            out[s] = df
    return out


def yf_downloader(symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    raw = yf.download(list(symbols), start=start, end=end, auto_adjust=True, group_by="ticker",
                      threads=True, progress=False)
    return parse_yf_frame(raw, list(symbols))


def yf_raw_downloader(symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    raw = yf.download(list(symbols), start=start, end=end, auto_adjust=False, group_by="ticker",
                      threads=True, progress=False)
    return parse_yf_frame(raw, list(symbols), close_col="Close")


def load_prices() -> pd.DataFrame:
    return io.read_frame("prices", PRICE_SCHEMA)


def load_prices_raw() -> pd.DataFrame:
    return io.read_frame("prices_raw", RAW_SCHEMA)


def _fetch_into(frame: str, manifest: str, schema: dict, close_name: str, symbols: list[str], start: str,
                end: str, downloader: Downloader, batch: int, pause_s: float, network: bool) -> dict:
    store = io.store_dir()
    store.mkdir(parents=True, exist_ok=True)
    man_path = store / manifest
    man = (json.loads(man_path.read_text(encoding="utf-8")) if man_path.exists()
           else {"start": start, "end": end, "fetched": [], "failed": []})
    start, end = man["start"], man["end"]                  # pinned by the first fetch
    wanted = list(dict.fromkeys(symbols))
    done = set(man["fetched"]) | set(man["failed"])
    todo = [s for s in wanted if s not in done]
    frames, fetched, failed = [], [], []
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        got = downloader(chunk, start, end)
        for s in chunk:
            df = got.get(s)
            if df is None or df.empty:
                failed.append(s)
            else:
                d = df[["date", "close", "volume"]].rename(columns={"close": close_name}).copy()
                d.insert(0, "symbol", s)
                frames.append(d)
                fetched.append(s)
        if network and pause_s:
            time.sleep(pause_s)                             # politeness for the real network only
    if frames:
        new = pd.concat(frames, ignore_index=True)
        new["volume"] = new["volume"].astype(float)
        old = io.read_frame(frame, schema) if io.frame_path(frame).exists() else None
        merged = pd.concat([old, new], ignore_index=True) if old is not None else new
        io.write_frame(frame, merged[list(schema)], schema)
    man["fetched"] = sorted(set(man["fetched"]) | set(fetched))
    man["failed"] = sorted(set(man["failed"]) | set(failed))
    io.write_json(man_path, man)
    return {"requested": len(wanted), "fetched": len(fetched), "failed": len(failed),
            "skipped_cached": len(wanted) - len(todo)}


def fetch(symbols: list[str], start: str, end: str, downloader: Downloader | None = None,
          batch: int = 50, pause_s: float = 1.0) -> dict:
    return _fetch_into("prices", MANIFEST, PRICE_SCHEMA, "close", symbols, start, end,
                       downloader or yf_downloader, batch, pause_s, downloader is None)


def fetch_raw(symbols: list[str], downloader: Downloader | None = None, batch: int = 50,
              pause_s: float = 1.0) -> dict:
    """Unadjusted-close snapshot for dollar volume, pinned to the ADJUSTED snapshot's start/end."""
    adj_man = io.store_dir() / MANIFEST
    if not adj_man.exists():
        raise FileNotFoundError("fetch the adjusted price snapshot first: the raw snapshot reuses its dates")
    pinned = json.loads(adj_man.read_text(encoding="utf-8"))
    return _fetch_into("prices_raw", RAW_MANIFEST, RAW_SCHEMA, "close_raw", symbols, pinned["start"],
                       pinned["end"], downloader or yf_raw_downloader, batch, pause_s, downloader is None)


def raw_sanity(prices: pd.DataFrame, raw: pd.DataFrame, tol: float = 1e-6) -> dict:
    """Consistency of the raw snapshot with the adjusted one (data-error handling, §12):
    - volume mismatches on common (symbol, date) rows (e.g. a split between the two downloads);
    - adjusted close above the raw close (a dividend adjustment can only lower past closes);
    - symbols in the adjusted snapshot with no raw history (their dollar volume is NaN, never a fallback)."""
    m = prices.merge(raw, on=["symbol", "date"], how="inner", suffixes=("", "_raw"))
    vol_bad = (m["volume"] - m["volume_raw"]).abs() > 0.5
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = m["close"] / m["close_raw"]
    ratio_bad = ratio > 1 + tol
    return {"common_rows": int(len(m)),
            "volume_mismatch_rows": int(vol_bad.sum()),
            "volume_mismatch_symbols": sorted(m.loc[vol_bad, "symbol"].unique().tolist()),
            "ratio_violation_rows": int(ratio_bad.sum()),
            "ratio_violation_symbols": sorted(m.loc[ratio_bad, "symbol"].unique().tolist()),
            "symbols_missing_raw": sorted(set(prices["symbol"]) - set(raw["symbol"]))}
