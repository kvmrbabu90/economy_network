"""HTML -> normalized text + section extraction for cached 10-K filings.

Two responsibilities:

1. `load_filing_text(path)` -- read a cached .htm filing, strip to plain text,
   normalize whitespace, and cache the result as a sibling .txt file. The .txt
   is the SINGLE source of truth that both the LLM extractor (Part B) and the
   verify gate read, so they always see the same string.

2. `extract_sections(text)` -- locate the two passages a 10-K reader cares
   about for relationship extraction:

      - 'competition'  : prose discussing competitors and competitive dynamics
                         (Item 1 "Competition" subsection, MD&A competitive
                         pressures, etc.)
      - 'customers'    : prose disclosing customer concentration (named
                         customers >10% of revenue, top-N customer counts)

   The 10-K body for these doesn't live under one stable heading -- real
   filings vary, and some (e.g. Procter & Gamble FY2025) don't carry a
   subsection literally titled "Competition" at all. So instead of pinning to
   headings, we collect the paragraphs that contain the right anchor terms
   ("compete", "competitor", percentages of net sales, etc.) and merge
   overlapping windows. This is permissive on purpose -- the grounding gate
   in Part B is what enforces correctness.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from bs4 import BeautifulSoup


log = logging.getLogger("ingest.sections")


# ---------------------------------------------------------------------------
# HTML -> text
# ---------------------------------------------------------------------------

# Anything before "PART I" / "Item 1" in a 10-K is XBRL / cover-page noise that
# tends to dwarf the actual prose. Trimming it up front shrinks the haystack
# the LLM sees and prevents the grounding gate from accidentally matching a
# snippet against random XBRL element names.
_BODY_ANCHOR_RE = re.compile(
    r"(?i)\b(part\s+i\b|item\s*1\.?\s*business|forward[- ]looking statements)",
)


def _html_to_text(html: str) -> str:
    """BeautifulSoup get_text + whitespace normalization, paragraph-preserving."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    raw = soup.get_text("\n")
    # Collapse intra-line whitespace, strip leading spaces per line, and
    # collapse runs of blank lines to a single blank. Keep \n as a paragraph
    # boundary so heuristics below can split on it.
    raw = raw.replace("\xa0", " ")
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n[ \t]+", "\n", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def _trim_to_body(text: str) -> str:
    """Drop the XBRL/coverpage preamble before the first real 10-K anchor."""
    m = _BODY_ANCHOR_RE.search(text)
    return text[m.start():] if m else text


def load_filing_text(htm_path: Path, *, force: bool = False) -> tuple[Path, str]:
    """Return (txt_path, normalized_text) for a cached .htm filing.

    Cache-first: if the sibling .txt already exists and isn't empty, reuse it.
    Pass force=True to re-derive (useful when changing the normalizer).
    """
    htm_path = Path(htm_path)
    txt_path = htm_path.with_suffix(".txt")
    if not force and txt_path.exists() and txt_path.stat().st_size > 0:
        return txt_path, txt_path.read_text(encoding="utf-8")
    html = htm_path.read_text(encoding="utf-8", errors="replace")
    text = _html_to_text(html)
    text = _trim_to_body(text)
    txt_path.write_text(text, encoding="utf-8")
    return txt_path, text


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------

# Anchors that suggest a passage discusses competitive dynamics. We deliberately
# include "compete" / "competitive" — many filings (e.g. P&G FY2025) discuss
# the competitive landscape under headings other than a literal "Competition"
# subsection.
_COMPETE_RE = re.compile(
    r"(?i)\b("
    r"competitor[s]?|"
    r"compete[sd]?|"
    r"competitive\s+(?:pressures|landscape|environment|advantage|set|setting)|"
    r"principal\s+competitors|"
    r"private[- ]label\s+brands"
    r")\b"
)

# Customer concentration anchors. We're looking for "X% of (net )?sales/revenue"
# language and named customers (the 10-K disclosure threshold is 10%, so the
# % digits tend to be in [10, 80]).
_CUSTOMER_PCT_RE = re.compile(
    r"(?i)("
    r"\d{1,2}(?:\.\d+)?\s*%\s+of\s+(?:our\s+|the\s+Company['’]s\s+|consolidated\s+|total\s+|combined\s+)?(?:net\s+|consolidated\s+)?(?:sales|revenue[s]?)|"
    r"sales\s+to\s+\w+(?:[\w&.\-,]+\s+){0,5}(?:represent|accounted\s+for|comprised)|"
    r"largest\s+customer|"
    r"top\s+(?:five|five|ten|10|five|ten)\s+customers|"
    r"top\s+\d+\s+customers|"
    r"no\s+other\s+customer\s+(?:individually\s+)?(?:represent|accounted)"
    r")"
)

# How much text to grab on either side of an anchor. Wide enough to give the
# LLM a coherent paragraph; narrow enough that 30 windows stay under 30K chars.
_WINDOW = 900


@dataclass
class SectionResult:
    """One filing's extracted prose chunks."""

    cik: str
    accession: str
    txt_path: str
    found: dict[str, bool] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)
    # Total source-text length (post-trim) — useful for diagnostics.
    text_length: int = 0


def _windows_for_pattern(text: str, pattern: re.Pattern, window: int) -> list[tuple[int, int]]:
    """Return merged (start, end) char-offset windows around every pattern hit."""
    spans: list[tuple[int, int]] = []
    for m in pattern.finditer(text):
        s = max(0, m.start() - window)
        e = min(len(text), m.end() + window)
        spans.append((s, e))
    if not spans:
        return []
    spans.sort()
    merged: list[tuple[int, int]] = [spans[0]]
    for s, e in spans[1:]:
        ps, pe = merged[-1]
        if s <= pe:  # overlap or contiguous
            merged[-1] = (ps, max(pe, e))
        else:
            merged.append((s, e))
    return merged


def _join_windows(text: str, windows: Iterable[tuple[int, int]], cap: int = 28000) -> str:
    """Concatenate text windows with separators; cap total length defensively."""
    parts: list[str] = []
    total = 0
    for s, e in windows:
        chunk = text[s:e].strip()
        if not chunk:
            continue
        # Prepend a position tag so the LLM can tell chunks apart.
        tagged = f"[chunk @ char {s}]\n{chunk}"
        if total + len(tagged) > cap:
            break
        parts.append(tagged)
        total += len(tagged) + 2
    return "\n\n---\n\n".join(parts)


def extract_sections(text: str) -> dict[str, str]:
    """Return {section_name: chunk_text}. Missing sections yield empty strings."""
    out: dict[str, str] = {}

    comp_windows = _windows_for_pattern(text, _COMPETE_RE, _WINDOW)
    out["competition"] = _join_windows(text, comp_windows)

    cust_windows = _windows_for_pattern(text, _CUSTOMER_PCT_RE, _WINDOW)
    out["customers"] = _join_windows(text, cust_windows)

    return out


def extract_filing_sections(htm_path: Path, *, force: bool = False) -> SectionResult:
    """One-stop: load .htm, normalize/cache .txt, return a SectionResult."""
    htm_path = Path(htm_path)
    txt_path, text = load_filing_text(htm_path, force=force)
    # Parse cik + accession from the cached path: data/filings/<cik>/<accession>.htm
    cik = htm_path.parent.name
    accession = htm_path.stem
    sections = extract_sections(text)
    result = SectionResult(
        cik=cik,
        accession=accession,
        txt_path=str(txt_path),
        text_length=len(text),
        sections=sections,
        found={k: bool(v) for k, v in sections.items()},
    )
    if not result.found.get("competition"):
        log.info("No competition prose located in %s/%s", cik, accession)
    if not result.found.get("customers"):
        log.info("No customer-concentration prose located in %s/%s", cik, accession)
    return result


# ---------------------------------------------------------------------------
# Convenience helpers used by the orchestrator
# ---------------------------------------------------------------------------

def filings_for_company(company_node: dict, data_root: Optional[Path] = None) -> list[Path]:
    """Resolve the .htm filing paths a company node points to (per Phase 1)."""
    data_root = Path(data_root) if data_root else Path("data")
    paths: list[Path] = []
    for f in (company_node.get("metadata") or {}).get("filings", []) or []:
        local = f.get("local_path")
        if not local:
            continue
        p = Path(local)
        if not p.is_absolute():
            # local_path is recorded relative to the repo root.
            p = (data_root.parent / p) if p.parts and p.parts[0] != "data" else Path(p)
        if not p.exists():
            log.warning("Filing path missing from disk: %s", p)
            continue
        paths.append(p)
    return paths
