"""Phase 4 loader: idempotency + referential integrity + graphology shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.build_graph import (
    _undirected_for,
    compute_stats,
    load_into_sqlite,
    to_graphology,
)
from schema.store import connect


def _prov(extracted_by="llm:claude-cli", filing="0000080424-25-000076"):
    return {
        "filing": filing,
        "url": "https://www.sec.gov/Archives/edgar/data/80424/" + filing + ".htm",
        "snippet": "We compete with Kimberly-Clark and Colgate-Palmolive.",
        "extracted_by": extracted_by,
    }


def _node(nid, name, *, ntype="Company", provisional=False, sector=None):
    return {
        "id": nid, "type": ntype, "name": name,
        "aliases": [name], "tickers": [],
        "identifiers": {"cik": nid.split(":")[-1]} if nid.startswith("cik:") else {},
        "sector": sector, "industry": None, "country": "US",
        "metadata": ({"provisional": True} if provisional else {}),
    }


def _edge(eid, src, tgt, etype, conf=0.9, extra=None):
    return {
        "id": eid, "source": src, "target": tgt, "type": etype,
        "directed": etype != "competes_with",
        "confidence": conf,
        "provenance": _prov(),
        "additional_provenance": extra or [],
        "weight": None,
    }


def test_load_into_sqlite_idempotent(tmp_path):
    db = tmp_path / "test.db"
    nodes = [
        _node("cik:0000080424", "Procter & Gamble"),
        _node("cik:0000104169", "Walmart"),
        _node("regulator:ftc", "Federal Trade Commission", ntype="Regulator"),
        _node("slug:nestle", "Nestle S.A.", provisional=True),
    ]
    edges = [
        _edge("e1", "cik:0000080424", "cik:0000104169", "supplies"),
        _edge("e2", "cik:0000080424", "regulator:ftc", "regulated_by", conf=1.0),
        _edge("e3", "cik:0000080424", "slug:nestle", "competes_with", conf=0.85),
    ]
    aliases = [
        {"alias": "PG", "alias_normalized": "pg", "canonical_id": "cik:0000080424", "source": "seed"},
        {"alias": "WMT", "alias_normalized": "wmt", "canonical_id": "cik:0000104169", "source": "seed"},
    ]

    r1 = load_into_sqlite(db, nodes=nodes, edges=edges, aliases=aliases, fresh=True)
    assert r1.nodes == 4 and r1.edges == 3 and r1.aliases == 2

    # Re-load (not fresh) -- counts must not increase.
    r2 = load_into_sqlite(db, nodes=nodes, edges=edges, aliases=aliases, fresh=False)
    assert r2.nodes == 4 and r2.edges == 3 and r2.aliases == 2


def test_load_into_sqlite_rejects_orphan_edge(tmp_path):
    db = tmp_path / "test.db"
    nodes = [_node("cik:0000080424", "P&G")]
    edges = [_edge("orphan-1", "cik:0000080424", "cik:9999999999", "supplies")]
    with pytest.raises(RuntimeError, match="Referential-integrity FAILED"):
        load_into_sqlite(db, nodes=nodes, edges=edges, aliases=[], fresh=True)


def test_to_graphology_shape():
    nodes = [
        _node("cik:0000080424", "P&G", sector="Consumer Staples"),
        _node("slug:nestle", "Nestle S.A.", provisional=True),
    ]
    edges = [
        _edge("e1", "cik:0000080424", "slug:nestle", "competes_with"),
        _edge("e2", "cik:0000080424", "regulator:ftc", "regulated_by", conf=1.0),
    ]
    g = to_graphology(nodes, edges)
    assert g["options"]["type"] == "mixed"
    assert g["options"]["multi"] is True
    assert len(g["nodes"]) == 2
    n0 = g["nodes"][0]
    assert n0["key"] == "cik:0000080424"
    assert n0["attributes"]["sector"] == "Consumer Staples"
    nestle = next(n for n in g["nodes"] if n["key"] == "slug:nestle")
    assert nestle["attributes"]["provisional"] is True
    # competes_with edges undirected; regulated_by directed.
    cw = next(e for e in g["edges"] if e["attributes"]["type"] == "competes_with")
    rb = next(e for e in g["edges"] if e["attributes"]["type"] == "regulated_by")
    assert cw["undirected"] is True
    assert rb["undirected"] is False
    assert cw["attributes"]["directed"] is False
    assert rb["attributes"]["directed"] is True


def test_undirected_for_only_competes_with():
    assert _undirected_for("competes_with") is True
    assert _undirected_for("supplies") is False
    assert _undirected_for("regulated_by") is False
    assert _undirected_for("part_of") is False


def test_compute_stats_separates_provisional_and_real():
    nodes = [
        _node("cik:0000080424", "P&G"),
        _node("cik:0000104169", "Walmart"),
        _node("slug:nestle", "Nestle S.A.", provisional=True),
        _node("regulator:ftc", "FTC", ntype="Regulator"),
    ]
    edges = [
        _edge("e1", "cik:0000080424", "cik:0000104169", "supplies"),
        _edge("e2", "cik:0000080424", "slug:nestle", "supplies"),  # provisional target
        _edge("e3", "cik:0000080424", "regulator:ftc", "regulated_by", conf=1.0),
        _edge("e4", "cik:0000080424", "slug:nestle", "competes_with"),
    ]
    s = compute_stats(nodes, edges)
    assert s.nodes_by_type["Company"] == 3
    assert s.nodes_by_type["Regulator"] == 1
    assert s.provisional_count == 1
    assert s.edges_by_type["supplies"] == 2
    assert s.edges_by_type_audit == {}  # no audit edges in this fixture
    assert s.supply_layer["total"] == 2
    assert s.supply_layer["to_real_filers"] == 1
    assert s.supply_layer["to_provisional_slugs"] == 1
    # Single connected component since everything touches P&G.
    assert s.components == 1

    # Audit-layer-aware view: passing the slug supplies edge as "below"
    # instead of "above" should show it in edges_by_type_audit and still
    # contribute to degree + supply-layer totals.
    above = [e for e in edges if e["target"] != "slug:nestle" or e["type"] != "supplies"]
    below = [e for e in edges if e["target"] == "slug:nestle" and e["type"] == "supplies"]
    s2 = compute_stats(nodes, above, below_edges=below)
    assert s2.edges_by_type.get("supplies") == 1
    assert s2.edges_by_type_audit.get("supplies") == 1
    assert s2.supply_layer["total"] == 2  # both still counted
