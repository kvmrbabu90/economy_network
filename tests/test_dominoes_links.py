"""Link snapshot: which firms are in the universe, which edges become study links with which roles, and
which pairs count as 'linked at all' for control exclusion (spec §3, §4, §13)."""
from __future__ import annotations

import json
import sqlite3

import pandas as pd
import pytest

from research.dominoes import snapshot_links as sl


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE nodes (id TEXT, type TEXT, name TEXT, tickers TEXT, sector TEXT)")
    c.execute("CREATE TABLE edges (id TEXT, source TEXT, target TEXT, type TEXT, weight REAL, "
              "prov_extracted_by TEXT, below_threshold INTEGER)")
    nodes = [
        ("cik:A", "Company", "Alpha", ["AAA"], "Information Technology"),
        ("cik:B", "Company", "Berk", ["BRK.B"], "Financials"),
        ("cik:C", "Company", "Cee", ["CCC"], "Healthcare"),
        ("cik:D", "Company", "Dee", ["DDD"], "Health Care"),
        ("cik:E", "Company", "Untickered", [], "Industrials"),
        ("cik:G", "Company", "CATL", ["300750.SZ"], "Industrials"),
        ("cik:H", "Company", "Novo", ["NVO", "NOVO-B.CO"], "Health Care"),
        ("commodity:gold", "Commodity", "Gold", None, None),
    ]
    c.executemany("INSERT INTO nodes VALUES (?,?,?,?,?)",
                  [(i, t, n, json.dumps(tk) if tk is not None else None, s) for i, t, n, tk, s in nodes])
    edges = [
        ("e1", "cik:A", "cik:B", "supplies", 0.2, "llm:claude-cli", 0),
        ("e2", "cik:C", "cik:A", "supplies", None, "llm:claude-cli", 0),
        ("e3", "cik:A", "cik:D", "competes_with", None, "llm:claude-cli", 0),
        ("e4", "cik:A", "cik:C", "competes_with", None, "wikidata", 0),            # excluded extractor
        ("e5", "cik:B", "cik:D", "competes_with", None, "inference:co-mention", 1),  # excluded
        ("e6", "cik:D", "cik:B", "supplies", None, "llm:claude-cli", 1),          # below threshold
        ("e7", "cik:A", "cik:E", "supplies", None, "llm:claude-cli", 0),          # E untickered
        ("e8", "cik:A", "regulator:sec", "regulated_by", None, "rule", 0),        # not company-company
        ("e9", "cik:H", "cik:A", "supplies", None, "manual:curation", 0),
        ("e10", "cik:C", "cik:A", "supplies", 0.4, "manual:curation", 0),          # duplicate role C->A
        ("e11", "cik:G", "cik:A", "supplies", None, "llm:claude-cli", 0),         # G excluded (foreign-only)
    ]
    c.executemany("INSERT INTO edges VALUES (?,?,?,?,?,?,?)", edges)
    return c


def test_us_symbol_rules():
    assert sl.us_symbol(["AAA"]) == "AAA"
    assert sl.us_symbol(["BRK.B"]) == "BRK-B"
    assert sl.us_symbol(["NVO", "NOVO-B.CO"]) == "NVO"
    assert sl.us_symbol(["300750.SZ"]) is None
    assert sl.us_symbol([]) is None


def test_firms_are_tickered_us_listed_companies_with_normalized_sectors():
    firms = sl.load_firms(_db()).set_index("firm_id")
    assert set(firms.index) == {"cik:A", "cik:B", "cik:C", "cik:D", "cik:H"}
    assert firms.loc["cik:C", "sector"] == "Health Care"
    assert firms.loc["cik:B", "symbol"] == "BRK-B" and firms.loc["cik:B", "ticker"] == "BRK.B"
    assert firms.loc["cik:H", "symbol"] == "NVO"


def test_study_links_and_roles():
    c = _db()
    firms = sl.load_firms(c)
    links = sl.load_links(c, set(firms["firm_id"]))
    got = {(r.a_id, r.b_id, r.role) for r in links.itertuples()}
    assert got == {
        ("cik:A", "cik:B", "customer"), ("cik:B", "cik:A", "supplier"),     # e1: A supplies B
        ("cik:A", "cik:C", "supplier"), ("cik:C", "cik:A", "customer"),     # e2/e10: C supplies A
        ("cik:A", "cik:D", "competitor"), ("cik:D", "cik:A", "competitor"),  # e3
        ("cik:A", "cik:H", "supplier"), ("cik:H", "cik:A", "customer"),     # e9: H supplies A
    }
    assert len(links) == len(got)                                            # one row per (a, b, role)
    ac = links[(links.a_id == "cik:A") & (links.b_id == "cik:C")].iloc[0]
    assert ac["extractors"] == "llm:claude-cli|manual:curation" and ac["weight"] == pytest.approx(0.4)
    ab = links[(links.a_id == "cik:A") & (links.b_id == "cik:B")].iloc[0]
    assert ab["weight"] == pytest.approx(0.2) and ab["extractors"] == "llm:claude-cli"
    assert pd.isna(links[(links.a_id == "cik:A") & (links.b_id == "cik:D")].iloc[0]["weight"])


def test_any_edge_pairs_include_every_edge_between_universe_firms():
    c = _db()
    firms = sl.load_firms(c)
    pairs = sl.load_any_edge_pairs(c, set(firms["firm_id"]))
    got = {tuple(sorted((r.x_id, r.y_id))) for r in pairs.itertuples()}
    assert got == {("cik:A", "cik:B"), ("cik:A", "cik:C"), ("cik:A", "cik:D"),
                   ("cik:B", "cik:D"), ("cik:A", "cik:H")}
    assert all(r.x_id < r.y_id for r in pairs.itertuples())                 # stored undirected, x < y


def test_snapshot_writes_three_schema_checked_frames(tmp_path, monkeypatch):
    monkeypatch.setenv("DOMINO_STORE", str(tmp_path))
    summary = sl.snapshot(_db())
    assert summary == {"firms": 5, "links": 8, "any_edge_pairs": 5}
    for name in ("firms", "links", "any_edge_pairs"):
        assert (tmp_path / f"{name}.parquet").exists()
