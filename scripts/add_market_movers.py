"""
add_market_movers.py
Add high-impact non-S&P 500 companies as provisional nodes with
supply-chain and competition edges.

Companies added:
  - NIO Inc.          (wikidata:Q16957870)  — Chinese EV, NYSE:NIO
  - CATL              (wikidata:Q21751798)  — World's largest EV battery maker
  - Novo Nordisk      (wikidata:Q386708)    — GLP-1 drugs (Ozempic/Wegovy), Europe's #1 by mkt cap
  - Li Auto           (wikidata:Q78793803)  — Chinese EV, NASDAQ:LI
  - XPeng Inc.        (wikidata:Q66041928)  — Chinese EV, NYSE:XPEV
  - Roche Holding     (wikidata:Q212322)    — Swiss pharma, oncology/diagnostics
  - BMW Group         (wikidata:Q26678)     — German automaker (BMW, Mini, Rolls-Royce)

Run from repo root:
    python scripts/add_market_movers.py
"""

import sqlite3
import uuid
import json
from pathlib import Path

DB_PATH      = Path(__file__).resolve().parent.parent / "econgraph.db"
NODES_JSONL  = Path(__file__).resolve().parent.parent / "data" / "nodes.jsonl"
EDGES_JSONL  = Path(__file__).resolve().parent.parent / "data" / "edges.jsonl"
ALIASES_JSONL = Path(__file__).resolve().parent.parent / "data" / "aliases.jsonl"

CONF_HIGH   = 0.95   # well-documented public relationship
CONF_MEDIUM = 0.80   # known but less precisely documented
EXTRACTED_BY = "manual:curation"

# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

NEW_NODES = [
    # ── EV makers ────────────────────────────────────────────────────────────
    {
        "id":      "wikidata:Q16957870",
        "type":    "Company",
        "name":    "NIO Inc.",
        "sector":  "Consumer Discretionary",
        "industry":"Automobiles",
        "country": "CN",
        "tickers": json.dumps(["NIO"]),
        "identifiers": json.dumps({"wikidata": "Q16957870"}),
        "aliases": json.dumps(["NIO", "Nio", "NIO Inc", "蔚来汽车"]),
        "metadata": json.dumps({"hq": "Shanghai, China", "founded": 2014,
                                 "note": "Chinese premium EV maker; NYSE:NIO"}),
    },
    {
        "id":      "wikidata:Q78793803",
        "type":    "Company",
        "name":    "Li Auto",
        "sector":  "Consumer Discretionary",
        "industry":"Automobiles",
        "country": "CN",
        "tickers": json.dumps(["LI"]),
        "identifiers": json.dumps({"wikidata": "Q78793803"}),
        "aliases": json.dumps(["Li Auto", "LI", "Lixiang Auto", "理想汽车", "Li Auto Inc"]),
        "metadata": json.dumps({"hq": "Beijing, China", "founded": 2015,
                                 "note": "Chinese EV maker (extended-range + BEV); NASDAQ:LI"}),
    },
    {
        "id":      "wikidata:Q66041928",
        "type":    "Company",
        "name":    "XPeng Inc.",
        "sector":  "Consumer Discretionary",
        "industry":"Automobiles",
        "country": "CN",
        "tickers": json.dumps(["XPEV"]),
        "identifiers": json.dumps({"wikidata": "Q66041928"}),
        "aliases": json.dumps(["XPeng", "XPEV", "XPENG", "Xpeng Motors", "小鹏汽车"]),
        "metadata": json.dumps({"hq": "Guangzhou, China", "founded": 2014,
                                 "note": "Chinese EV maker; NYSE:XPEV"}),
    },
    # ── Batteries ────────────────────────────────────────────────────────────
    {
        "id":      "wikidata:Q21751798",
        "type":    "Company",
        "name":    "CATL",
        "sector":  "Industrials",
        "industry":"Electrical Components & Equipment",
        "country": "CN",
        "tickers": json.dumps(["300750.SZ"]),
        "identifiers": json.dumps({"wikidata": "Q21751798"}),
        "aliases": json.dumps(["CATL", "Contemporary Amperex Technology",
                                "Contemporary Amperex Technology Co. Limited",
                                "宁德时代"]),
        "metadata": json.dumps({"hq": "Ningde, Fujian, China", "founded": 2011,
                                 "note": "World's largest EV battery maker; ~37% global market share; "
                                         "supplies Tesla, VW, BMW, Mercedes, NIO, Li Auto, XPeng"}),
    },
    # ── Pharma ───────────────────────────────────────────────────────────────
    {
        "id":      "wikidata:Q386708",
        "type":    "Company",
        "name":    "Novo Nordisk",
        "sector":  "Healthcare",
        "industry":"Pharmaceuticals",
        "country": "DK",
        "tickers": json.dumps(["NVO", "NOVO-B.CO"]),
        "identifiers": json.dumps({"wikidata": "Q386708"}),
        "aliases": json.dumps(["Novo Nordisk", "NVO", "Novo Nordisk A/S",
                                "Novo", "Ozempic maker", "Wegovy maker"]),
        "metadata": json.dumps({"hq": "Bagsværd, Denmark", "founded": 1923,
                                 "note": "GLP-1 drug leader (Ozempic, Wegovy, Rybelsus); "
                                         "largest European company by market cap (~$570B)"}),
    },
    {
        "id":      "wikidata:Q212322",
        "type":    "Company",
        "name":    "Roche Holding",
        "sector":  "Healthcare",
        "industry":"Pharmaceuticals",
        "country": "CH",
        "tickers": json.dumps(["RHHBY", "ROG.SW"]),
        "identifiers": json.dumps({"wikidata": "Q212322"}),
        "aliases": json.dumps(["Roche", "Roche Holding", "Roche Holding AG",
                                "F. Hoffmann-La Roche", "Hoffmann-La Roche",
                                "RHHBY"]),
        "metadata": json.dumps({"hq": "Basel, Switzerland", "founded": 1896,
                                 "note": "Swiss pharma/diagnostics giant; oncology, "
                                         "immunology, rare diseases; owns Genentech"}),
    },
    # ── Autos ────────────────────────────────────────────────────────────────
    {
        "id":      "wikidata:Q26678",
        "type":    "Company",
        "name":    "BMW Group",
        "sector":  "Consumer Discretionary",
        "industry":"Automobiles",
        "country": "DE",
        "tickers": json.dumps(["BMW.DE", "BMWYY"]),
        "identifiers": json.dumps({"wikidata": "Q26678"}),
        "aliases": json.dumps(["BMW", "BMW Group", "BMW AG",
                                "Bayerische Motoren Werke",
                                "BMWYY"]),
        "metadata": json.dumps({"hq": "Munich, Germany", "founded": 1916,
                                 "note": "German premium automaker; BMW, Mini, Rolls-Royce brands; "
                                         "major CATL battery customer"}),
    },
]

# ---------------------------------------------------------------------------
# Aliases (rows for the aliases table)
# Each node's alias list is stored in both nodes.aliases (JSON) and the
# aliases table for fast fuzzy lookup.
# Format: (alias_text, alias_normalized, node_id)
# ---------------------------------------------------------------------------

def _aliases_for(node_id: str, alias_list: list[str]) -> list[tuple[str, str, str]]:
    rows = []
    for a in alias_list:
        rows.append((a, a.lower().strip(), node_id))
    return rows


# ---------------------------------------------------------------------------
# Edges
# Format: (source_id, target_id, edge_type, geography, confidence, snippet)
# ---------------------------------------------------------------------------

_S = "supplies"
_C = "competes_with"

# Existing node IDs needed for edges
TESLA         = "cik:0001318605"
VW_GROUP      = "wikidata:Q156578"
MERCEDES      = "wikidata:Q27530"
BYD_AUTO      = "wikidata:Q27423"
ELI_LILLY     = "cik:0000059478"
ASTRAZENECA   = "cik:0000901832"
NOVARTIS      = "cik:0001114448"
PFIZER        = "cik:0000078003"
ALBEMARLE     = "cik:0000915913"   # largest US lithium producer
SQM           = "slug:sociedad-quimica-y-minera-de-chile"
GANFENG       = "slug:jiangxi-ganfeng-lithium"
LI_CYCLE      = None   # not in graph — skip
LITHIUM       = "commodity:lithium"
NICKEL        = "commodity:nickel"
COBALT        = "commodity:cobalt"
GRAPHITE      = "commodity:graphite"

NIO           = "wikidata:Q16957870"
CATL          = "wikidata:Q21751798"
NOVO_NORDISK  = "wikidata:Q386708"
LI_AUTO       = "wikidata:Q78793803"
XPENG         = "wikidata:Q66041928"
ROCHE         = "wikidata:Q212322"
BMW_GROUP     = "wikidata:Q26678"

NEW_EDGES = [
    # ── CATL → OEM customers (batteries) ──────────────────────────────────
    (CATL, TESLA,     _S, "global", CONF_HIGH,
     "CATL is a major battery cell supplier to Tesla; confirmed in Tesla 10-K supply disclosures"),
    (CATL, VW_GROUP,  _S, "global", CONF_HIGH,
     "CATL supplies battery cells for Volkswagen Group's MEB platform EVs globally"),
    (CATL, BMW_GROUP, _S, "global", CONF_HIGH,
     "CATL has a multi-year battery supply agreement with BMW Group for iX and i4 models"),
    (CATL, MERCEDES,  _S, "global", CONF_HIGH,
     "CATL supplies battery systems for Mercedes-Benz EQ-series electric vehicles"),
    (CATL, NIO,       _S, "global", CONF_HIGH,
     "CATL is NIO's primary battery cell supplier; acknowledged in NIO investor disclosures"),
    (CATL, LI_AUTO,   _S, "global", CONF_HIGH,
     "CATL supplies NMC and LFP battery cells to Li Auto for its EREV and BEV models"),
    (CATL, XPENG,     _S, "global", CONF_HIGH,
     "CATL is a battery cell supplier to XPeng; disclosed in XPeng annual reports"),

    # ── Lithium / materials → CATL ─────────────────────────────────────────
    (ALBEMARLE, CATL, _S, "global", CONF_HIGH,
     "Albemarle Corp supplies lithium hydroxide to CATL for EV battery production"),
    (SQM,       CATL, _S, "global", CONF_HIGH,
     "SQM (Sociedad Química y Minera de Chile) supplies lithium carbonate to CATL"),
    (GANFENG,   CATL, _S, "global", CONF_MEDIUM,
     "Jiangxi Ganfeng Lithium is a lithium raw-material supplier in CATL's supply chain"),

    # ── CATL commodity inputs ──────────────────────────────────────────────
    (LITHIUM,  CATL, _S, "global", CONF_HIGH,
     "Lithium is the primary critical mineral input for CATL's lithium-ion battery cells"),
    (NICKEL,   CATL, _S, "global", CONF_HIGH,
     "Nickel (as nickel sulfate) is a key cathode material for CATL's NMC battery cells"),
    (COBALT,   CATL, _S, "global", CONF_MEDIUM,
     "Cobalt is used in NMC cathode chemistries; CATL is reducing reliance via LFP shift"),
    (GRAPHITE, CATL, _S, "global", CONF_HIGH,
     "Graphite is the primary anode material in CATL's lithium-ion battery cells"),

    # ── Chinese EV competition ─────────────────────────────────────────────
    (NIO,     XPENG,    _C, "global", CONF_HIGH,
     "NIO and XPeng directly compete in China's premium and mid-range EV segments"),
    (NIO,     LI_AUTO,  _C, "global", CONF_HIGH,
     "NIO and Li Auto compete across premium EV segments in China"),
    (NIO,     BYD_AUTO, _C, "global", CONF_HIGH,
     "NIO and BYD compete across multiple EV price segments in China"),
    (NIO,     TESLA,    _C, "global", CONF_HIGH,
     "NIO competes with Tesla in China's premium EV market"),
    (LI_AUTO, XPENG,    _C, "global", CONF_HIGH,
     "Li Auto and XPeng are direct competitors in China's mid-premium EV market"),
    (LI_AUTO, BYD_AUTO, _C, "global", CONF_HIGH,
     "Li Auto and BYD Auto compete in China's EV market across overlapping segments"),
    (XPENG,   BYD_AUTO, _C, "global", CONF_HIGH,
     "XPeng and BYD compete in China's volume EV segments"),

    # ── GLP-1 pharma competition ───────────────────────────────────────────
    (NOVO_NORDISK, ELI_LILLY,  _C, "global", CONF_HIGH,
     "Novo Nordisk (Ozempic/Wegovy) and Eli Lilly (Mounjaro/Zepbound) are the two dominant "
     "GLP-1 agonist competitors in the global obesity and diabetes drug markets"),
    (NOVO_NORDISK, ASTRAZENECA, _C, "global", CONF_MEDIUM,
     "Novo Nordisk and AstraZeneca compete in cardiovascular and metabolic disease treatments"),
    (NOVO_NORDISK, ROCHE,       _C, "global", CONF_MEDIUM,
     "Novo Nordisk and Roche Holding compete across oncology and rare-disease pipelines"),

    # ── Roche competition ──────────────────────────────────────────────────
    (ROCHE, PFIZER,     _C, "global", CONF_HIGH,
     "Roche and Pfizer compete in oncology, immunology, and diagnostic markets globally"),
    (ROCHE, NOVARTIS,   _C, "global", CONF_HIGH,
     "Roche and Novartis are the two largest Swiss pharma companies; compete across oncology "
     "and specialty therapeutics"),
    (ROCHE, ASTRAZENECA, _C, "global", CONF_HIGH,
     "Roche and AstraZeneca compete directly in oncology (targeted therapies, immuno-oncology)"),

    # ── BMW competition & supply ───────────────────────────────────────────
    (BMW_GROUP, MERCEDES,  _C, "global", CONF_HIGH,
     "BMW Group and Mercedes-Benz Group are direct competitors in global premium automotive"),
    (BMW_GROUP, VW_GROUP,  _C, "global", CONF_HIGH,
     "BMW Group competes with Volkswagen Group's Audi and Porsche brands in premium segments"),
    (BMW_GROUP, TESLA,     _C, "global", CONF_HIGH,
     "BMW Group and Tesla compete in premium electric vehicle segments globally"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_nodes(cur: sqlite3.Cursor) -> list[str]:
    inserted = []
    for n in NEW_NODES:
        if cur.execute("SELECT 1 FROM nodes WHERE id=?", (n["id"],)).fetchone():
            print(f"  [SKIP  node] {n['id']}  ({n['name']})")
            continue
        cur.execute(
            """INSERT INTO nodes
               (id, type, name, aliases, tickers, identifiers,
                sector, industry, country, metadata)
               VALUES (:id,:type,:name,:aliases,:tickers,:identifiers,
                       :sector,:industry,:country,:metadata)""",
            n,
        )
        inserted.append(n["id"])
        print(f"  [INSERT node] {n['id']}  {n['name']}")
    return inserted


def _insert_aliases(cur: sqlite3.Cursor) -> int:
    count = 0
    for n in NEW_NODES:
        alias_list = json.loads(n["aliases"])
        for a in alias_list:
            norm = a.lower().strip()
            if cur.execute(
                "SELECT 1 FROM aliases WHERE alias_normalized=? AND node_id=?",
                (norm, n["id"]),
            ).fetchone():
                continue
            cur.execute(
                "INSERT INTO aliases (alias, alias_normalized, node_id) VALUES (?,?,?)",
                (a, norm, n["id"]),
            )
            count += 1
    return count


def _insert_edges(cur: sqlite3.Cursor) -> list[tuple]:
    inserted = []
    skipped_missing = []
    for source, target, etype, geo, conf, snippet in NEW_EDGES:
        # Skip if either endpoint doesn't exist (guards forward-references)
        if not cur.execute("SELECT 1 FROM nodes WHERE id=?", (source,)).fetchone():
            skipped_missing.append(f"source {source}")
            continue
        if not cur.execute("SELECT 1 FROM nodes WHERE id=?", (target,)).fetchone():
            skipped_missing.append(f"target {target}")
            continue
        if cur.execute(
            "SELECT 1 FROM edges WHERE source=? AND target=? AND type=?",
            (source, target, etype),
        ).fetchone():
            print(f"  [SKIP  edge] {source} --{etype}--> {target}")
            continue
        eid = str(uuid.uuid4())
        cur.execute(
            """INSERT INTO edges
               (id, source, target, type, directed, confidence,
                weight, prov_filing, prov_url, prov_snippet,
                prov_extracted_by, additional_provenance,
                below_threshold, supply_geography)
               VALUES (?,?,?,?,1,?,NULL,'','',?,?,'[]',0,?)""",
            (eid, source, target, etype, conf, snippet, EXTRACTED_BY, geo),
        )
        inserted.append((source, target, etype, geo))
        src_name = cur.execute("SELECT name FROM nodes WHERE id=?", (source,)).fetchone()[0]
        tgt_name = cur.execute("SELECT name FROM nodes WHERE id=?", (target,)).fetchone()[0]
        print(f"  [INSERT edge] {src_name} --{etype}--> {tgt_name} [{geo}]")
    if skipped_missing:
        for m in skipped_missing:
            print(f"  [WARN ] edge skipped — node not found: {m}")
    return inserted


def _summary(cur: sqlite3.Cursor, inserted_nodes, alias_count, inserted_edges):
    print()
    print("=" * 65)
    print("SUMMARY")
    print("=" * 65)
    print(f"  Nodes inserted  : {len(inserted_nodes)}")
    print(f"  Aliases inserted: {alias_count}")
    print(f"  Edges inserted  : {len(inserted_edges)}")
    print()
    print("Edge breakdown by type:")
    for etype in ("supplies", "competes_with"):
        cnt = sum(1 for _, _, t, _ in inserted_edges if t == etype)
        print(f"  {etype:20s}: {cnt}")
    print()
    print("New nodes with edge counts:")
    new_ids = {n["id"] for n in NEW_NODES}
    for nid in new_ids:
        name = cur.execute("SELECT name FROM nodes WHERE id=?", (nid,)).fetchone()
        if not name:
            continue
        out_ = cur.execute(
            "SELECT COUNT(*) FROM edges WHERE source=?", (nid,)
        ).fetchone()[0]
        in_  = cur.execute(
            "SELECT COUNT(*) FROM edges WHERE target=?", (nid,)
        ).fetchone()[0]
        print(f"  {name[0]:35s}  out={out_:3d}  in={in_:3d}")


def _write_to_jsonl(inserted_node_ids: set, inserted_edges: list[tuple]) -> None:
    """Append new nodes and edges to the JSONL pipeline files so they survive
    build_graph.py rebuilds (which wipe and reload econgraph.db from JSONL)."""

    # ── Nodes ────────────────────────────────────────────────────────────────
    if NODES_JSONL.exists():
        existing_node_ids: set[str] = set()
        with open(NODES_JSONL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    existing_node_ids.add(json.loads(line)["id"])
    else:
        existing_node_ids = set()

    appended_nodes = 0
    with open(NODES_JSONL, "a", encoding="utf-8") as f:
        for n in NEW_NODES:
            if n["id"] in existing_node_ids:
                continue
            if n["id"] not in inserted_node_ids:
                continue   # wasn't inserted into DB (already existed) — skip
            # Convert DB-format JSON strings back to Python objects for JSONL
            record = {
                "id":          n["id"],
                "type":        n["type"],
                "name":        n["name"],
                "aliases":     json.loads(n["aliases"]),
                "tickers":     json.loads(n["tickers"]),
                "identifiers": json.loads(n["identifiers"]),
                "sector":      n["sector"],
                "industry":    n["industry"],
                "country":     n["country"],
                "metadata":    json.loads(n["metadata"]),
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            appended_nodes += 1
    print(f"  {appended_nodes} nodes appended to {NODES_JSONL.name}")

    # ── Edges ─────────────────────────────────────────────────────────────────
    if EDGES_JSONL.exists():
        existing_edge_keys: set[tuple] = set()
        with open(EDGES_JSONL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    e = json.loads(line)
                    existing_edge_keys.add((e["source"], e["target"], e["type"]))
    else:
        existing_edge_keys = set()

    # Build a lookup: (source, target, type) -> (conf, snippet, directed)
    edge_meta: dict[tuple, tuple] = {}
    for source, target, etype, geo, conf, snippet in NEW_EDGES:
        key = (source, target, etype)
        edge_meta[key] = (conf, snippet, etype != "competes_with")

    appended_edges = 0
    with open(EDGES_JSONL, "a", encoding="utf-8") as f:
        for source, target, etype, geo in inserted_edges:
            key = (source, target, etype)
            if key in existing_edge_keys:
                continue
            conf, snippet, directed = edge_meta.get(key, (CONF_MEDIUM, "", True))
            record = {
                "id":       str(uuid.uuid4()),
                "source":   source,
                "target":   target,
                "type":     etype,
                "directed": directed,
                "confidence": conf,
                "provenance": {
                    "filing":       "",
                    "url":          "",
                    "snippet":      snippet,
                    "extracted_by": EXTRACTED_BY,
                },
                "additional_provenance": [],
                "supply_geography": geo,
            }
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            appended_edges += 1
    print(f"  {appended_edges} edges appended to {EDGES_JSONL.name}")

    # ── Aliases ───────────────────────────────────────────────────────────────
    if ALIASES_JSONL.exists():
        existing_alias_keys: set[tuple] = set()
        with open(ALIASES_JSONL, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    existing_alias_keys.add((r["alias_normalized"], r["canonical_id"]))
    else:
        existing_alias_keys = set()

    appended_aliases = 0
    with open(ALIASES_JSONL, "a", encoding="utf-8") as f:
        for n in NEW_NODES:
            if n["id"] not in inserted_node_ids:
                continue
            alias_list = json.loads(n["aliases"])
            for alias in alias_list:
                norm = alias.lower().strip()
                key = (norm, n["id"])
                if key in existing_alias_keys:
                    continue
                record = {
                    "alias":            alias,
                    "alias_normalized": norm,
                    "canonical_id":     n["id"],
                    "source":           EXTRACTED_BY,
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                existing_alias_keys.add(key)
                appended_aliases += 1
    print(f"  {appended_aliases} aliases appended to {ALIASES_JSONL.name}")


def main():
    print(f"Database: {DB_PATH}")
    if not DB_PATH.exists():
        raise FileNotFoundError(DB_PATH)

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA foreign_keys = OFF")   # aliases FK is soft; keep OFF for safety
    cur = conn.cursor()

    try:
        print("\n--- Inserting nodes ---")
        inserted_nodes = _insert_nodes(cur)

        print("\n--- Inserting aliases ---")
        alias_count = _insert_aliases(cur)
        print(f"  {alias_count} alias rows inserted")

        print("\n--- Inserting edges ---")
        inserted_edges = _insert_edges(cur)

        conn.commit()
        _summary(cur, inserted_nodes, alias_count, inserted_edges)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    print("\n--- Persisting to JSONL pipeline files ---")
    _write_to_jsonl(set(inserted_nodes), inserted_edges)
    print("\nDone. Re-run `python -m pipeline.build_graph` to refresh graph.json.")


if __name__ == "__main__":
    main()
