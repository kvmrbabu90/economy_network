"""Research-store paths, schema-checked parquet frames, the read-only production connection, and the
holdout gate.

The production DB is opened read-only and is located ONLY via DOMINO_PROD_DB (default: the canonical
public copy). ECONGRAPH_DB is deliberately ignored: in some shells it points at a stale pre-migration DB.
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pandas as pd
from pandas.api import types as pdt

from research.dominoes import config

REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_STORE = "C:/Users/Public/econgraph/research/dominoes"
_DEFAULT_PROD_DB = "C:/Users/Public/econgraph/econgraph.db"


def store_dir() -> Path:
    return Path(os.environ.get("DOMINO_STORE") or _DEFAULT_STORE)


def prod_conn() -> sqlite3.Connection:
    """Read-only connection to the production graph DB (URI mode=ro: writes raise)."""
    path = Path(os.environ.get("DOMINO_PROD_DB") or _DEFAULT_PROD_DB).resolve()
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _is_str_column(s: pd.Series) -> bool:
    if pdt.is_string_dtype(s.dtype) and not pdt.is_object_dtype(s.dtype):
        return True
    if pdt.is_object_dtype(s.dtype):
        return all(v is None or isinstance(v, str) or (isinstance(v, float) and math.isnan(v)) for v in s)
    return False


_KIND_CHECKS = {
    "str": _is_str_column,
    "float": lambda s: pdt.is_float_dtype(s.dtype),
    "int": lambda s: pdt.is_integer_dtype(s.dtype),
    "bool": lambda s: pdt.is_bool_dtype(s.dtype),
    "date": lambda s: pdt.is_datetime64_any_dtype(s.dtype),
}


def validate(df: pd.DataFrame, schema: dict[str, str], name: str = "frame") -> None:
    """Raise ValueError unless every schema column exists with the declared kind."""
    missing = [c for c in schema if c not in df.columns]
    if missing:
        raise ValueError(f"{name}: missing columns {missing}")
    for col, kind in schema.items():
        if kind not in _KIND_CHECKS:
            raise ValueError(f"{name}: unknown kind {kind!r} for column {col!r}")
        if len(df) and not _KIND_CHECKS[kind](df[col]):
            raise ValueError(f"{name}: column {col!r} expected {kind}, got {df[col].dtype}")


def frame_path(name: str) -> Path:
    return store_dir() / f"{name}.parquet"


def write_frame(name: str, df: pd.DataFrame, schema: dict[str, str]) -> Path:
    validate(df, schema, name)
    path = frame_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return path


def read_frame(name: str, schema: dict[str, str]) -> pd.DataFrame:
    df = pd.read_parquet(frame_path(name))
    validate(df, schema, name)
    return df


def holdout_unlocked() -> bool:
    """True only when the marker names a commit that exists in this repo (the committed exploration readout)."""
    marker = store_dir() / config.HOLDOUT_MARKER
    if not marker.exists():
        return False
    commit = marker.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        return False
    r = subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=REPO_ROOT, capture_output=True)
    return r.returncode == 0


def require_holdout_unlocked() -> None:
    if not holdout_unlocked():
        raise PermissionError(
            "Holdout is locked: commit the exploration readout and write its hash to "
            f"{store_dir() / config.HOLDOUT_MARKER} first (spec §12, §13.11).")
