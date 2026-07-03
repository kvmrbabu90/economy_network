"""GDELT DOC 2.0 API client — a polite, rate-limited news-article search.

So What? V2 uses this as a **targeted per-node** source: instead of a blind
global firehose, `ingest_news.fetch_gdelt` queries GDELT for each high-centrality
graph node *by name*, so the seed node is already known — sidestepping GDELT's
biggest weakness (entity disambiguation has no ticker/CIK mapping) and ours
(fuzzy name resolution) at once.

GDELT is open and unrestricted (no API key). It IS rate-limited on their side to
protect their clusters and now requires a descriptive ``User-Agent``; we honor
both with a process-wide min-interval throttle (mirroring ``pipeline.sec``) and a
UA reused from ``EDGAR_USER_AGENT``. High recall / low precision is expected —
the caller's materiality LLM gate, dedupe, and per-cycle cap do the noise
control, exactly as GDELT's own docs prescribe.

Docs: https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"

# GDELT's own 429 body states: "limit requests to one every 5 seconds." Honor it.
_DEFAULT_MIN_INTERVAL_S = 5.0
_DEFAULT_TIMEOUT_S = 15.0


def gdelt_user_agent() -> str:
    """Descriptive User-Agent — GDELT now requires one.

    Reuse ``EDGAR_USER_AGENT`` (a real name + email) as the project's contact
    string, matching how ``pipeline.wikidata`` reuses it for Wikidata's UA
    policy. Falls back to a static descriptive default so GDELT (unlike SEC)
    never hard-fails on a missing env var.
    """
    return (
        os.environ.get("GDELT_USER_AGENT")
        or os.environ.get("EDGAR_USER_AGENT")
        or "EconGraph/1.0 (kondaru.mk@gmail.com)"
    ).strip()


class _Throttle:
    """Thread-safe process-wide min-interval limiter.

    Mirrors ``pipeline.sec._GlobalRateLimiter`` but reads the interval from the
    environment on each acquire so tests can disable pacing with
    ``GDELT_MIN_INTERVAL_S=0`` and production can tune it live.
    """

    def __init__(self) -> None:
        self._last = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        try:
            interval = float(os.environ.get("GDELT_MIN_INTERVAL_S", _DEFAULT_MIN_INTERVAL_S))
        except (TypeError, ValueError):
            interval = _DEFAULT_MIN_INTERVAL_S
        if interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = interval - (now - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


# Module-level singleton: ALL GDELT requests in this process share the pacing.
_LIMITER = _Throttle()


def seendate_to_date(seendate: Optional[str]) -> Optional[str]:
    """GDELT ``seendate`` (``YYYYMMDDTHHMMSSZ``) → ``YYYY-MM-DD``. None if unparseable."""
    s = (seendate or "").strip()
    if len(s) >= 8 and s[:8].isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return None


def _build_query(entity: str, *, extra: str = "") -> str:
    """Phrase-match the entity name, English sources only.

    Double-quotes inside the name are replaced with spaces so they can't break
    GDELT's quoted-phrase syntax. ``extra`` appends caller-supplied operators
    (e.g. a theme/tone filter) verbatim.
    """
    name = (entity or "").replace('"', " ").strip()
    query = f'"{name}" sourcelang:english'
    if extra:
        query = f"{query} {extra}"
    return query


def _timeout() -> float:
    try:
        return float(os.environ.get("GDELT_TIMEOUT_S", _DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S


def gdelt_search(
    entity: str,
    *,
    timespan: str = "1h",
    maxrecords: int = 10,
    extra_query: str = "",
) -> list[dict[str, Any]]:
    """One DOC 2.0 ``artlist`` query for ``entity``.

    Returns raw article dicts with keys ``url, title, domain, seendate,
    language, sourcecountry, published_at``. An empty/blank entity returns ``[]``
    without a request.

    A **non-JSON or empty 200 body** — GDELT's shape when a query has no results —
    is treated as no results (``[]``). Transport and HTTP errors (timeouts,
    non-2xx via ``raise_for_status``) propagate: the caller (``fetch_gdelt``)
    wraps each query so one bad entity can't kill the batch, and
    ``run_ingest``'s per-fetcher guard is the final backstop.
    """
    if not (entity or "").strip():
        return []
    params = {
        "query": _build_query(entity, extra=extra_query),
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(max(1, min(250, int(maxrecords)))),
        "timespan": timespan,
        "sort": "datedesc",
    }
    headers = {"User-Agent": gdelt_user_agent()}
    # GDELT rate-limits aggressively and asks clients to respect a 429 with a
    # backoff (blog.gdeltproject.org/ukraine-api-rate-limiting...). Retry once
    # after a short sleep; a persistent 429 propagates to the caller's guard.
    try:
        backoff = float(os.environ.get("GDELT_BACKOFF_S", "5"))
    except (TypeError, ValueError):
        backoff = 5.0
    resp = None
    for attempt in range(2):
        _LIMITER.acquire()
        resp = requests.get(GDELT_DOC_URL, params=params, headers=headers, timeout=_timeout())
        if resp.status_code == 429 and attempt == 0:
            log.warning("gdelt: 429 Too Many Requests for %r; backing off %.1fs", entity, backoff)
            if backoff > 0:
                time.sleep(backoff)
            continue
        break
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        # No-result queries (and some transient errors) return HTML or an empty
        # body with a 200 — not an exception, just "nothing found".
        log.debug("gdelt: non-JSON response for %r (%d bytes)", entity, len(resp.text or ""))
        return []
    articles = data.get("articles", []) if isinstance(data, dict) else []
    out: list[dict[str, Any]] = []
    for art in articles:
        if not isinstance(art, dict):
            continue
        url = (art.get("url") or "").strip()
        title = (art.get("title") or "").strip()
        if not url or not title:
            continue   # an article with no URL or title is unusable as an event
        seendate = (art.get("seendate") or "").strip()
        out.append({
            "url": url,
            "title": title,
            "domain": (art.get("domain") or "").strip(),
            "seendate": seendate,
            "language": (art.get("language") or "").strip(),
            "sourcecountry": (art.get("sourcecountry") or "").strip(),
            "published_at": seendate_to_date(seendate),
        })
    return out
