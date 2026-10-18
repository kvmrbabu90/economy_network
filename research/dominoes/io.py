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


def write_json(path: Path, obj) -> Path:
    """JSON with LF line endings on every OS, so a file's sha256 is the same in the store and in git."""
    import json
    Path(path).write_text(json.dumps(obj, indent=2), encoding="utf-8", newline="\n")
    return Path(path)


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


INTEGRITY_ROW = re.compile(r"^\|\s*`([^`/\\]+\.json)`\s*\|\s*([0-9a-f]{64})\s*\|", re.MULTILINE)
REQUIRED_INTEGRITY = ("results_exploration.json", "power.json")
_GATE_CACHE: dict = {}


def clear_gate_cache() -> None:
    _GATE_CACHE.clear()


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True)


def _file_sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def committed_integrity(commit: str) -> tuple[str | None, dict[str, str], str]:
    """(full sha or None, {file: sha256} from the readout committed at `commit`, reason if unusable)."""
    full = _git("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    if full.returncode != 0:
        return None, {}, f"commit {commit} does not exist in this repository"
    sha = full.stdout.strip()
    shown = _git("show", f"{sha}:{config.READOUT_PATH}")
    if shown.returncode != 0:
        return sha, {}, f"commit {sha[:10]} does not contain the exploration readout {config.READOUT_PATH}"
    table = dict(INTEGRITY_ROW.findall(shown.stdout))
    missing = [f for f in REQUIRED_INTEGRITY if f not in table]
    if missing:
        return sha, table, f"the committed readout has no integrity hash for {missing}"
    return sha, table, ""


def gate_status() -> tuple[bool, str]:
    """The holdout opens only if the marker names a commit that (1) exists, (2) is an ancestor of HEAD, and
    (3) contains the exploration readout whose integrity table matches the store's exploration results and
    power files byte for byte (sha256)."""
    marker = store_dir() / config.HOLDOUT_MARKER
    if not marker.exists():
        return False, "no holdout marker"
    commit = marker.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        return False, "the marker does not hold a commit hash"
    key_files = sorted(p for p in store_dir().glob("*.json") if p.name.startswith(("results_exploration", "power")))
    head = _git("rev-parse", "HEAD").stdout.strip()
    key = (commit, head, config.READOUT_PATH, str(store_dir()),
           tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in key_files))
    if key in _GATE_CACHE:
        return _GATE_CACHE[key]
    sha, table, reason = committed_integrity(commit)
    if not reason and _git("merge-base", "--is-ancestor", sha, "HEAD").returncode != 0:
        reason = f"commit {sha[:10]} is not an ancestor of HEAD (re-unlock after a squash or rebase)"
    if not reason:
        extra = sorted({p.name for p in key_files} - set(table))
        if extra:
            reason = (f"{', '.join(extra)} exist in the store but not in the committed readout's integrity table "
                      "(explored after the readout was committed)")
    if not reason:
        for name, want in sorted(table.items()):
            path = store_dir() / name
            if not path.exists() or _file_sha256(path) != want:
                reason = f"{name} in the store does not match the committed readout's integrity hash"
                break
    result = (not reason, reason)
    _GATE_CACHE[key] = result
    return result


def holdout_unlocked() -> bool:
    return gate_status()[0]


def require_holdout_unlocked() -> None:
    if not holdout_unlocked():
        raise PermissionError(
            f"Holdout is locked ({gate_status()[1]}): commit the exploration readout and run "
            "`unlock --commit <hash>` first (spec §12, §13.11).")
