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
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger(__name__)

# Graph DB used to gate headlines: a brief item whose primary entity isn't a node
# can't seed a "So What?" trace, so we drop it. Resolved relative to the repo root.
_DB_PATH = Path(__file__).resolve().parent.parent / "econgraph.db"

# ---------------------------------------------------------------------------
# RSS feed catalogue
# ---------------------------------------------------------------------------

# (name, url, max_age_days). Curated general-business / markets feeds with a tight
# recency window. We deliberately dropped the SEC/Fed primary feeds and the PR
# Newswire/GlobeNewswire wires: they yielded a useful seed only rarely (a rate
# decision, a large-cap deal) while injecting daily junk — regulatory-process
# notices, obituaries, and untraceable micro-cap M&A — that padded the brief.
_RSS_FEEDS: list[tuple[str, str, int]] = [
    ("CNBC Top News",       "https://www.cnbc.com/id/100003114/device/rss/rss.html", 2),
    ("CNBC Economy",        "https://www.cnbc.com/id/20910258/device/rss/rss.html", 2),
    ("CNBC Earnings",       "https://www.cnbc.com/id/15839135/device/rss/rss.html", 3),
    ("CNBC Technology",     "https://www.cnbc.com/id/19854910/device/rss/rss.html", 2),
    ("MarketWatch",         "https://feeds.marketwatch.com/marketwatch/topstories/", 2),
    ("Yahoo Finance",       "https://finance.yahoo.com/news/rssindex", 2),
    ("Investing.com",       "https://www.investing.com/rss/news.rss", 2),
    # NOTE: the old feeds.reuters.com Business/Technology feeds were removed —
    # Reuters discontinued public RSS, so those URLs were dead (0 items).
    # V2: broadened category coverage (verified each returns items 2026-06-30).
    ("CNBC World",       "https://www.cnbc.com/id/100727362/device/rss/rss.html", 2),
    ("CNBC Politics",    "https://www.cnbc.com/id/10000113/device/rss/rss.html", 2),
    ("CNBC Health Care", "https://www.cnbc.com/id/10000108/device/rss/rss.html", 3),
    ("CNBC Energy",      "https://www.cnbc.com/id/19836768/device/rss/rss.html", 2),
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
    """Single Claude CLI call; returns model text or empty string on error.

    Windows-safe process management (mirrors api/impact.py._claude_call): the CLI
    spawns a Node child tree, and subprocess.run(timeout=) only kills the direct
    process, orphaning that tree. Use Popen in a new process group + communicate,
    and on timeout `taskkill /F /T` the whole tree, then drain the pipes with a
    bounded communicate() so the process is actually reaped before we return.
    Fail-open: any error/timeout returns "".
    """
    import tempfile
    binary = _resolve_claude_binary()
    cmd = [binary, "-p", prompt, "--output-format", "json"]
    t0 = time.time()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            _kw: dict = dict(
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=tmpdir,   # run outside repo root so CLAUDE.md doesn't block
            )
            if sys.platform == "win32":
                # Run in a new process group so we can kill the whole tree.
                _kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            with subprocess.Popen(cmd, **_kw) as proc:
                try:
                    stdout_b, stderr_b = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    log.warning("news: Claude CLI timeout after %ds — killing process tree", timeout)
                    if sys.platform == "win32":
                        subprocess.call(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        proc.kill()
                    try:
                        proc.communicate(timeout=5)  # drain pipes so the process exits cleanly
                    except subprocess.TimeoutExpired:
                        pass
                    return ""
                stdout_bytes = stdout_b
                stderr_bytes = stderr_b
                returncode = proc.returncode
    except Exception as exc:
        log.warning("news: Claude CLI launch failed: %s", exc)
        return ""
    stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    log.info("news: Claude CLI (%.1fs, exit=%d)", time.time() - t0, returncode)
    if returncode != 0:
        log.warning("news: Claude CLI error: %s", stderr[:300])
        return ""
    try:
        envelope = json.loads(stdout)
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
a named company taking a specific action, or a named commodity/region \
experiencing a supply or demand shock — so the tool can start a propagation \
chain without hallucinating a node.

CAUSE vs AFTERMATH (the most important rule):
The seed must be a CAUSE that will ripple FORWARD through the graph, never a \
measurement of ripples that already happened. A price move, index move, or \
data print is an AFTERMATH — the market already reacted, so there is nothing \
left to propagate.
- If a headline reports an effect (price/index move, data print) AND names \
  the event that caused it, the seed is the CAUSE. REWRITE the headline to \
  lead with the causal event and DROP the measured effect — the tool computes \
  the effect itself. Example: "U.S. crude falls below $85 as U.S. and Iran \
  near deal to reopen Hormuz" → rewrite to "U.S. and Iran near deal to reopen \
  Strait of Hormuz" (the deal is the seed; the price drop is what the tool \
  derives). Example: "Wholesale prices rose 1.1% in May, driven by energy" → \
  rewrite to "Energy costs climb, lifting U.S. wholesale prices in May" (the \
  energy-cost rise is the forward-propagating driver).
- If a headline reports an effect with NO named cause, it is pure aftermath — \
  EXCLUDE it. "Crude oil falls below $85" alone, "S&P 500 closes higher", \
  "PPI rose 1.1%" with no driver → EXCLUDE.

TODAY'S DATE: {today}
RECENCY RULE — exclude anything older than 2 days:
- If the underlying event clearly happened more than 2 days before today, \
  exclude it — even if the article was published today. Signs of staleness: \
  explicit past dates ("announced June 3"), "last week", "earlier this month", \
  or context that makes clear the event predates the 2-day window.
- If you cannot tell when the event happened, keep it (benefit of the doubt).
- Recap, anniversary, and retrospective articles about past events are excluded \
  regardless of publication date.

INCLUDE — headline has a clear seed and describes a CAUSE, not an effect:
- Named company actions with specific detail: M&A (named acquirer + target), \
  named partnerships/contracts, product launches (named product or service), \
  factory openings/closures, layoffs with a stated number, regulatory \
  approvals/rejections of a named drug/product, named contract wins/losses
- Named company earnings ONLY when tied to a concrete driver (new product, \
  cost programme, market entry) — not just "beat estimates"
- Commodity supply or demand events: output cuts, crop failures, new \
  discoveries, trade route disruptions, freight rate changes, energy \
  supply agreements
- Macroeconomic data releases (CPI, PPI, GDP, payrolls, PMI) ONLY when the \
  headline names the DRIVER behind the move — and then REWRITE to lead with \
  that driver, not the data print (see CAUSE vs AFTERMATH). "Wholesale prices \
  +1.1% driven by energy" → keep, rewritten as "Energy costs lift U.S. \
  wholesale prices 1.1% in May". A bare print with NO named driver ("PPI rose \
  1.1%", "GDP grew 2%", "payrolls up 150k") is aftermath → EXCLUDE, however \
  major the release.
- Central bank rate decisions
- Trade policy directly affecting named goods or companies: tariffs, \
  sanctions, export controls, import bans
- Geopolitical events ONLY when the headline explicitly names a commodity or \
  supply-chain impact AND states the mechanism (e.g. "Hormuz closure cuts oil \
  flow by 20%", "port strike halts auto-parts shipments"). "Tensions rise", \
  "ceasefire in jeopardy", "missiles fired" with no commodity named → EXCLUDE.

EXCLUDE — no identifiable seed, describes aftermath, or lacks specifics:
- Vague company announcements with no concrete detail: "X expands Y \
  operations", "X grows Z business", "X moves into Y space", "X bets on Y" — \
  include ONLY if the headline names a specific partner, deal value, geography, \
  product, or quantity. "Amazon expands trucking" with nothing else → EXCLUDE. \
  "Amazon signs $500M trucking contract with UPS" → INCLUDE.
- Political news (elections, primaries, endorsements, polling, government \
  statements, politician interviews, speeches, press conferences) unless the \
  headline names a specific company or commodity directly affected.
- Government/geopolitical events with no explicit commodity or company impact.
- Stock price moves as the main story: "X shares rise/fall N%", "X hits $1T \
  valuation" — these are outcomes, not causes. If the headline names the \
  concrete cause, KEEP it but REWRITE to lead with the cause and drop the \
  share-price move (e.g. "Oracle shares fall 11% after announcing $10B capital \
  raise" → "Oracle announces $10B capital raise"). No named cause → EXCLUDE.
- Index or broad market moves: "S&P 500 up/down", "Nasdaq hits high" — \
  aftermath with no seed, always EXCLUDE.
- Commodity price moves with no named cause: "crude oil falls below $85", \
  "gold tops $2,500" — aftermath, EXCLUDE. With a named cause, reframe to the \
  cause per the CAUSE vs AFTERMATH rule.
- Earnings beats/misses with no named driver
- IPO valuation or market-cap milestone articles (how rich investors got, \
  what millionaires will buy, will it break the bull market) — pure finance
- Celebrity, entertainment, sports, personal finance, obituaries
- Opinion, analysis, editorial, explainer, or forecast pieces. Signals: \
  "Inside the…", "What to know about…", "Why…", "How…", "explained", \
  "what it means", "the real story behind"
- Speculative / forward-looking pieces with no confirmed action: signals are \
  "may", "could", "might", "is expected to", "is likely to" with no named \
  entity taking a confirmed action.
- Any headline where you would need to invent a company or commodity name to \
  create a seed — if the seed is not stated, exclude it.
- Press-release / wire NOISE (these dominate PR Newswire / GlobeNewswire): \
  product/wellness/supplement promos, "Top 100 …" lists, content-marketing \
  ("X Article Examines …", "HelloNation"), award/recognition puffery, and \
  shareholder-lawsuit solicitations ("Investors with Losses", "Deadline", \
  "class action", "investigation"). EXCLUDE all of these.
- Micro-cap / obscure-company actions: the tool's graph covers MAJOR public \
  companies (S&P 500 + large global caps). If the named company is small or \
  obscure and not a globally recognizable public firm, EXCLUDE it — a seed that \
  isn't in the graph cannot propagate. Prefer well-known names.

DIVERSITY RULE: if one company or story appears in many raw headlines, \
include at most 2 of them (the most concrete/actionable). Do not let a \
single IPO, earnings cycle, or ongoing event crowd out other qualifying news.

REWRITE RULES — apply to every headline you keep:
- ≤15 words
- Who + what only. Neutral verbs: announces, reports, rises, falls, cuts, \
  acquires, approves, launches, signs, halts, closes, opens, raises, reduces.
- No loaded words: rattles, warns, fears, surges, soars, plummets, looms, \
  threatens, roils, jolts, shocks, crisis, turmoil, chaos, sparks, storms, \
  fragile, in jeopardy, at risk, braces, reeling, scrambles, reportedly, \
  allegedly, sources say.
- No judgment adjectives: massive, alarming, stunning, historic, unprecedented, \
  fragile, troubled, controversial, sweeping, dramatic.
- Keep specific numbers (prices, %, quantities) — they are facts.
- Do NOT invent facts not in the original headline.

QUALITY BAR (strict — do NOT pad): return ONLY items that pass EVERY rule above. \
There is NO target count. 1–3 strong items is a correct, expected result, and an \
empty array [] is correct when nothing qualifies. NEVER include a borderline or \
excluded item to reach a number. A short, clean brief is better than a padded one. \
Quality over quantity, always.

ENTITY: for each kept headline, also output `entity` — the SINGLE most specific \
seed the tool can anchor on: the primary named public company, commodity, or \
region the headline is about (e.g. "Nvidia", "Crude Oil", "Europe"), using its \
common name. If the headline has no such concrete, nameable entity, EXCLUDE it.

Return ONLY a valid JSON array — no markdown, no other text:
[{{"text": "<rewritten headline ≤15 words>", "source": "<outlet>", "url": "<url>", "entity": "<primary company/commodity/region>"}}]

Raw headlines:
{headlines}
"""

# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


# Articles older than this are dropped before Claude ever sees them.
# 2 days keeps the brief feeling fresh (5 was letting 4-day-old ADP/deal
# stories recirculate and appear identical to previous days).
_MAX_ARTICLE_AGE_DAYS = 2


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


def _fmt_pub_date(pub_dt: datetime | None) -> str | None:
    """Format a pubDate as a UTC calendar day ("YYYY-MM-DD"), or None.

    Converts to UTC first so a tz-offset item near midnight (e.g. +13:00) isn't
    stamped a day ahead — a day-ahead stamp would win max recency weight
    downstream. Tz-naive datetimes are assumed to already be UTC.
    """
    if pub_dt is None:
        return None
    if pub_dt.tzinfo is None:
        pub_dt = pub_dt.replace(tzinfo=timezone.utc)
    return pub_dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


# Per-feed and overall caps. We sample up to _PER_FEED items from EACH feed and
# round-robin them into the final list, so a single high-volume feed (Yahoo) can't
# crowd out the high-signal seeded feeds (SEC/Fed/Investing) — the old sequential
# "fill to 40 then break" let later feeds never be reached.
_PER_FEED = int(os.environ.get("NEWS_PER_FEED", "8"))
_MAX_ITEMS = int(os.environ.get("NEWS_MAX_ITEMS", "60"))


def _fetch_raw(max_items: int = _MAX_ITEMS) -> list[dict[str, Any]]:
    """Pull headlines from every RSS feed, up to _PER_FEED each, then round-robin
    them into a diverse list capped at max_items.

    Articles whose <pubDate> is older than _MAX_ARTICLE_AGE_DAYS are dropped at the
    feed layer so Claude never sees stale items. Articles with no parseable pubDate
    are kept (benefit of the doubt — most are same-day). URLs are de-duped across
    feeds so the same wire story doesn't appear twice.
    """
    now = datetime.now(tz=timezone.utc)
    per_feed: list[list[dict[str, Any]]] = []
    for source, url, max_age_days in _RSS_FEEDS:
        cutoff = now - timedelta(days=max_age_days)
        feed_items: list[dict[str, Any]] = []
        try:
            r = requests.get(url, timeout=8, headers=_HEADERS)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for elem in root.iter("item"):
                if len(feed_items) >= _PER_FEED:
                    break
                title = (elem.findtext("title") or "").strip()
                link = _get_link_from_item(elem)
                if not title or not link:
                    continue
                pub_dt = _parse_pub_date(elem)
                if pub_dt is not None and pub_dt < cutoff:
                    log.debug("news: skipping stale item (%s): %s", pub_dt.date(), title[:60])
                    continue
                feed_items.append({
                    "title": title,
                    "source": source,
                    "url": link,
                    # Normalise to UTC before formatting: a tz-offset item near
                    # midnight (e.g. +13:00) would otherwise be stamped a day
                    # ahead, which grants it max recency weight downstream.
                    "pub_date": _fmt_pub_date(pub_dt),
                })
        except Exception as exc:
            log.debug("news: feed %s failed: %s", source, exc)
        per_feed.append(feed_items)

    # Round-robin interleave so every feed is represented even if we hit max_items.
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    depth = 0
    while len(items) < max_items and any(depth < len(f) for f in per_feed):
        for feed_items in per_feed:
            if depth < len(feed_items) and len(items) < max_items:
                it = feed_items[depth]
                if it["url"] in seen_urls:
                    continue
                seen_urls.add(it["url"])
                items.append(it)
        depth += 1
    log.info("news: fetched %d raw items from %d feeds", len(items), len(_RSS_FEEDS))
    return items


def _entity_resolves(conn: sqlite3.Connection, name: str) -> bool:
    """True if `name` matches a node of ANY type by name or alias — exact, then
    starts-with, then contains. Broader than the impact engine's Company-only
    resolver so commodities/regions count too."""
    q = (name or "").lower().strip()
    if not q:
        return False
    if conn.execute("SELECT 1 FROM nodes WHERE LOWER(name) = ? LIMIT 1", (q,)).fetchone():
        return True
    if conn.execute("SELECT 1 FROM aliases WHERE alias_normalized = ? LIMIT 1", (q,)).fetchone():
        return True
    if conn.execute("SELECT 1 FROM nodes WHERE LOWER(name) LIKE ? LIMIT 1", (q + "%",)).fetchone():
        return True
    if len(q) >= 5 and conn.execute(
        "SELECT 1 FROM nodes WHERE LOWER(name) LIKE ? LIMIT 1", (f"%{q}%",)
    ).fetchone():
        return True
    return False


def _graph_gate(headlines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop headlines whose primary `entity` can't be resolved to a graph node —
    a brief item that can't seed a trace is noise (e.g. SpaceX, micro-cap M&A).
    Fail-open if the graph DB is unavailable so a missing file can't blank the
    brief."""
    if not _DB_PATH.exists():
        return headlines
    try:
        conn = sqlite3.connect(_DB_PATH)
    except Exception as exc:
        log.warning("news: graph-gate could not open DB (%s); keeping unfiltered", exc)
        return headlines
    kept: list[dict[str, Any]] = []
    try:
        for h in headlines:
            ent = h.get("entity", "")
            if ent and _entity_resolves(conn, ent):
                kept.append(h)
            else:
                log.info("news: graph-gate dropped %r (entity %r not in graph)",
                         h.get("text", "")[:60], ent)
    finally:
        conn.close()
    return kept


def _filter_with_claude(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ask Claude to pick + trim the top economic headlines, then graph-gate."""
    headlines_block = "\n".join(
        # Include pub_date so Claude can reason about recency precisely.
        f"{i + 1}. [{item['source']}] [published: {item.get('pub_date') or 'unknown'}] "
        f"{item['title']}  URL: {item['url']}"
        for i, item in enumerate(raw)
    )
    today_str = date.today().strftime("%Y-%m-%d")
    prompt = _FILTER_PROMPT.format(headlines=headlines_block, today=today_str)
    # Generous timeout: with ~40+ raw items the filter call runs ~90s; the old
    # 120s default risked timing out (→ empty → raw fallback) as volume grew.
    text = _claude_call(prompt, timeout=180)
    if not text:
        # The Claude CLI itself failed (non-zero exit, timeout, or 401 auth).
        # Raise so the caller treats this as UNAVAILABLE (and does not cache) —
        # distinct from a successful-but-empty result (a quiet/all-gated day),
        # which legitimately returns []. We must NOT fall back to raw, unfiltered
        # headlines here: that's what served opinion/obituary/micro-cap junk.
        raise RuntimeError("Claude CLI returned nothing (likely auth/timeout)")
    # Claude frequently wraps the JSON array in conversational prose ("Most of
    # this batch is opinion… Two qualify:\n\n[…]") or markdown fences. A strict
    # json.loads on that preamble throws, which silently dropped us to the RAW
    # (unfiltered, 15-word-truncated) fallback — letting opinion/analysis through
    # and cutting headlines mid-sentence. Reuse the impact engine's tolerant
    # extractor: it strips fences/special tokens and scans for the first balanced
    # [ … ] block.
    from api.impact import _parse_llm_json
    parsed = _parse_llm_json(text)
    if isinstance(parsed, dict) and "results" in parsed:
        parsed = parsed["results"]
    if not isinstance(parsed, list):
        log.warning("news: could not extract a JSON array from Claude output; head=%s", text[:200])
        return []
    # Build a URL → pub_date lookup from the raw items so we can restore
    # the article date on each filtered headline (the LLM doesn't return it).
    url_to_date: dict[str, str | None] = {
        item["url"]: item.get("pub_date") for item in raw
    }
    result = []
    for h in parsed:
        if isinstance(h, dict) and h.get("text"):
            url = str(h.get("url", "")).strip()
            result.append({
                "text":     str(h["text"]).strip(),
                "source":   str(h.get("source", "")).strip(),
                "url":      url,
                "pub_date": url_to_date.get(url),   # "YYYY-MM-DD" or None
                "entity":   str(h.get("entity", "")).strip(),
            })
    # Graph-gate: keep only headlines whose primary entity is a node we can seed.
    result = _graph_gate(result)
    return result[:10]  # cap at 10 — frontend paginates to 5 / 10


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

    # The brief is only ever the filtered + graph-gated set (possibly empty on a
    # quiet day). We deliberately do NOT fall back to raw, unfiltered headlines —
    # that previously served opinion/obituary/micro-cap junk whenever Claude was
    # slow or unauthenticated. If the Claude CLI is unavailable, return an empty
    # brief and DO NOT cache it, so the next call retries once the CLI recovers.
    try:
        filtered = _filter_with_claude(raw)
    except Exception as exc:
        log.warning("news: filter unavailable (%s); returning empty brief (not cached)", exc)
        return []

    _cache[today] = filtered   # cache successes only (incl. a valid empty result)
    return filtered
