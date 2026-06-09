"""Phase K-3: Extract financial weights from cached 10-K filings for hub nodes.

For each hub in data/hubs.jsonl that has a CIK and a cached 10-K, this stage:
  1. Extracts the Risk Factors and MD&A sections from the cached .txt file
  2. Uses Claude CLI to find all explicitly named customer/supplier concentrations
  3. Matches extracted entity names to existing graph nodes via fuzzy alias lookup
  4. Writes weight, source_tier=sec_explicit, confidence=0.90 back to edges.jsonl
  5. Creates NEW supply edges (with UUID) when a concentration is found for a
     graph node that has no existing edge in edges.jsonl.

Run AFTER score_confidence.py (which handles snippet-level extraction) and
BEFORE build_graph.py (or sync_weights_to_db.py for live updates).

Usage:
    python -m pipeline.extract_weights [--dry-run] [--max-hubs N]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = REPO_ROOT / "data"
FILINGS_DIR = DATA_DIR / "filings"

PARALLELISM = int(os.environ.get("WEIGHT_PARALLELISM", "4"))
LLM_TIMEOUT = int(os.environ.get("WEIGHT_LLM_TIMEOUT", "90"))

# ---------------------------------------------------------------------------
# Claude CLI helper (mirrors api/news.py pattern)
# ---------------------------------------------------------------------------

_CLAUDE_BIN_CACHE: str | None = None


def _resolve_claude_binary() -> str:
    global _CLAUDE_BIN_CACHE
    if _CLAUDE_BIN_CACHE:
        return _CLAUDE_BIN_CACHE
    candidates = [
        os.environ.get("CLAUDE_CLI"),
        str(Path.home() / ".local" / "bin" / "claude.exe"),
        shutil.which("claude.exe"),
        shutil.which("claude"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            _CLAUDE_BIN_CACHE = c
            return c
    raise RuntimeError("Could not find `claude` CLI.")


def _claude_call(prompt: str, timeout: int = LLM_TIMEOUT) -> str:
    binary = _resolve_claude_binary()
    cmd = [binary, "-p", prompt, "--output-format", "json"]
    try:
        # ignore_cleanup_errors avoids Windows [WinError 32] when Claude CLI
        # keeps handles open after the subprocess exits.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=tmpdir,
            )
    except subprocess.TimeoutExpired:
        log.warning("Claude CLI timed out after %ds", timeout)
        return ""
    if proc.returncode != 0:
        log.warning("Claude CLI error (exit %d): %s", proc.returncode,
                    (proc.stderr or "")[:200])
        return ""
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ""
    if envelope.get("is_error"):
        return ""
    return envelope.get("result", "") or ""


# ---------------------------------------------------------------------------
# 10-K section extraction
# ---------------------------------------------------------------------------

# Section header patterns (case-insensitive)
_SECTION_PATTERNS = [
    # Item 1A Risk Factors
    re.compile(r"item\s+1a[\.\s]*risk\s+factors", re.IGNORECASE),
    # Item 7 MD&A
    re.compile(r"item\s+7[\.\s]*management.{0,30}discussion", re.IGNORECASE),
    # Concentration of risk / Significant customers
    re.compile(r"concentrat(?:ion|ions)\s+of\s+(?:credit\s+)?risk", re.IGNORECASE),
    re.compile(r"significant\s+customers?", re.IGNORECASE),
    re.compile(r"major\s+customers?", re.IGNORECASE),
]

_NEXT_SECTION = re.compile(
    r"item\s+(?:1b|2|3|4|5|6|7a|8|9|10|11|12|13|14|15)\b", re.IGNORECASE
)


def _extract_filing_sections(text: str, max_chars_per_section: int = 8000) -> list[str]:
    """Extract customer-concentration-relevant sections from 10-K text."""
    sections = []
    for pattern in _SECTION_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        start = m.start()
        # Find the end of this section (next item header or 8k chars)
        end_match = _NEXT_SECTION.search(text, start + 50)
        end = end_match.start() if end_match else start + max_chars_per_section
        end = min(end, start + max_chars_per_section)
        chunk = text[start:end].strip()
        if len(chunk) > 200:  # ignore tiny/empty sections
            sections.append(chunk)
    return sections


# ---------------------------------------------------------------------------
# LLM extraction prompt
# ---------------------------------------------------------------------------

_EXTRACTION_PROMPT = """\
You are extracting financial concentration data from an SEC 10-K or 20-F filing.

Find all explicitly stated NAMED customer or supplier concentration percentages.
Rules:
- Only include percentages that are EXPLICITLY stated in the text.
- The company name must be explicitly written — NOT "our largest customer" alone.
- Do not estimate, infer, or combine numbers.
- If two percentages are given for different years, use the most recent one.
- Ignore percentages that refer to product mix, geography, or operating ratios.

Return ONLY a valid JSON array — no markdown, no other text:
[{{"entity": "<company name as written in text>", "type": "customer|supplier", "pct": <float>, "quote": "<verbatim excerpt ≤25 words>"}}]

If nothing qualifies, return [].

Filing excerpt:
{text}
"""


def _extract_concentrations_llm(text: str) -> list[dict]:
    """Call Claude CLI to extract named entity + % concentration from text."""
    prompt = _EXTRACTION_PROMPT.format(text=text[:6000])  # safety truncation
    raw = _claude_call(prompt)
    if not raw:
        return []
    # Strip markdown fences if present
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        raw = "\n".join(lines[1:])
    if raw.endswith("```"):
        raw = "\n".join(raw.splitlines()[:-1])
    raw = raw.strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.debug("JSON parse failed for LLM response: %s", raw[:200])
        return []
    if not isinstance(parsed, list):
        return []
    results = []
    for item in parsed:
        if isinstance(item, dict) and item.get("entity") and item.get("pct"):
            try:
                pct = float(item["pct"])
                if 5.0 <= pct <= 99.0:
                    results.append({
                        "entity": str(item["entity"]).strip(),
                        "type": str(item.get("type", "customer")).lower(),
                        "pct": pct,
                        "quote": str(item.get("quote", "")).strip()[:200],
                    })
            except (ValueError, TypeError):
                pass
    return results


# ---------------------------------------------------------------------------
# Alias lookup
# ---------------------------------------------------------------------------

def _build_alias_map(nodes_path: Path) -> dict[str, str]:
    """Build entity_name_lower -> node_id lookup from nodes.jsonl aliases.

    CIK nodes (SEC filers) take precedence over wikidata/slug nodes for the
    same name so that "Apple" → cik:0000320193 (Apple Inc) rather than
    wikidata:QXX (Apple International or another subsidiary).
    """
    alias_map: dict[str, str] = {}
    if not nodes_path.exists():
        return alias_map

    # Two-pass: first index wikidata/slug nodes, then CIK nodes overwrite them.
    # This ensures CIK entries always win for ambiguous names.
    all_nodes: list[dict] = []
    with nodes_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            all_nodes.append(json.loads(line))

    def _priority(node: dict) -> int:
        """Lower number = processed later (wins overwrite)."""
        return 0 if node["id"].startswith("cik:") else 1

    for n in sorted(all_nodes, key=_priority, reverse=True):
        node_id = n["id"]
        # Index by name and all aliases
        for name in [n.get("name", "")] + n.get("aliases", []):
            if name:
                alias_map[name.lower().strip()] = node_id
    return alias_map


def _fuzzy_match(entity: str, alias_map: dict[str, str],
                 threshold: int = 80) -> Optional[str]:
    """Return node_id for the best fuzzy match of entity in alias_map, or None.

    Two-stage matching:
    1. partial_ratio (query appears inside target) — catches "Apple" in
       "Apple Inc." (score=100) while penalising false tokens like "Appen".
    2. token_sort_ratio fallback for cases where the query is longer than
       the candidate (uncommon in practice, but handled for completeness).
    """
    try:
        from rapidfuzz import process, fuzz
    except ImportError:
        # Fallback: exact match only
        return alias_map.get(entity.lower().strip())

    entity_lower = entity.lower().strip()
    if not entity_lower:
        return None

    # 1. Exact match
    if entity_lower in alias_map:
        return alias_map[entity_lower]

    # 2. partial_ratio — entity string appears within the alias
    #    "apple" vs "apple inc." → 100; "apple" vs "appen" → 80
    #    When multiple aliases tie (e.g. "apple inc." vs "apple international"),
    #    pick the SHORTEST matching alias (most specific/exact name wins).
    candidates = process.extract(
        entity_lower,
        alias_map.keys(),
        scorer=fuzz.partial_ratio,
        score_cutoff=90,
        limit=20,
    )
    if candidates:
        # Sort: highest score first, then shortest alias (prefer "Apple Inc."
        # over "Apple International" when both score 100).
        best = min(candidates, key=lambda r: (-r[1], len(r[0])))
        return alias_map[best[0]]

    # 3. token_sort_ratio fallback (catches multi-token aliases with
    #    different word order, e.g. "Berkshire Hathaway" vs "Hathaway Berkshire")
    result = process.extractOne(
        entity_lower,
        alias_map.keys(),
        scorer=fuzz.token_sort_ratio,
        score_cutoff=threshold,
    )
    if result:
        matched_alias, score, _ = result
        return alias_map[matched_alias]

    return None


# ---------------------------------------------------------------------------
# Edge update
# ---------------------------------------------------------------------------

def _update_edges_with_weights(
    edges_path: Path,
    weight_map: dict[tuple[str, str], float],  # (source, target) -> weight
    weight_quotes: dict[tuple[str, str], str],  # (source, target) -> quote
    dry_run: bool = False,
) -> int:
    """Update edges.jsonl with financial weights. Returns number of edges updated."""
    if not edges_path.exists():
        return 0
    lines = edges_path.read_text(encoding="utf-8").splitlines()
    updated_lines = []
    n_updated = 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        e = json.loads(line)
        src, tgt = e.get("source"), e.get("target")
        key = (src, tgt)
        if key in weight_map and e.get("weight") is None:
            e["weight"] = round(weight_map[key] / 100.0, 4)
            e["source_tier"] = "sec_explicit"
            e["confidence"] = 0.90
            # Update snippet with the verbatim quote if better
            quote = weight_quotes.get(key, "")
            if quote and len(quote) > len(e.get("provenance", {}).get("snippet", "")):
                e.setdefault("provenance", {})["snippet"] = quote
            n_updated += 1
            log.info("Set weight %.2f on %s -> %s", e["weight"], src, tgt)
        updated_lines.append(json.dumps(e, ensure_ascii=False))

    if not dry_run:
        edges_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    return n_updated


# ---------------------------------------------------------------------------
# New edge creation helpers
# ---------------------------------------------------------------------------

def _derive_edgar_url(cik_id: str, accession: str) -> str:
    """Derive EDGAR filing index URL from a CIK node-id and an accession string.

    Example:
        cik_id="cik:0000004281", accession="0000004281-26-000012"
        → "https://www.sec.gov/Archives/edgar/data/4281/000000428126000012/"
    """
    cik_int = cik_id.split(":")[-1].lstrip("0") or "0"
    accession_nodash = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_nodash}/"


def _build_edge_pair_index(edges_path: Path) -> set[tuple[str, str]]:
    """Return the set of (source, target) pairs already in edges.jsonl.

    Includes ALL edge types so we don't create a supplies edge between nodes
    that already have e.g. a competes_with relationship and vice-versa.
    Actually we only suppress NEW supplies edges; we use a supplies-only set
    so that a competes_with edge doesn't prevent a supply edge being created.
    """
    pairs: set[tuple[str, str]] = set()
    if not edges_path.exists():
        return pairs
    with edges_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            src = e.get("source")
            tgt = e.get("target")
            if src and tgt:
                pairs.add((src, tgt))
    return pairs


def _create_new_edge(
    src: str,
    tgt: str,
    pct: float,
    quote: str,
    filing_accession: str,
    filing_url: str,
) -> dict:
    """Build a new SEC-explicit supplies edge dict, ready for JSONL serialisation."""
    return {
        "id": str(uuid.uuid4()),
        "source": src,
        "target": tgt,
        "type": "supplies",
        "directed": True,
        "confidence": 0.90,
        "provenance": {
            "filing": filing_accession,
            "url": filing_url,
            "snippet": quote,
            "extracted_by": "llm:claude-cli",
        },
        "additional_provenance": [],
        "supply_geography": "US",
        "weight": round(pct / 100.0, 4),
        "source_tier": "sec_explicit",
    }


def _append_new_edges(
    edges_path: Path,
    new_edges: list[dict],
    dry_run: bool = False,
) -> int:
    """Append new_edges to edges.jsonl. Returns number of edges written."""
    if not new_edges:
        return 0
    if dry_run:
        for e in new_edges:
            log.info("[DRY] Would create edge %s -> %s (%.0f%%, %s)",
                     e["source"], e["target"],
                     e["weight"] * 100, e["provenance"]["filing"])
        return len(new_edges)
    with edges_path.open("a", encoding="utf-8") as fh:
        for e in new_edges:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    return len(new_edges)


# ---------------------------------------------------------------------------
# Per-hub processing
# ---------------------------------------------------------------------------

def _get_filing_path(cik_digits: str) -> Optional[Path]:
    """Find the cached 10-K .txt file for a given CIK."""
    hub_dir = FILINGS_DIR / cik_digits
    if not hub_dir.exists():
        return None
    txt_files = sorted(hub_dir.glob("*.txt"))
    return txt_files[-1] if txt_files else None  # most recent by name sort


def _process_hub(hub: dict, alias_map: dict[str, str]) -> dict[tuple[str, str], dict]:
    """Return {(src, tgt): {pct, quote, filing, filing_url}} for all extractable
    weights from this hub.

    Each value also carries the filing accession and URL so that callers can
    create new edges with correct provenance without re-deriving them.
    """
    hub_id = hub["id"]
    results: dict[tuple[str, str], dict] = {}

    if not hub_id.startswith("cik:"):
        return results  # no cached 10-K for non-filers

    cik_digits = hub_id.split(":")[-1].lstrip("0") or "0"
    # Pad back to 10 digits for directory lookup
    cik_padded = cik_digits.zfill(10)
    filing_path = _get_filing_path(cik_padded)
    if not filing_path:
        log.debug("No 10-K filing found for hub %s (%s)", hub["name"], cik_padded)
        return results

    log.info("Processing hub: %s (%s)", hub["name"], filing_path.name)
    try:
        text = filing_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        log.warning("Could not read %s: %s", filing_path, exc)
        return results

    # Derive provenance metadata from the cached filing path.
    # filing_path.stem == "0000004281-26-000012"  (the accession number)
    filing_accession = filing_path.stem
    filing_url = _derive_edgar_url(hub_id, filing_accession)

    sections = _extract_filing_sections(text)
    if not sections:
        log.debug("No relevant sections found in %s", filing_path.name)
        return results

    all_extractions: list[dict] = []
    for section_text in sections:
        extractions = _extract_concentrations_llm(section_text)
        all_extractions.extend(extractions)
        if extractions:
            log.info("  Extracted %d concentration(s) from %s section",
                     len(extractions), filing_path.name)
        time.sleep(0.5)  # brief pause between sections

    # Match extracted entity names to graph nodes
    for extraction in all_extractions:
        entity_name = extraction["entity"]
        rel_type = extraction["type"]  # "customer" or "supplier"
        pct = extraction["pct"]
        quote = extraction["quote"]

        matched_id = _fuzzy_match(entity_name, alias_map)
        if not matched_id:
            log.debug("  No graph node found for entity: %s", entity_name)
            continue

        # Determine edge direction:
        # If matched entity is a "customer" of this hub → hub supplies matched_id
        # If matched entity is a "supplier" to this hub → matched_id supplies hub
        if rel_type == "customer":
            src, tgt = hub_id, matched_id
        else:
            src, tgt = matched_id, hub_id

        existing = results.get((src, tgt))
        if existing is None or pct > existing["pct"]:
            results[(src, tgt)] = {
                "pct": pct,
                "quote": quote,
                "filing": filing_accession,
                "filing_url": filing_url,
            }
            log.info("  Matched: %s -> %s = %.1f%% (%s)",
                     src, tgt, pct, entity_name)

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(dry_run: bool = False, max_hubs: Optional[int] = None) -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(message)s",
                        stream=sys.stdout)

    hubs_path = DATA_DIR / "hubs.jsonl"
    if not hubs_path.exists():
        log.error("hubs.jsonl not found — run identify_hubs.py first")
        sys.exit(1)

    hubs: list[dict] = []
    with hubs_path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                hubs.append(json.loads(line))

    if max_hubs:
        hubs = hubs[:max_hubs]

    log.info("Processing %d hub nodes for weight extraction...", len(hubs))

    # Build alias map from nodes
    alias_map = _build_alias_map(DATA_DIR / "nodes.jsonl")
    log.info("Alias map: %d entries", len(alias_map))

    edges_path = DATA_DIR / "edges.jsonl"

    # Build index of existing (source, target) pairs so we know which
    # concentrations need a new edge vs. an update to an existing one.
    existing_pairs = _build_edge_pair_index(edges_path)
    log.info("Existing edge pairs in edges.jsonl: %d", len(existing_pairs))

    # Process hubs sequentially (LLM calls are the bottleneck)
    all_weights: dict[tuple[str, str], float] = {}
    all_quotes: dict[tuple[str, str], str] = {}
    # Per-pair filing metadata for new-edge creation
    all_filings: dict[tuple[str, str], tuple[str, str]] = {}  # (src,tgt) → (accession, url)

    for hub in hubs:
        hub_results = _process_hub(hub, alias_map)
        for (src, tgt), data in hub_results.items():
            key = (src, tgt)
            if key not in all_weights or data["pct"] > all_weights[key]:
                all_weights[key] = data["pct"]
                all_quotes[key] = data["quote"]
                all_filings[key] = (data["filing"], data["filing_url"])

    log.info("Total weight extractions: %d", len(all_weights))

    # Split into: update existing edges vs. create new edges.
    update_weights: dict[tuple[str, str], float] = {}
    update_quotes: dict[tuple[str, str], str] = {}
    new_edges_to_create: list[dict] = []

    for (src, tgt), pct in all_weights.items():
        key = (src, tgt)
        if key in existing_pairs:
            update_weights[key] = pct
            update_quotes[key] = all_quotes[key]
        else:
            filing_accession, filing_url = all_filings[key]
            new_edge = _create_new_edge(
                src=src,
                tgt=tgt,
                pct=pct,
                quote=all_quotes[key],
                filing_accession=filing_accession,
                filing_url=filing_url,
            )
            new_edges_to_create.append(new_edge)
            log.info("  NEW edge: %s -> %s (%.0f%%, %s)",
                     src, tgt, pct, filing_accession)

    log.info("Existing edges to update: %d, New edges to create: %d",
             len(update_weights), len(new_edges_to_create))

    # 1. Update existing edges
    n_updated = _update_edges_with_weights(
        edges_path, update_weights, update_quotes, dry_run=dry_run
    )
    # Also update audit edges
    n_audit = _update_edges_with_weights(
        DATA_DIR / "edges_below_threshold.jsonl",
        update_weights, update_quotes, dry_run=dry_run
    )

    # 2. Append new edges
    n_created = _append_new_edges(edges_path, new_edges_to_create, dry_run=dry_run)

    if dry_run:
        log.info("[DRY RUN] Would have updated %d core + %d audit edges; "
                 "created %d new edges",
                 n_updated, n_audit, n_created)
    else:
        log.info("Updated %d core + %d audit edges; created %d new supply edges",
                 n_updated, n_audit, n_created)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract financial weights from 10-K filings")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be changed without writing files")
    parser.add_argument("--max-hubs", type=int, default=None,
                        help="Process only the first N hubs (for testing)")
    args = parser.parse_args()
    main(dry_run=args.dry_run, max_hubs=args.max_hubs)
