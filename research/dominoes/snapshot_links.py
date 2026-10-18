"""Stage: snapshot the study universe and links from the production graph (read-only).

Writes three frames to the research store:
- firms           one row per US-listed tickered Company node (foreign-only listings excluded, §13);
- links           study links expanded per shocked firm: "if A shocks, B is A's <role>";
- any_edge_pairs  every edge of any type/extractor/threshold between two universe firms, undirected —
                  used to keep controls unlinked from the shocked firm (spec §4).
"""
from __future__ import annotations

import json
import math
import re
import sqlite3

import pandas as pd

from research.dominoes import config, io

FIRM_SCHEMA = {"firm_id": "str", "ticker": "str", "symbol": "str", "sector": "str", "name": "str"}
LINK_SCHEMA = {"a_id": "str", "b_id": "str", "role": "str", "extractors": "str", "weight": "float"}
PAIR_SCHEMA = {"x_id": "str", "y_id": "str"}

_US_TICKER = re.compile(r"^[A-Z]{1,5}([.-][A-Z])?$")


def us_symbol(tickers: list[str] | None) -> str | None:
    """Yahoo symbol for the first US-style ticker ('BRK.B' -> 'BRK-B'); None for foreign-only listings."""
    for t in tickers or []:
        if _US_TICKER.match(t):
            return t.replace(".", "-")
    return None


def load_firms(conn: sqlite3.Connection) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT id, name, tickers, sector FROM nodes "
        "WHERE type='Company' AND tickers IS NOT NULL AND tickers NOT IN ('[]','')").fetchall()
    out = []
    for node_id, name, tickers_json, sector in rows:
        tickers = json.loads(tickers_json) if tickers_json else []
        symbol = us_symbol(tickers)
        if symbol is None:
            continue
        ticker = next(t for t in tickers if _US_TICKER.match(t))
        out.append({"firm_id": node_id, "ticker": ticker, "symbol": symbol,
                    "sector": config.normalize_sector(sector) or "", "name": name or ""})
    return pd.DataFrame(out, columns=list(FIRM_SCHEMA)).sort_values("firm_id").reset_index(drop=True)


def load_links(conn: sqlite3.Connection, firm_ids: set[str]) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT source, target, type, weight, prov_extracted_by, COALESCE(below_threshold, 0) FROM edges "
        "WHERE type IN ('supplies', 'competes_with')").fetchall()
    recs: dict[tuple, dict] = {}
    for src, tgt, etype, weight, extractor, below in rows:
        if below or extractor in config.EXCLUDED_EXTRACTORS or src == tgt:
            continue
        if src not in firm_ids or tgt not in firm_ids:
            continue
        if etype == "supplies":   # src supplies tgt
            roles = ((tgt, src, "supplier"), (src, tgt, "customer"))
        else:
            roles = ((src, tgt, "competitor"), (tgt, src, "competitor"))
        for a, b, role in roles:
            cur = recs.setdefault((a, b, role), {"extractors": set(), "weight": None})
            cur["extractors"].add(extractor or "")
            if weight is not None and (cur["weight"] is None or weight > cur["weight"]):
                cur["weight"] = float(weight)
    out = [{"a_id": a, "b_id": b, "role": role, "extractors": "|".join(sorted(v["extractors"])),
            "weight": v["weight"] if v["weight"] is not None else math.nan}
           for (a, b, role), v in recs.items()]
    df = pd.DataFrame(out, columns=list(LINK_SCHEMA))
    df["weight"] = df["weight"].astype(float)
    return df.sort_values(["a_id", "b_id", "role"]).reset_index(drop=True)


def load_any_edge_pairs(conn: sqlite3.Connection, firm_ids: set[str]) -> pd.DataFrame:
    pairs = set()
    for src, tgt in conn.execute("SELECT source, target FROM edges").fetchall():
        if src != tgt and src in firm_ids and tgt in firm_ids:
            pairs.add((min(src, tgt), max(src, tgt)))
    return pd.DataFrame(sorted(pairs), columns=list(PAIR_SCHEMA))


def snapshot(conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    conn = conn or io.prod_conn()
    try:
        firms = load_firms(conn)
        ids = set(firms["firm_id"])
        links = load_links(conn, ids)
        pairs = load_any_edge_pairs(conn, ids)
    finally:
        if own:
            conn.close()
    io.write_frame("firms", firms, FIRM_SCHEMA)
    io.write_frame("links", links, LINK_SCHEMA)
    io.write_frame("any_edge_pairs", pairs, PAIR_SCHEMA)
    return {"firms": len(firms), "links": len(links), "any_edge_pairs": len(pairs)}
