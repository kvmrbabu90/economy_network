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
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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
You are filtering headlines for a supply-chain and equity impact tool. \
The tool traces how news propagates through a graph of companies, commodities, \
and regions. For a headline to be useful it must have an IDENTIFIABLE SEED — \
a named company taking an action, or a named commodity/region experiencing a \
supply or demand shock — so the tool can start a propagation chain without \
hallucinating a node.

TODAY'S DATE: {today}
RECENCY RULE — exclude anything older than 5 days:
- If the underlying event clearly happened more than 5 days before today, \
  exclude it — even if the article was published today. Signs of staleness: \
  explicit past dates ("announced March 10"), "last week", "earlier this month", \
  or context that makes clear the event predates the 5-day window.
- If you cannot tell when the event happened, keep it (benefit of the doubt).
- Recap, anniversary, and retrospective articles about past events are excluded \
  regardless of publication date.

INCLUDE — headline has a clear seed and describes a CAUSE, not an effect:
- Named company actions: M&A, partnerships, deals, product launches, factory \
  openings/closures, layoffs, regulatory approvals/rejections, contract wins/losses
- Named company earnings ONLY when tied to a concrete driver (new product, \
  cost programme, market entry) — not just "beat estimates"
- Commodity supply or demand events: output cuts, crop failures, new discoveries, \
  trade route disruptions, freight rate changes, energy supply agreements
- Macroeconomic data releases with a named sector or region: industrial output, \
  manufacturing PMI, trade balance, inflation by category
- Trade policy directly affecting named goods or companies: tariffs, sanctions, \
  export controls, import bans
- Central bank rate decisions
- Geopolitical events ONLY when the headline explicitly names a commodity or \
  supply-chain impact (e.g. "Hormuz closure cuts oil flow", "port strike halts \
  auto parts" — not just "tensions rise")

EXCLUDE — no identifiable seed, or describes aftermath rather than a cause:
- Political news (elections, primaries, endorsements, polling) unless the \
  headline names a specific company or commodity directly affected by that event
- Stock price or valuation milestones: "X shares rise/fall N%", "X hits $1T \
  valuation", "X stock surges" — these are outcomes, not causes; the tool \
  cannot act on past price moves
- Index or broad market moves: "S&P 500 up/down", "Nasdaq hits high", \
  "markets mixed"
- Earnings beats/misses with no named driver ("X tops estimates", "profits rise")
- Celebrity, entertainment, sports, obituaries
- Opinion, analysis, editorial, or forecast pieces
- Any headline where you would need to invent a company or commodity name to \
  create a seed — if the seed is not stated, exclude it

REWRITE RULES — apply to every headline you keep:
- ≤15 words
- Who + what only. Neutral verbs: announces, reports, rises, falls, cuts, \
  acquires, approves, launches, signs, halts, closes, opens, raises, reduces.
- No loaded words: rattles, warns, fears, surges, soars, plummets, looms, \
  threatens, roils, jolts, shocks, crisis, turmoil, chaos, sparks.
- No judgment adjectives: massive, alarming, stunning, historic, unprecedented.
- Keep specific numbers (prices, %, quantities) — they are facts.
- Do NOT invent facts not in the original headline.

Return ONLY a valid JSON array — no markdown, no other text:
[{{"text": "<rewritten headline ≤15 words>", "source": "<outlet name>", "url": "<url>"}}]

Select the top 5 by usefulness for supply-chain impact tracing. \
Return fewer if fewer than 5 qualify — it is better to return 2 good headlines \
than 5 where some are borderline.

Raw headlines:
{headlines}
"""

# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


# Articles older than this are dropped before Claude ever sees them.
_MAX_ARTICLE_AGE_DAYS = 5


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


def _parse_pub_date(item: ET.Element) -> datetime | None:
    """Return the UTC-aware pubDate of an RSS item, or None if unparseable."""
    raw = item.findtext("pubDate") or ""
    raw = raw.strip()
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        # Ensure timezone-aware for comparison with utcnow.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _fetch_raw(max_items: int = 40) -> list[dict[str, Any]]:
    """Pull headlines from RSS feeds published within the last 5 days.

    Articles whose <pubDate> is older than _MAX_ARTICLE_AGE_DAYS are dropped
    at the feed layer so Claude never sees stale items. Articles with no
    parseable pubDate are kept (benefit of the doubt — most are same-day).
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=_MAX_ARTICLE_AGE_DAYS)
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
                if not title or not link:
                    continue
                # Drop articles older than the cutoff. Items with no pubDate
                # are kept — most are same-day and better to include than skip.
                pub_dt = _parse_pub_date(elem)
                if pub_dt is not None and pub_dt < cutoff:
                    log.debug("news: skipping stale item (%s): %s", pub_dt.date(), title[:60])
                    continue
                items.append({
                    "title": title,
                    "source": source,
                    "url": link,
                    "pub_date": pub_dt.strftime("%Y-%m-%d") if pub_dt else None,
                })
                if len(items) >= max_items:
                    break
        except Exception as exc:
            log.debug("news: feed %s failed: %s", source, exc)
    return items


def _filter_with_claude(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ask Claude to pick + trim the top-5 economic headlines."""
    headlines_block = "\n".join(
        # Include pub_date so Claude can reason about recency precisely.
        f"{i + 1}. [{item['source']}] [published: {item.get('pub_date') or 'unknown'}] "
        f"{item['title']}  URL: {item['url']}"
        for i, item in enumerate(raw)
    )
    today_str = date.today().strftime("%Y-%m-%d")
    prompt = _FILTER_PROMPT.format(headlines=headlines_block, today=today_str)
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
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("news: Claude JSON parse failed: %s; head=%s", exc, text[:200])
        return []
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
    return result[:5]  # hard cap — always show exactly 5 or fewer


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

    def _raw_fallback(items: list[dict]) -> list[dict]:
        """Return raw headlines trimmed to 15 words — used when Claude fails or returns nothing."""
        result = []
        for item in items[:5]:
            words = item["title"].split()
            text = " ".join(words[:15]) + ("…" if len(words) > 15 else "")
            result.append({"text": text, "source": item["source"], "url": item["url"]})
        return result

    try:
        filtered = _filter_with_claude(raw)
        if not filtered:
            log.warning("news: Claude returned 0 headlines; using raw fallback")
            filtered = _raw_fallback(raw)
    except Exception as exc:
        log.warning("news: Claude filter failed (%s); using raw fallback", exc)
        filtered = _raw_fallback(raw)

    _cache[today] = filtered
    return filtered
