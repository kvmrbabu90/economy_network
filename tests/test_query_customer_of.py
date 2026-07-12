"""Hermetic tests for customer_of derivation excluding Region market-buckets.

A company gets a `supplies` edge to its Region (company --supplies--> region:us-consumer)
purely for impact aggregation. Reversing it into `customer_of` would synthesize a
nonsensical "region customer_of company" edge. These tests pin the exclusion without
depending on the live DB.
"""
from __future__ import annotations

from schema import store
from api.query import get_node_edges

ACME = "cik:0000000001"
SUPPLIER = "cik:0000000002"
REGION = "region:us-consumer"


def _seed(tmp_path):
    conn = store.connect(tmp_path / "q.db"); store.init_db(conn)
    for nid, typ, name in [(ACME, "Company", "Acme"),
                           (SUPPLIER, "Company", "SupplierCo"),
                           (REGION, "Region", "US Consumer")]:
        conn.execute("INSERT INTO nodes (id, type, name) VALUES (?,?,?)", (nid, typ, name))

    def supplies(eid, src, tgt):
        conn.execute(
            "INSERT INTO edges (id, source, target, type, directed, confidence, "
            "prov_filing, prov_url, prov_snippet, prov_extracted_by) "
            "VALUES (?,?,?,?,1,0.9,'','','snip','rule')", (eid, src, tgt, "supplies"))

    supplies("e_real", SUPPLIER, ACME)      # SupplierCo --supplies--> Acme  (real supply chain)
    supplies("e_region", ACME, REGION)      # Acme --supplies--> region      (aggregation edge)
    conn.commit()
    return conn


def _customer_of(edges):
    return [e for e in edges if e["attributes"]["type"] == "customer_of"]


def test_customer_of_excludes_region_market_bucket(tmp_path):
    conn = _seed(tmp_path)
    derived = _customer_of(get_node_edges(conn, ACME, types=["customer_of"]))
    # The real reversal is kept: "Acme customer_of SupplierCo" (Acme buys from SupplierCo).
    assert any(e["source"] == ACME and e["target"] == SUPPLIER for e in derived)
    # The aggregation edge does NOT produce a "region customer_of Acme" edge.
    assert not any(e["source"] == REGION or e["target"] == REGION for e in derived)


def test_customer_of_real_supplier_still_derived(tmp_path):
    # Sanity: the exclusion does not drop legitimate company<->company customer_of.
    conn = _seed(tmp_path)
    derived = _customer_of(get_node_edges(conn, SUPPLIER, types=["customer_of"]))
    assert any(e["source"] == ACME and e["target"] == SUPPLIER for e in derived)
