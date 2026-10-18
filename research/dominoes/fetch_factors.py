"""Stage (robustness 2 only): Fama-French 5 factors + momentum, daily, from the Ken French data library.

Each zip is treated as untrusted data: it is extracted into its own new, empty directory, member names
are flattened to basenames (no zip-slip), only .csv members are read, and the CSV is parsed as text —
nothing from the archive is ever executed or imported. Cached: never re-downloaded once parsed.
"""
from __future__ import annotations

import io as _io
import re
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

import pandas as pd

from research.dominoes import io

FIVE_URL = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
            "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip")
MOM_URL = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Momentum_Factor_daily_CSV.zip"
FACTOR_SCHEMA = {"date": "date", "mkt_rf": "float", "smb": "float", "hml": "float", "rmw": "float",
                 "cma": "float", "rf": "float", "mom": "float"}
_RENAME = {"Mkt-RF": "mkt_rf", "SMB": "smb", "HML": "hml", "RMW": "rmw", "CMA": "cma", "RF": "rf", "Mom": "mom"}


def parse_french_csv(text: str) -> pd.DataFrame:
    """Daily Ken French CSV text -> date + factor columns in decimals (file values are percent)."""
    lines = text.splitlines()
    hdr = next(i for i, ln in enumerate(lines)
               if ln.startswith(",") and any(k in ln for k in _RENAME))
    cols = [_RENAME[c.strip()] for c in lines[hdr].split(",")[1:] if c.strip()]
    recs = []
    for ln in lines[hdr + 1:]:
        parts = [p.strip() for p in ln.split(",")]
        if not parts or not re.fullmatch(r"\d{8}", parts[0]):
            break
        recs.append([parts[0]] + [float(p) / 100.0 for p in parts[1:1 + len(cols)]])
    df = pd.DataFrame(recs, columns=["date"] + cols)
    df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
    return df


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "EconGraph research (domino study)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _extract_csv(data: bytes, raw_dir: Path) -> str:
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix="ff_", dir=raw_dir))      # its own new, empty directory
    with zipfile.ZipFile(_io.BytesIO(data)) as z:
        for member in z.infolist():
            name = Path(member.filename).name                         # flatten: no path traversal
            if member.is_dir() or not name.lower().endswith(".csv"):
                continue
            path = target / name
            path.write_bytes(z.read(member))
            return path.read_text(encoding="latin-1")
    raise ValueError("no CSV member in factor archive")


def fetch_factors(fetcher: Callable[[str], bytes] | None = None) -> int:
    if io.frame_path("factors").exists():
        return len(io.read_frame("factors", FACTOR_SCHEMA))
    get = fetcher or _http_get
    raw_dir = io.store_dir() / "factors_raw"
    five = parse_french_csv(_extract_csv(get(FIVE_URL), raw_dir))
    mom = parse_french_csv(_extract_csv(get(MOM_URL), raw_dir))
    merged = five.merge(mom, on="date", how="inner")[list(FACTOR_SCHEMA)]
    io.write_frame("factors", merged, FACTOR_SCHEMA)
    return len(merged)
