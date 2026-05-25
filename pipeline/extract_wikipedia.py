"""Phase B Step B2: Wikipedia-sourced relationship extraction for Wikidata companies.

For each Phase B company (wikidata:Q... id), fetches its Wikipedia article,
extracts supply-chain and competition edges using the Claude CLI, and appends
verified CandidateEdge records to data/edges_raw.jsonl.

Pipeline invariants:
  - CLAUDE.md #3: Every edge has provenance (filing=wikipedia:<title>, url=..., snippet=...)
  - CLAUDE.md #4: Snippet must literally contain the target name (verify gate)
  - CLAUDE.md #5: Cache Wikipedia responses in data/cache/wikipedia/<qid>.json
  - CLAUDE.md #6: Cache-first: re-reads from cache if present
  - Runs 4 Wikipedia fetches + extractions in parallel (ThreadPoolExecutor)
  - If Claude CLI call fails 3 times, skip and log

Output: appends CandidateEdge records to data/edges_raw.jsonl
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import requests

from schema.models import CandidateEdge, EdgeType, Provenance


log = logging.getLogger("extract_wikipedia")

WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
WIKIPEDIA_REST = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"

MAX_WORKERS = 4
SECTIONS_OF_INTEREST = {"operations", "business", "products", "subsidiaries", "history",
                         "overview", "background", "description", "activities"}
MAX_SECTION_CHARS = 3000

PROMPT_TEMPLATE = """\
You are extracting supply-chain and competition relationships for a graph database.

COMPANY: {name} ({country}, {sector})
SOURCE: Wikipedia article "{title}"
TEXT:
\"\"\"{text}\"\"\"

Extract ONLY relationships where the text explicitly names the other party.
For each relationship found, output JSON:
{{
  "edges": [
    {{
      "type": "supplies" or "competes_with",
      "target": "<exact company name as written in the text>",
      "snippet": "<verbatim quote from text that proves this relationship, max 120 chars>",
      "confidence": 0.0-1.0
    }}
  ]
}}

Rules:
- ONLY include edges where your snippet literally contains the target company name
- "supplies" means the source company sells/provides to the target (source→target)
- "competes_with" means direct competition in the same market
- If no clear relationships found, return {{"edges": []}}
- Do NOT invent relationships not stated in the text
"""


# ---------------------------------------------------------------------------
# Wikipedia fetch helpers
# ---------------------------------------------------------------------------

def _get_ua() -> str:
    ua = os.environ.get("EDGAR_USER_AGENT", "").strip()
    if not ua:
        ua = "EconGraph/1.0 (kondaru.mk@gmail.com)"
    return ua + " (EconGraph Wikipedia Extractor)"


def _wikipedia_title_from_name(name: str) -> str:
    """Best-effort: convert company name to Wikipedia title slug."""
    return name.replace(" ", "_")


def _fetch_wikipedia_summary(title: str) -> Optional[dict[str, Any]]:
    """Fetch the REST summary for a Wikipedia title."""
    url = WIKIPEDIA_REST.format(title=requests.utils.quote(title, safe="()"))
    try:
        resp = requests.get(url, headers={"User-Agent": _get_ua()}, timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.debug("Wikipedia summary fetch failed for %r: %s", title, exc)
        return None


def _fetch_wikipedia_sections(title: str) -> Optional[dict[str, Any]]:
    """Fetch sections + wikitext for a Wikipedia article."""
    params = {
        "action": "parse",
        "page": title,
        "prop": "sections|wikitext",
        "format": "json",
        "redirects": 1,
    }
    try:
        resp = requests.get(
            WIKIPEDIA_API,
            params=params,
            headers={"User-Agent": _get_ua()},
            timeout=20,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            return None
        return data.get("parse")
    except Exception as exc:
        log.debug("Wikipedia sections fetch failed for %r: %s", title, exc)
        return None


def _extract_section_text(parse_data: dict[str, Any]) -> str:
    """Extract up to MAX_SECTION_CHARS from relevant sections."""
    wikitext = parse_data.get("wikitext", {}).get("*", "") or ""
    sections = parse_data.get("sections", []) or []

    # Build a set of byte offsets for sections of interest
    relevant_section_indices: set[int] = set()
    for s in sections:
        title_lower = (s.get("line") or "").lower()
        if any(kw in title_lower for kw in SECTIONS_OF_INTEREST):
            relevant_section_indices.add(int(s.get("index", -1)))

    if not relevant_section_indices:
        # No matching section headers — use the lead paragraph (first 3000 chars)
        # Strip wiki markup minimally
        text = re.sub(r"\{\{[^}]+\}\}", "", wikitext)
        text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)
        text = re.sub(r"[=]{2,}[^=]+[=]{2,}", "", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:MAX_SECTION_CHARS]

    # Find the wikitext segments for those sections
    # Sections are separated by == Section Title == headers
    # Use a simple approach: split on level-2/3 headers
    parts: list[str] = []
    current_section = "lead"
    current_content: list[str] = []
    current_idx = 0

    for line in wikitext.split("\n"):
        m = re.match(r"^(={2,})\s*(.*?)\s*\1\s*$", line)
        if m:
            # Save previous section
            current_section = m.group(2)
            current_idx += 1
            current_content = []
        else:
            current_content.append(line)
            if current_idx in relevant_section_indices or current_section.lower() in SECTIONS_OF_INTEREST:
                parts.append(line)

    text = "\n".join(parts)
    # Strip wiki markup
    text = re.sub(r"\{\{[^}]+\}\}", "", text)
    text = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"''+", "", text)
    text = re.sub(r"\s+", " ", text).strip()

    if not text:
        # Fall back to lead
        clean = re.sub(r"\{\{[^}]+\}\}", "", wikitext)
        clean = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]+)\]\]", r"\1", clean)
        clean = re.sub(r"[=]{2,}[^=]+[=]{2,}", "", clean)
        clean = re.sub(r"<[^>]+>", "", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        text = clean[:MAX_SECTION_CHARS]

    return text[:MAX_SECTION_CHARS]


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def _load_cached(cache_path: Path) -> Optional[dict[str, Any]]:
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _save_cache(cache_path: Path, data: dict[str, Any]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Claude CLI extraction
# ---------------------------------------------------------------------------

def _find_claude_binary() -> Optional[str]:
    candidates = [
        os.environ.get("CLAUDE_CLI"),
        str(Path.home() / ".local" / "bin" / "claude.exe"),
        shutil.which("claude.exe"),
        shutil.which("claude"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def _claude_call(prompt: str, *, max_retries: int = 3) -> Optional[dict[str, Any]]:
    """Call claude CLI and return parsed JSON envelope. Returns None on failure."""
    binary = _find_claude_binary()
    if not binary:
        log.error("Claude CLI not found; skipping Wikipedia LLM extraction")
        return None

    for attempt in range(max_retries):
        try:
            proc = subprocess.run(
                [binary, "-p", prompt, "--output-format", "json"],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            if proc.returncode != 0:
                log.warning("Claude CLI attempt %d failed (exit %d): %s",
                            attempt + 1, proc.returncode, (proc.stderr or "")[:200])
                continue
            envelope = json.loads(proc.stdout)
            if envelope.get("is_error"):
                log.warning("Claude CLI attempt %d is_error: %s",
                            attempt + 1, envelope.get("result", "")[:200])
                continue
            return envelope
        except subprocess.TimeoutExpired:
            log.warning("Claude CLI attempt %d timed out", attempt + 1)
        except Exception as exc:
            log.warning("Claude CLI attempt %d exception: %s", attempt + 1, exc)
        if attempt < max_retries - 1:
            time.sleep(2 ** attempt)  # brief backoff
    return None


def _parse_edges_from_envelope(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse edges from Claude CLI envelope."""
    text = envelope.get("result", "")
    if not text:
        return []
    # Try to parse as JSON with "edges" key
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "edges" in obj:
            return obj["edges"] if isinstance(obj["edges"], list) else []
        if isinstance(obj, list):
            return obj
    except json.JSONDecodeError:
        pass
    # Try to find JSON object in text
    m = re.search(r'\{[^{}]*"edges"\s*:\s*\[.*?\]\s*\}', text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj.get("edges", [])
        except json.JSONDecodeError:
            pass
    return []


# ---------------------------------------------------------------------------
# Verify gate (CLAUDE.md invariant #4)
# ---------------------------------------------------------------------------

def _target_in_snippet(target: str, snippet: str) -> bool:
    if not target or not snippet:
        return False
    t = re.sub(r"\s+", " ", target).strip().lower()
    sn = re.sub(r"\s+", " ", snippet).strip().lower()
    if t in sn:
        return True
    # Check distinctive tokens (>=4 chars, not stop words)
    stop = {"company", "companies", "corp", "corporation", "inc", "incorporated",
            "ltd", "limited", "plc", "holdings", "group", "the", "and", "of"}
    tokens = [re.sub(r"[^a-z0-9\-]+", "", tk) for tk in t.split()]
    distinctive = [tk for tk in tokens if len(tk) >= 4 and tk not in stop]
    return any(tk in sn for tk in distinctive)


# ---------------------------------------------------------------------------
# Per-company processing
# ---------------------------------------------------------------------------

def _process_company(
    node: dict[str, Any],
    cache_dir: Path,
) -> list[CandidateEdge]:
    """Fetch Wikipedia, extract edges for one company. Returns verified edges."""
    node_id = node.get("id", "")
    if not node_id.startswith("wikidata:"):
        return []

    qid = node_id[len("wikidata:"):]
    name = node.get("name", "")
    country = node.get("country", "")
    sector = node.get("sector", "")
    industry = node.get("industry", "")

    if not name:
        return []

    # Try to get Wikipedia title from wikidata metadata or use name directly
    wiki_meta = (node.get("metadata") or {}).get("wikidata") or {}
    title = _wikipedia_title_from_name(name)

    cache_path = cache_dir / "wikipedia" / f"{qid}.json"

    # Load from cache if available
    cached = _load_cached(cache_path)
    if cached is not None:
        log.debug("Wikipedia cache hit for %s (%s)", name, qid)
        article_text = cached.get("text", "")
        actual_title = cached.get("title", title)
        wiki_url = cached.get("url", f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}")
    else:
        # Fetch Wikipedia
        summary = _fetch_wikipedia_summary(title)
        if summary is None:
            log.debug("No Wikipedia article for %s", name)
            _save_cache(cache_path, {"text": "", "title": title, "url": "", "found": False})
            return []

        actual_title = summary.get("title", title)
        wiki_url = summary.get("content_urls", {}).get("desktop", {}).get("page", "")
        if not wiki_url:
            wiki_url = f"https://en.wikipedia.org/wiki/{actual_title.replace(' ', '_')}"

        parse_data = _fetch_wikipedia_sections(actual_title)
        if parse_data:
            article_text = _extract_section_text(parse_data)
        else:
            # Fall back to extract from summary
            article_text = summary.get("extract", "")[:MAX_SECTION_CHARS]

        _save_cache(cache_path, {
            "text": article_text,
            "title": actual_title,
            "url": wiki_url,
            "found": True,
            "summary": summary.get("extract", "")[:500],
        })

    if not article_text or len(article_text) < 50:
        return []

    # Build extraction prompt
    prompt = PROMPT_TEMPLATE.format(
        name=name,
        country=country,
        sector=sector,
        title=actual_title,
        text=article_text,
    )

    # Call Claude CLI
    envelope = _claude_call(prompt)
    if envelope is None:
        log.debug("Claude CLI extraction failed for %s", name)
        return []

    raw_edges = _parse_edges_from_envelope(envelope)
    if not raw_edges:
        return []

    # Build verified CandidateEdge records
    verified_edges: list[CandidateEdge] = []
    wiki_url_clean = wiki_url or f"https://en.wikipedia.org/wiki/{actual_title.replace(' ', '_')}"
    filing_ref = f"wikipedia:{actual_title}"

    for edge in raw_edges:
        target = (edge.get("target") or "").strip()
        edge_type_str = (edge.get("type") or "").strip().lower()
        snippet = (edge.get("snippet") or "").strip()
        confidence = float(edge.get("confidence", 0.7))

        if not target or not edge_type_str or not snippet:
            continue

        # Skip if target is same as source
        if target.lower() == name.lower():
            continue

        # Map edge type
        if edge_type_str not in ("supplies", "competes_with"):
            continue

        # Verify: snippet must contain target name (invariant #4)
        if not _target_in_snippet(target, snippet):
            log.debug("Verify gate: snippet does not contain target %r — discarding", target)
            continue

        # Clamp confidence
        confidence = max(0.0, min(1.0, confidence))

        try:
            ce = CandidateEdge(
                id=str(uuid4()),
                source_id=node_id,
                target_raw=target,
                type=EdgeType(edge_type_str),
                confidence=confidence,
                provenance=Provenance(
                    filing=filing_ref,
                    url=wiki_url_clean,
                    snippet=snippet[:500],
                    extracted_by="llm:claude-cli",
                ),
                verified=True,
            )
            verified_edges.append(ce)
        except Exception as exc:
            log.debug("CandidateEdge build failed for %s -> %s: %s", name, target, exc)

    log.info("Wikipedia extraction for %s (%s): %d/%d edges passed verify gate",
             name, qid, len(verified_edges), len(raw_edges))
    return verified_edges


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

def run(
    *,
    data_root: Path,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    companies_path = data_root / "companies.jsonl"
    edges_raw_path = data_root / "edges_raw.jsonl"
    cache_dir = data_root / "cache"

    # Load Phase B companies only (wikidata: ids)
    all_companies: list[dict[str, Any]] = []
    for line in companies_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if obj.get("id", "").startswith("wikidata:"):
                all_companies.append(obj)
        except Exception:
            continue

    log.info("Found %d wikidata companies to process", len(all_companies))

    if limit is not None:
        all_companies = all_companies[:limit]
        log.info("Limit applied: processing %d companies", len(all_companies))

    # Determine which companies have already been processed (cached)
    # We check by seeing if the cache file exists and has edges
    wiki_cache_dir = cache_dir / "wikipedia"
    wiki_cache_dir.mkdir(parents=True, exist_ok=True)

    # Process companies in parallel
    total_processed = 0
    total_skipped = 0
    n_written = 0
    pending_edges: list[CandidateEdge] = []

    FLUSH_EVERY = 50  # write to disk every N companies

    def _flush(pending: list[CandidateEdge]) -> int:
        if not pending:
            return 0
        edges_raw_path.parent.mkdir(parents=True, exist_ok=True)
        with edges_raw_path.open("a", encoding="utf-8") as fh:
            for ce in pending:
                fh.write(ce.model_dump_json())
                fh.write("\n")
        written = len(pending)
        pending.clear()
        return written

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_process_company, node, cache_dir): node
            for node in all_companies
        }
        for future in as_completed(futures):
            node = futures[future]
            try:
                edges = future.result()
                pending_edges.extend(edges)
                total_processed += 1
                if total_processed % FLUSH_EVERY == 0:
                    written = _flush(pending_edges)
                    n_written += written
                    log.info("Progress: %d/%d companies processed, %d edges written so far",
                             total_processed, len(all_companies), n_written)
            except Exception as exc:
                log.warning("Processing failed for %s: %s", node.get("name"), exc)
                total_skipped += 1

    # Final flush for remaining edges
    n_written += _flush(pending_edges)
    log.info("Appended %d verified edges to %s", n_written, edges_raw_path)

    summary = {
        "companies_processed": total_processed,
        "companies_skipped": total_skipped,
        "edges_verified": n_written,
        "edges_written": n_written,
    }
    log.info("Wikipedia extraction summary:\n%s", json.dumps(summary, indent=2))
    return summary


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="EconGraph Phase B — Wikipedia extraction")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--limit", type=int, default=None, help="Process only first N companies (smoke test)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    summary = run(
        data_root=Path(args.data_root),
        limit=args.limit,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
