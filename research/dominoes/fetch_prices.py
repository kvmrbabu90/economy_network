"""Stage: daily adjusted close + volume snapshot from Yahoo Finance (yfinance).

Cached in the research store as `prices.parquet` with a manifest. The start and end dates are pinned by
the FIRST fetch so the snapshot is fixed; later calls only fetch symbols never attempted before and never
re-fetch anything cached (CLAUDE.md #6). The downloader is injectable so tests never touch the network.
"""
from __future__ import annotations

import json
import time
from typing import Callable

import pandas as pd

from research.dominoes import io

PRICE_SCHEMA = {"symbol": "str", "date": "date", "close": "float", "volume": "float"}
MANIFEST = "fetch_manifest.json"

Downloader = Callable[[list, str, str], dict]


def parse_yf_frame(raw: pd.DataFrame | None, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """yfinance `download(..., group_by='ticker')` output -> {symbol: frame(date, close, volume)}."""
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
        if "Close" not in sub.columns:
            continue
        idx = pd.DatetimeIndex(sub.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        vol = sub["Volume"].astype(float).to_numpy() if "Volume" in sub.columns else float("nan")
        df = pd.DataFrame({"date": idx.normalize(), "close": sub["Close"].astype(float).to_numpy(), "volume": vol})
        df = df.dropna(subset=["close"]).reset_index(drop=True)
        if len(df):
            out[s] = df
    return out


def yf_downloader(symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    raw = yf.download(list(symbols), start=start, end=end, auto_adjust=True, group_by="ticker",
                      threads=True, progress=False)
    return parse_yf_frame(raw, list(symbols))


def load_prices() -> pd.DataFrame:
    return io.read_frame("prices", PRICE_SCHEMA)


def fetch(symbols: list[str], start: str, end: str, downloader: Downloader | None = None,
          batch: int = 50, pause_s: float = 1.0) -> dict:
    store = io.store_dir()
    store.mkdir(parents=True, exist_ok=True)
    man_path = store / MANIFEST
    man = (json.loads(man_path.read_text(encoding="utf-8")) if man_path.exists()
           else {"start": start, "end": end, "fetched": [], "failed": []})
    start, end = man["start"], man["end"]                  # pinned by the first fetch
    wanted = list(dict.fromkeys(symbols))
    done = set(man["fetched"]) | set(man["failed"])
    todo = [s for s in wanted if s not in done]
    dl = downloader or yf_downloader
    frames, fetched, failed = [], [], []
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        got = dl(chunk, start, end)
        for s in chunk:
            df = got.get(s)
            if df is None or df.empty:
                failed.append(s)
            else:
                d = df[["date", "close", "volume"]].copy()
                d.insert(0, "symbol", s)
                frames.append(d)
                fetched.append(s)
        if downloader is None and pause_s:
            time.sleep(pause_s)                             # politeness for the real network only
    if frames:
        new = pd.concat(frames, ignore_index=True)
        new["volume"] = new["volume"].astype(float)
        merged = pd.concat([load_prices(), new], ignore_index=True) if io.frame_path("prices").exists() else new
        io.write_frame("prices", merged[list(PRICE_SCHEMA)], PRICE_SCHEMA)
    man["fetched"] = sorted(set(man["fetched"]) | set(fetched))
    man["failed"] = sorted(set(man["failed"]) | set(failed))
    man_path.write_text(json.dumps(man, indent=2), encoding="utf-8")
    return {"requested": len(wanted), "fetched": len(fetched), "failed": len(failed),
            "skipped_cached": len(wanted) - len(todo)}
