"""Phase B: Wikidata SPARQL enrichment for wikidata:-id companies.

Queries the Wikidata SPARQL endpoint for P:1830 (competitor) relations
for all Phase B companies that have wikidata:Q... canonical IDs.

Writes verified CandidateEdge records directly to data/edges_raw.jsonl.

Invariants:
  - CLAUDE.md #3: Every edge has provenance (filing=wikidata:<QID>, url=entity URL)
  - CLAUDE.md #7: Rate-limited to ≤1 request/second to Wikidata SPARQL
  - Idempotent: check for existing wikidata: edges in edges_raw before adding
  - Only adds competes_with edges (P:1830); supply-chain from Wikipedia extraction
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

import requests

from schema.models import CandidateEdge, EdgeType, Provenance


log = logging.getLogger("wikidata_phase_b")

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"
WIKIDATA_ENTITY = "https://www.wikidata.org/wiki/"
SPARQL_CHUNK = 50   # conservative for Phase B (686 companies / 50 = 14 queries)
WIKIDATA_CONFIDENCE = 0.85
_MIN_INTERVAL = 1.1  # ≤1 req/s — slight buffer


def _ua() -> str:
    import os
    ua = os.environ.get("EDGAR_USER_AGENT", "EconGraph/1.0 (kondaru.mk@gmail.com)")
    return f"{ua} (EconGraph Wikidata Phase B enricher)"


def _sparql(query: str) -> list[dict[str, Any]]:
    """Execute SPARQL query and return bindings. Rate-limited to ≤1 req/s."""
    time.sleep(_MIN_INTERVAL)
    resp = requests.get(
        WIKIDATA_SPARQL,
        params={"query": query, "format": "json"},
        headers={
            "User-Agent": _ua(),
            "Accept": "application/sparql-results+json",
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json().get("results", {}).get("bindings", [])


def _qid(url: str) -> Optional[str]:
    m = re.search(r"/(Q\d+)$", url)
    return m.group(1) if m else None


def fetch_p1830_competitors(qids: list[str]) -> list[dict[str, Any]]:
    """Fetch P:1830 (competitor of) for a batch of QIDs."""
    rows: list[dict[str, Any]] = []
    for start in range(0, len(qids), SPARQL_CHUNK):
        chunk = qids[start:start + SPARQL_CHUNK]
        values = " ".join(f"wd:{q}" for q in chunk)
        query = f"""
        SELECT ?company ?companyLabel ?competitor ?competitorLabel WHERE {{
          VALUES ?company {{ {values} }}
          ?company wdt:P1830 ?competitor .
          FILTER(?company != ?competitor)
          SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
        }}
        """
        try:
            bindings = _sparql(query)
            for row in bindings:
                src = _qid(row.get("company", {}).get("value", ""))
                tgt = _qid(row.get("competitor", {}).get("value", ""))
                if src and tgt and src != tgt:
                    rows.append({
                        "source_qid": src,
                        "source_label": row.get("companyLabel", {}).get("value", src),
                        "target_qid": tgt,
                        "target_label": row.get("competitorLabel", {}).get("value", tgt),
                    })
            log.info(
                "P:1830 chunk %d/%d: %d rows",
                start // SPARQL_CHUNK + 1,
                (len(qids) + SPARQL_CHUNK - 1) // SPARQL_CHUNK,
                len(bindings),
            )
        except Exception as exc:
            log.warning("P:1830 SPARQL failed for chunk at %d: %s", start, exc)
    return rows


def run(*, data_root: Path) -> dict[str, Any]:
    companies_path = data_root / "companies.jsonl"
    edges_raw_path = data_root / "edges_raw.jsonl"

    # Load Phase B companies (wikidata: ids)
    phase_b: list[dict[str, Any]] = []
    for line in companies_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if obj.get("id", "").startswith("wikidata:"):
                phase_b.append(obj)
        except Exception:
            continue
    log.info("Phase B companies to enrich: %d", len(phase_b))

    # Build QID → canonical ID map
    qid_to_id: dict[str, str] = {}
    for node in phase_b:
        cid = node["id"]
        qid = cid[len("wikidata:"):]
        qid_to_id[qid] = cid

    qids = list(qid_to_id.keys())

    # Check for already-existing wikidata P:1830 edges to avoid duplicates
    existing_pairs: set[tuple[str, str]] = set()
    if edges_raw_path.exists():
        for line in edges_raw_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ce = json.loads(line)
                prov = ce.get("provenance", {})
                if "wikidata" in (prov.get("extracted_by") or ""):
                    # Already a wikidata-extracted edge
                    existing_pairs.add((ce.get("source_id", ""), ce.get("target_raw", "")))
            except Exception:
                continue
    log.info("Existing wikidata edge pairs (dedup): %d", len(existing_pairs))

    # Fetch P:1830 competitor relations
    log.info("Querying Wikidata SPARQL for P:1830 (competitor) relations …")
    competitor_rows = fetch_p1830_competitors(qids)
    log.info("Raw P:1830 rows: %d", len(competitor_rows))

    # Build CandidateEdge records
    candidates: list[CandidateEdge] = []
    skipped = 0
    for row in competitor_rows:
        src_qid = row["source_qid"]
        tgt_qid = row["target_qid"]
        src_id = qid_to_id.get(src_qid)
        if not src_id:
            # Source company not in our Phase B set — skip
            skipped += 1
            continue
        tgt_label = row["target_label"] or f"wikidata:{tgt_qid}"
        # Use canonical wikidata: ID if target is a Phase B node, else raw label
        tgt_raw = qid_to_id.get(tgt_qid, tgt_label)

        pair = (src_id, tgt_raw)
        if pair in existing_pairs:
            skipped += 1
            continue

        snippet = (
            f"Wikidata P:1830 (competitor of): "
            f"{row['source_label']} competes with {tgt_label} ({tgt_qid})"
        )
        try:
            ce = CandidateEdge(
                source_id=src_id,
                target_raw=tgt_raw,
                type=EdgeType.competes_with,
                confidence=WIKIDATA_CONFIDENCE,
                provenance=Provenance(
                    filing=f"wikidata:{src_qid}",
                    url=WIKIDATA_ENTITY + src_qid,
                    snippet=snippet,
                    extracted_by="wikidata",
                ),
                verified=True,
            )
            candidates.append(ce)
            existing_pairs.add(pair)
        except Exception as exc:
            log.warning("CandidateEdge build failed %s → %s: %s", src_id, tgt_raw, exc)
            skipped += 1

    # Append to edges_raw.jsonl
    n_written = 0
    if candidates:
        edges_raw_path.parent.mkdir(parents=True, exist_ok=True)
        with edges_raw_path.open("a", encoding="utf-8") as fh:
            for ce in candidates:
                fh.write(ce.model_dump_json())
                fh.write("\n")
                n_written += 1

    log.info(
        "Wikidata Phase B enrichment complete: %d competes_with edges written, %d skipped",
        n_written, skipped,
    )
    return {
        "phase_b_companies": len(phase_b),
        "p1830_rows_raw": len(competitor_rows),
        "edges_written": n_written,
        "skipped": skipped,
    }


def main() -> int:
    import argparse
    import logging as _logging

    parser = argparse.ArgumentParser(description="EconGraph Phase B — Wikidata SPARQL enrichment")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    _logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    result = run(data_root=Path(args.data_root))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
