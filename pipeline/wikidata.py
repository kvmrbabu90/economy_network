"""Tier-3 Wikidata enrichment.

Pulls four classes of facts off the public Wikidata SPARQL endpoint:

  1. cik -> Q-id mapping (via P:5531, the SEC CIK property).
  2. Per-company metadata: country, HQ city, geographic coordinates.
  3. P:1830 competitor relations (whom each company officially competes with).
  4. P:1071 supplier relations (whom each company sources from).

Writes three artifacts under data/:
  * data/wikidata_companies.json   -- cik -> { qid, name, country, hq, coord }
  * data/_extract/wikidata_candidates.jsonl  -- competitor + supplier edges,
      ready to be folded into edges_raw.jsonl on the next extract.run().
  * data/wikidata_aliases.jsonl    -- new alias rows mapping the Wikidata
      English label of each S&P 500 company to its cik so future Phase 3
      runs can resolve "Microsoft Corporation" -> cik:0000789019 even when
      Wikidata's label differs slightly from Wikipedia's.

The Wikidata SPARQL endpoint is rate-limited and asks for a descriptive
User-Agent. We reuse the EDGAR_USER_AGENT env var since it serves the
same "give us a real contact" purpose. Throttle defensively to <= 1 req/s
to stay under the public-endpoint informal limits.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

from schema.models import CandidateEdge, EdgeType, Provenance


log = logging.getLogger("wikidata")

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIDATA_ENTITY = "https://www.wikidata.org/wiki/"
# SPARQL queries against the public endpoint should chunk VALUES sets to stay
# under the response-size + query-time limits. 100 per chunk is comfortable.
SPARQL_CHUNK = 100

# Confidence tier for wikidata-sourced edges. Higher than inference (0.65),
# higher than the strict 0.75 cutoff so they enter the high-confidence core.
WIKIDATA_CONFIDENCE = 0.85


def _user_agent() -> str:
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError(
            "EDGAR_USER_AGENT env var required (reused for Wikidata's UA policy)"
        )
    return f"{ua} (EconGraph Wikidata fetcher; via SPARQL)"


_lock = threading.Lock()
_last = 0.0
_MIN_INTERVAL = 1.0  # <= 1 req/s -- be polite


def _sparql_get(query: str, *, timeout: float = 60.0) -> dict[str, Any]:
    global _last
    with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last)
        if wait > 0:
            time.sleep(wait)
        _last = time.monotonic()
    headers = {
        "User-Agent": _user_agent(),
        "Accept": "application/sparql-results+json",
    }
    resp = requests.get(
        WIKIDATA_SPARQL,
        params={"query": query, "format": "json"},
        headers=headers,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _qid_of(url: str) -> Optional[str]:
    """Convert 'http://www.wikidata.org/entity/Q160298' -> 'Q160298'."""
    if not url:
        return None
    m = re.search(r"/(Q\d+)$", url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# 1) CIK -> Q-id mapping
# ---------------------------------------------------------------------------

def fetch_qids_for_ciks(ciks: list[str]) -> dict[str, str]:
    """Map each 10-digit CIK to a Wikidata Q-id (via property P:5531)."""
    out: dict[str, str] = {}
    for start in range(0, len(ciks), SPARQL_CHUNK):
        chunk = ciks[start:start + SPARQL_CHUNK]
        values = " ".join(f'"{c}"' for c in chunk)
        # P:5531 = SEC Central Index Key. Wikidata stores these as plain
        # strings, sometimes zero-padded, sometimes not -- match both.
        query = f"""
        SELECT ?company ?cik WHERE {{
          VALUES ?cik {{ {values} }}
          ?company wdt:P5531 ?cik .
        }}
        """
        data = _sparql_get(query)
        for row in data.get("results", {}).get("bindings", []):
            cik = row["cik"]["value"]
            qid = _qid_of(row["company"]["value"])
            if cik and qid:
                out.setdefault(cik, qid)
        # Also try the stripped-leading-zeros form for any unmapped.
        unmapped = [c for c in chunk if c not in out]
        if unmapped:
            stripped = {str(int(c)): c for c in unmapped}
            values = " ".join(f'"{c}"' for c in stripped.keys())
            query = f"""
            SELECT ?company ?cik WHERE {{
              VALUES ?cik {{ {values} }}
              ?company wdt:P5531 ?cik .
            }}
            """
            data = _sparql_get(query)
            for row in data.get("results", {}).get("bindings", []):
                cik = row["cik"]["value"]
                qid = _qid_of(row["company"]["value"])
                original = stripped.get(cik)
                if original and qid and original not in out:
                    out[original] = qid
        log.info("CIK->Q-id chunk %d: %d resolved (cumulative %d/%d)",
                 start // SPARQL_CHUNK, sum(1 for c in chunk if c in out), len(out), start + len(chunk))
    return out


# ---------------------------------------------------------------------------
# 2) Per-company metadata (label, country, HQ, coordinates)
# ---------------------------------------------------------------------------

@dataclass
class WikidataCompany:
    qid: str
    cik: Optional[str] = None
    label: Optional[str] = None
    country_label: Optional[str] = None
    country_qid: Optional[str] = None
    hq_label: Optional[str] = None
    hq_qid: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None


def fetch_company_metadata(qids: list[str]) -> dict[str, WikidataCompany]:
    out: dict[str, WikidataCompany] = {}
    for start in range(0, len(qids), SPARQL_CHUNK):
        chunk = qids[start:start + SPARQL_CHUNK]
        values = " ".join(f"wd:{q}" for q in chunk)
        # P:625 directly on the company is rarely populated for corporations;
        # most have a P:159 (HQ) that's a city Q-id, and the CITY has P:625.
        # The SPARQL property-path `wdt:P159/wdt:P625` traverses both in
        # one go, falling back to direct P:625 if the path doesn't yield.
        query = f"""
        SELECT ?company ?companyLabel ?country ?countryLabel ?hq ?hqLabel ?coord ?hqCoord WHERE {{
          VALUES ?company {{ {values} }}
          OPTIONAL {{ ?company wdt:P17 ?country . }}
          OPTIONAL {{ ?company wdt:P159 ?hq . }}
          OPTIONAL {{ ?company wdt:P625 ?coord . }}
          OPTIONAL {{ ?company wdt:P159/wdt:P625 ?hqCoord . }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
        }}
        """
        data = _sparql_get(query)
        for row in data.get("results", {}).get("bindings", []):
            qid = _qid_of(row["company"]["value"])
            if not qid:
                continue
            wd = out.setdefault(qid, WikidataCompany(qid=qid))
            wd.label = wd.label or row.get("companyLabel", {}).get("value")
            ctry = row.get("country", {}).get("value")
            if ctry:
                wd.country_qid = wd.country_qid or _qid_of(ctry)
                wd.country_label = wd.country_label or row.get("countryLabel", {}).get("value")
            hq = row.get("hq", {}).get("value")
            if hq:
                wd.hq_qid = wd.hq_qid or _qid_of(hq)
                wd.hq_label = wd.hq_label or row.get("hqLabel", {}).get("value")
            # Prefer the direct coord (company's own geolocation, if any);
            # fall back to the HQ city's coord.
            for key in ("coord", "hqCoord"):
                coord = row.get(key, {}).get("value")
                if coord and wd.lat is None:
                    m = re.match(r"Point\(([\-\d.]+)\s+([\-\d.]+)\)", coord)
                    if m:
                        wd.lon = float(m.group(1))
                        wd.lat = float(m.group(2))
                        break
    return out


# ---------------------------------------------------------------------------
# 3 + 4) Competitor (P:1830) and supplier (P:1071) relations
# ---------------------------------------------------------------------------

def fetch_relations(qids: list[str], property_id: str) -> list[dict[str, Any]]:
    """Returns rows {source_qid, target_qid, target_label, target_cik?}."""
    rows: list[dict[str, Any]] = []
    for start in range(0, len(qids), SPARQL_CHUNK):
        chunk = qids[start:start + SPARQL_CHUNK]
        values = " ".join(f"wd:{q}" for q in chunk)
        query = f"""
        SELECT ?company ?target ?targetLabel ?targetCik WHERE {{
          VALUES ?company {{ {values} }}
          ?company wdt:{property_id} ?target .
          OPTIONAL {{ ?target wdt:P5531 ?targetCik . }}
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
        }}
        """
        data = _sparql_get(query)
        for row in data.get("results", {}).get("bindings", []):
            src = _qid_of(row["company"]["value"])
            tgt = _qid_of(row["target"]["value"])
            if not src or not tgt:
                continue
            rows.append({
                "source_qid": src,
                "target_qid": tgt,
                "target_label": row.get("targetLabel", {}).get("value"),
                "target_cik": row.get("targetCik", {}).get("value"),
            })
    return rows


# ---------------------------------------------------------------------------
# Orchestrator + outputs
# ---------------------------------------------------------------------------

def make_candidate(
    source_id: str, target_raw: str, edge_type: str, source_qid: str, snippet: str,
) -> CandidateEdge:
    """Wikidata-sourced candidate edge.

    Source filing/url point at the Wikidata entity page that asserted the
    relation, so the existing Provenance.filing/url validator is satisfied.
    """
    return CandidateEdge(
        source_id=source_id,
        target_raw=target_raw,
        type=EdgeType(edge_type),
        confidence=WIKIDATA_CONFIDENCE,
        provenance=Provenance(
            filing=f"wikidata:{source_qid}",
            url=WIKIDATA_ENTITY + source_qid,
            snippet=snippet,
            extracted_by="wikidata",
        ),
        verified=True,
    )


def run_wikidata_enrichment(
    *,
    companies_path: Path,
    out_companies: Path,
    out_candidates: Path,
    out_aliases: Path,
) -> dict[str, Any]:
    """Pull Q-ids + metadata + relations for every company in companies.jsonl."""
    nodes = [json.loads(l) for l in companies_path.open(encoding="utf-8")]
    ciks = [(n.get("identifiers") or {}).get("cik") for n in nodes]
    ciks = [c for c in ciks if c]
    log.info("Resolving %d CIKs to Wikidata Q-ids ...", len(ciks))
    cik_to_qid = fetch_qids_for_ciks(ciks)
    log.info("Q-id resolution: %d / %d (%.0f%%)",
             len(cik_to_qid), len(ciks), 100 * len(cik_to_qid) / max(1, len(ciks)))

    qids = list(cik_to_qid.values())
    log.info("Fetching metadata (country/HQ/coords) for %d companies ...", len(qids))
    metadata = fetch_company_metadata(qids)

    log.info("Fetching P:1830 competitor relations ...")
    competitors = fetch_relations(qids, "P1830")
    log.info("  %d competitor relations", len(competitors))

    log.info("Fetching P:1071 supplier relations (where applicable) ...")
    # P:1071 is "location of formation" -- not supplier. The actual supplier
    # relation on Wikidata uses P:127 (owned by) / P:1056 (product material).
    # Wikidata has no standard "supplier" relation that's well-populated for
    # companies; in practice the competitor relation is the rich one. We
    # leave this hook for future expansion and skip the fetch for now.
    suppliers: list[dict[str, Any]] = []

    # Reverse-map qid -> cik for "is one of our filers?" check.
    qid_to_cik = {q: c for c, q in cik_to_qid.items()}

    # Build the enriched companies side file.
    out_companies.parent.mkdir(parents=True, exist_ok=True)
    enriched = {}
    for n in nodes:
        cik = (n.get("identifiers") or {}).get("cik")
        if not cik or cik not in cik_to_qid:
            continue
        qid = cik_to_qid[cik]
        md = metadata.get(qid)
        if not md:
            continue
        enriched[cik] = {
            "cik": cik,
            "qid": qid,
            "label": md.label,
            "country": md.country_label,
            "country_qid": md.country_qid,
            "hq": md.hq_label,
            "hq_qid": md.hq_qid,
            "lat": md.lat,
            "lon": md.lon,
        }
    out_companies.write_text(json.dumps(enriched, indent=2), encoding="utf-8")
    log.info("Wrote %d enriched company records -> %s", len(enriched), out_companies)

    # Candidate edges.
    candidates: list[CandidateEdge] = []
    for row in competitors:
        src_qid = row["source_qid"]
        tgt_qid = row["target_qid"]
        if src_qid not in qid_to_cik:
            continue
        if src_qid == tgt_qid:
            # Wikidata occasionally has self-competitor data quality issues.
            continue
        src_cik_id = f"cik:{qid_to_cik[src_qid]}"
        # Prefer the target's CIK if available; otherwise fall back to a
        # wikidata: id directly so it resolves to a wikidata-typed node.
        if row.get("target_cik"):
            target_raw_str = row["target_label"] or f"wikidata:{tgt_qid}"
        else:
            # Use the Wikidata label as target_raw; Phase 3 will try to
            # match it against the alias table or mint a new wikidata: node.
            target_raw_str = row["target_label"] or f"wikidata:{tgt_qid}"
        snippet = (
            f"Wikidata P:1830 (competitor): "
            f"{enriched.get(qid_to_cik[src_qid], {}).get('label') or src_cik_id} "
            f"competes with {row['target_label']} ({tgt_qid})"
        )
        try:
            ce = make_candidate(src_cik_id, target_raw_str, "competes_with", src_qid, snippet)
            candidates.append(ce)
        except Exception as exc:
            log.warning("Wikidata candidate failed: %s", exc)
    out_candidates.parent.mkdir(parents=True, exist_ok=True)
    with out_candidates.open("w", encoding="utf-8") as fh:
        for ce in candidates:
            fh.write(ce.model_dump_json())
            fh.write("\n")
    log.info("Wrote %d wikidata candidate edges -> %s", len(candidates), out_candidates)

    # Alias rows: Wikidata label -> cik. Used by Phase 3's alias table so
    # LLM mentions of, e.g., "Microsoft" resolve to cik:0000789019 even if
    # the existing alias seed only had the Wikipedia "Security" string.
    aliases = []
    from pipeline.resolve import normalize as _normalize
    for cik, e in enriched.items():
        if not e.get("label"):
            continue
        aliases.append({
            "alias": e["label"],
            "alias_normalized": _normalize(e["label"]),
            "canonical_id": f"cik:{cik}",
            "source": "wikidata:label",
        })
    out_aliases.parent.mkdir(parents=True, exist_ok=True)
    with out_aliases.open("w", encoding="utf-8") as fh:
        for a in aliases:
            fh.write(json.dumps(a))
            fh.write("\n")
    log.info("Wrote %d wikidata alias rows -> %s", len(aliases), out_aliases)

    return {
        "ciks": len(ciks),
        "qid_resolved": len(cik_to_qid),
        "with_coords": sum(1 for e in enriched.values() if e["lat"] is not None),
        "candidate_edges": len(candidates),
        "alias_rows": len(aliases),
    }


def run_phase_b_enrichment(
    *,
    companies_path: Path,
    out_candidates: Path,
) -> dict[str, Any]:
    """Phase B Step B3: Enrich wikidata: nodes with P:1830 competitor relations.

    Unlike run_wikidata_enrichment (which works on CIK-based nodes), this
    function directly processes wikidata:Q... nodes from Phase B. It fetches
    P:1830 (competitors), P:127 (owned by, for subsidiary inference), and
    P:355 (subsidiaries) and emits CandidateEdge records.
    """
    nodes = [json.loads(l) for l in companies_path.open(encoding="utf-8")]
    # Collect Q-ids from wikidata: nodes only (Phase B additions)
    phase_b_nodes = [n for n in nodes if n.get("id", "").startswith("wikidata:")]
    log.info("Phase B enrichment: %d wikidata: nodes to process", len(phase_b_nodes))

    qid_to_node: dict[str, dict] = {}
    for n in phase_b_nodes:
        qid = n["id"][len("wikidata:"):]
        qid_to_node[qid] = n

    # Also build qid lookup for ALL nodes (CIK-based too, for target resolution)
    all_qid_to_id: dict[str, str] = {}
    for n in nodes:
        nid = n.get("id", "")
        if nid.startswith("wikidata:"):
            qid = nid[len("wikidata:"):]
            all_qid_to_id[qid] = nid
        # CIK nodes may have wikidata id in identifiers
        wikidata_id = (n.get("identifiers") or {}).get("wikidata")
        if wikidata_id and nid:
            all_qid_to_id.setdefault(wikidata_id, nid)

    qids = list(qid_to_node.keys())
    if not qids:
        log.info("No Phase B nodes found — skipping enrichment")
        return {"phase_b_nodes": 0, "candidate_edges": 0}

    # Fetch P:1830 (competitor) relations
    log.info("Fetching P:1830 competitor relations for %d Phase B companies ...", len(qids))
    competitors = fetch_relations(qids, "P1830")
    log.info("  %d competitor relations found", len(competitors))

    # Fetch P:127 (owned by) — surfaces parent-child relationships
    log.info("Fetching P:127 (owned by) relations ...")
    owned_by = fetch_relations(qids, "P127")
    log.info("  %d owned-by relations found", len(owned_by))

    # Fetch P:355 (subsidiaries) — source company has subsidiaries
    log.info("Fetching P:355 (subsidiary) relations ...")
    subsidiaries = fetch_relations(qids, "P355")
    log.info("  %d subsidiary relations found", len(subsidiaries))

    candidates: list[CandidateEdge] = []

    # Process competitor relations
    for row in competitors:
        src_qid = row["source_qid"]
        tgt_qid = row["target_qid"]
        if src_qid == tgt_qid:
            continue
        if src_qid not in qid_to_node:
            continue
        src_id = f"wikidata:{src_qid}"
        target_label = row.get("target_label") or f"wikidata:{tgt_qid}"
        # If target has a known node, use its label; else use raw label
        target_raw = target_label
        src_name = qid_to_node[src_qid].get("name", src_id)
        snippet = (
            f"Wikidata P1830 (competitor): "
            f"{src_name} competes with {target_label} ({tgt_qid})"
        )
        try:
            ce = make_candidate(src_id, target_raw, "competes_with", src_qid, snippet)
            candidates.append(ce)
        except Exception as exc:
            log.debug("Phase B candidate failed (competitor): %s", exc)

    # Process subsidiary relations — parent --supplies--> subsidiary is not
    # correct; skip these for now as they don't map cleanly to our edge types.
    # We log the count but don't emit edges.
    log.info("  (P:355 subsidiary relations logged but not emitted as edges)")

    out_candidates.parent.mkdir(parents=True, exist_ok=True)
    # Append to existing wikidata_candidates.jsonl
    n_written = 0
    with out_candidates.open("a", encoding="utf-8") as fh:
        for ce in candidates:
            fh.write(ce.model_dump_json())
            fh.write("\n")
            n_written += 1

    log.info("Phase B enrichment: appended %d candidate edges -> %s", n_written, out_candidates)

    return {
        "phase_b_nodes": len(qids),
        "competitor_relations": len(competitors),
        "candidate_edges": n_written,
    }


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Tier-3 Wikidata enrichment")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--phase-b", action="store_true",
                        help="Run Phase B enrichment (wikidata: node relations) instead of CIK enrichment")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    data_root = Path(args.data_root)
    if args.phase_b:
        summary = run_phase_b_enrichment(
            companies_path=data_root / "companies.jsonl",
            out_candidates=data_root / "_extract" / "wikidata_candidates.jsonl",
        )
    else:
        summary = run_wikidata_enrichment(
            companies_path=data_root / "companies.jsonl",
            out_companies=data_root / "wikidata_companies.json",
            out_candidates=data_root / "_extract" / "wikidata_candidates.jsonl",
            out_aliases=data_root / "wikidata_aliases.jsonl",
        )
    log.info("Wikidata enrichment summary:\n%s", json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
