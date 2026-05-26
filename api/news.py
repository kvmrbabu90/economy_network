"""Daily morning brief — fetch top economic headlines, filter with Claude.

Uses RSS feeds from major financial outlets, then asks Claude to distil the
5 most supply-chain / equity-relevant items and trim each to ≤15 words.
Result is cached in-process per calendar day (auto-refreshes after midnight).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import date
from typing import Any

import requests

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# RSS feed catalogue
# ---------------------------------------------------------------------------

_RSS_FEEDS: list[tuple[str, str]] = [
    ("Reuters Business",    "https://feeds.reuters.com/reuters/businessNews"),
    ("Reuters Technology",  "https://feeds.reuters.com/reuters/technologyNews"),
    ("CNBC Top News",       "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("CNBC Economy",        "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("MarketWatch",         "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Yahoo Finance",       "https://finance.yahoo.com/news/rssindex"),
]

_HEADERS = {"User-Agent": "EconGraph/0.1 kondaru.mk@gmail.com"}

# ---------------------------------------------------------------------------
# Claude CLI caller (mirrors api/impact.py's pattern)
# ---------------------------------------------------------------------------

_CLAUDE_BIN_CACHE: str | None = None


def _resolve_claude_binary() -> str:
    global _CLAUDE_BIN_CACHE
    if _CLAUDE_BIN_CACHE:
        return _CLAUDE_BIN_CACHE
    candidates = [
        os.environ.get("CLAUDE_CLI"),
        str(__import__("pathlib").Path.home() / ".local" / "bin" / "claude.exe"),
        shutil.which("claude.exe"),
        shutil.which("claude"),
    ]
    for c in candidates:
        if c and __import__("pathlib").Path(c).exists():
            _CLAUDE_BIN_CACHE = c
            return c
    raise RuntimeError("Could not find `claude` CLI.")


def _claude_call(prompt: str, timeout: int = 120) -> str:
    """Single Claude CLI call; returns model text or empty string on error."""
    binary = _resolve_claude_binary()
    cmd = [binary, "-p", prompt, "--output-format", "json"]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("news: Claude CLI timeout after %ds", timeout)
        return ""
    log.info("news: Claude CLI (%.1fs, exit=%d)", time.time() - t0, proc.returncode)
    if proc.returncode != 0:
        log.warning("news: Claude CLI error: %s", (proc.stderr or "")[:300])
        return ""
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return ""
    if envelope.get("is_error"):
        return ""
    return envelope.get("result", "") or ""

# ---------------------------------------------------------------------------
# Filter prompt
# ---------------------------------------------------------------------------

_FILTER_PROMPT = """\
You are a financial news editor for a supply-chain and equity-markets intelligence tool.

From the raw headlines below, select the 5 most actionable for analysts tracking named companies, commodity prices, trade flows, and regulatory actions. Prioritize:
- Named company events: earnings, layoffs, deals, M&A, product launches, executive changes
- Commodity / input-cost shocks: oil, gas, semiconductors, metals, food crops, freight
- Trade policy: tariffs, sanctions, export controls, port disruptions
- Central bank / regulatory actions with named-company impact

Exclude:
- Political horse-race coverage (polls, campaigns) with no direct market angle
- Celebrity, entertainment, sports
- "Markets mixed / markets up/down" with no named driver
- Purely local stories with no public-company angle

Trim each headline to ≤15 words while keeping the core fact.
Return ONLY a JSON array — no markdown fences, no other text:
[{{"text": "<≤15-word headline>", "source": "<outlet>", "url": "<url>"}}]

If fewer than 5 headlines qualify, return only the ones that do.

Raw headlines:
{headlines}
"""

# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _get_link_from_item(item: ET.Element) -> str:
    """Extract URL from an RSS <item>. Handles both text-content and Atom href."""
    # Standard RSS 2.0: <link>https://…</link>
    link_text = item.findtext("link")
    if link_text and link_text.strip():
        return link_text.strip()
    # Atom: <link href="https://…" />
    for child in item:
        tag = child.tag.split("}")[-1]  # strip namespace
        if tag == "link":
            href = child.get("href", "").strip()
            if href:
                return href
    return ""


def _fetch_raw(max_items: int = 40) -> list[dict[str, Any]]:
    """Pull headlines from RSS feeds. Returns up to max_items dicts."""
    items: list[dict[str, Any]] = []
    for source, url in _RSS_FEEDS:
        if len(items) >= max_items:
            break
        try:
            r = requests.get(url, timeout=8, headers=_HEADERS)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for elem in root.iter("item"):
                title = (elem.findtext("title") or "").strip()
                link = _get_link_from_item(elem)
                if title and link:
                    items.append({"title": title, "source": source, "url": link})
                if len(items) >= max_items:
                    break
        except Exception as exc:
            log.debug("news: feed %s failed: %s", source, exc)
    return items


def _filter_with_claude(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ask Claude to pick + trim the top-5 economic headlines."""
    headlines_block = "\n".join(
        f"{i + 1}. [{item['source']}] {item['title']}  URL: {item['url']}"
        for i, item in enumerate(raw)
    )
    prompt = _FILTER_PROMPT.format(headlines=headlines_block)
    text = _claude_call(prompt)
    if not text:
        return []
    # Strip accidental markdown fences
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:])
    if text.endswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[:-1])
    text = text.strip()
    parsed = json.loads(text)
    if not isinstance(parsed, list):
        return []
    result = []
    for h in parsed:
        if isinstance(h, dict) and h.get("text"):
            result.append({
                "text": str(h["text"]).strip(),
                "source": str(h.get("source", "")).strip(),
                "url": str(h.get("url", "")).strip(),
            })
    return result


# ---------------------------------------------------------------------------
# Public API (cached)
# ---------------------------------------------------------------------------

_cache: dict[str, list[dict[str, Any]]] = {}  # date_str → headlines


def get_daily_headlines(*, force: bool = False) -> list[dict[str, Any]]:
    """Return today's top-5 filtered headlines.

    Result is cached per calendar day so repeated /news/headlines calls
    within the same day are instant. Pass force=True to bypass the cache
    (e.g. manual refresh).
    """
    today = str(date.today())
    if not force and today in _cache:
        return _cache[today]

    raw = _fetch_raw()
    if not raw:
        log.warning("news: all RSS feeds returned 0 items")
        return []

    try:
        filtered = _filter_with_claude(raw)
    except Exception as exc:
        log.warning("news: Claude filter failed (%s); falling back to raw top-5", exc)
        # Graceful degradation: return raw headlines truncated to 15 words
        filtered = []
        for item in raw[:5]:
            words = item["title"].split()
            text = " ".join(words[:15]) + ("…" if len(words) > 15 else "")
            filtered.append({"text": text, "source": item["source"], "url": item["url"]})

    _cache[today] = filtered
    return filtered
