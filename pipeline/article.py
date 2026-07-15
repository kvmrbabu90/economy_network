"""So What? V2 — article fetch + deterministic reduction.

Two durable, LLM-free layers behind the enrichment stage (pipeline/enrich.py):

  fetch_article(url)      -> (html | None, status)   cached to disk, never re-fetched
  reduce_html(html, seed) -> reduced signal text     ~5000 tokens -> ~300, zero LLM

`reduce_html` is a PURE function (unit-tested without network). `fetch_article`
mirrors gkg.download_slice: gzip cache + a typed negative-cache sidecar so a
paywall / 404 is remembered and never re-hit, with a TTL for transient failures.
Politeness: descriptive User-Agent + timeout + per-domain rate-limit.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from pipeline import gkg

log = logging.getLogger(__name__)

# Cache OUTSIDE OneDrive (WAL/sync-safe, visible to the scheduled cycle) — mirrors
# the DB + gkg cache locations. Overridable for tests.
ARTICLE_CACHE_DIR = Path(os.environ.get("ARTICLE_CACHE_DIR")
                         or (Path(os.environ.get("LOCALAPPDATA") or "/tmp") / "econgraph" / "article_cache"))
ARTICLE_TIMEOUT_S = float(os.environ.get("ARTICLE_TIMEOUT_S", "12"))
ARTICLE_NEG_TTL_S = float(os.environ.get("ARTICLE_NEG_TTL_S", str(7 * 24 * 3600)))  # transient failures expire
ARTICLE_MIN_CHARS = int(os.environ.get("ARTICLE_MIN_CHARS", "400"))  # below this after reduce = paywall/js stub
_PER_DOMAIN_MIN_INTERVAL_S = float(os.environ.get("ARTICLE_DOMAIN_INTERVAL_S", "0.5"))

# Boilerplate containers to drop before extracting main text.
_BOILERPLATE_TAGS = ("script", "style", "nav", "header", "footer", "aside",
                     "form", "noscript", "svg", "button", "iframe", "figure")

# Surface verbs that mark a concrete business EVENT — used to prioritise sentences.
# (Distinct from gkg.HARD_EVENT_THEMES, which are GDELT theme CODES, not surface text.)
_EVENT_VERB_RE = re.compile(
    r"\b(beat|beats|miss|misses|missed|guidance|acquir\w*|merg\w+|takeover|buyout|"
    r"stake|recall\w*|lawsuit|settle\w*|fine[sd]?|sanction\w*|tariff\w*|approv\w*|"
    r"reject\w*|ban\w*|layoff\w*|job cuts|restructur\w*|bankrupt\w*|default\w*|"
    r"strike\w*|shutdown|halt\w*|disrupt\w*|shortage\w*|recall|contract|award\w*|"
    r"unveil\w*|launch\w*|cut\w*|raise[sd]?|slash\w*|surg\w*|plung\w*|jump\w*|"
    r"fell|fall\w*|drop\w*|rose|rise[sn]?|profit|revenue|loss\w*|earnings|dividend|"
    r"trial|debut|breach|cyber\w*|resign\w*|appoint\w*)\b", re.I)
_NUMBER_RE = re.compile(r"[\$€£¥₩₹]\s?\d|\b\d+(\.\d+)?\s?(%|percent|bn|billion|million|trillion|cr|crore|lakh)\b|\b\d{2,}\b")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“])")
_WS_RE = re.compile(r"\s+")
# Prompt-injection / control-char guard: page text is attacker-influenceable DATA,
# never instructions. Strip control chars and neutralise obvious instruction bait.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_INJECT_RE = re.compile(
    r"(?i)("
    r"ignore(?:\s+\w+){0,4}\s+(?:instructions|prompt|rules)|"
    r"disregard(?:\s+\w+){0,4}\s+(?:instructions|prompt|above|rules)|"
    r"system\s+prompt|you\s+are\s+now|new\s+instructions|assistant\s*:|\bsudo\b"
    r")")
# A body shorter than this is a paywall / JS stub / non-article, not signal.
_MIN_BODY_CHARS = int(os.environ.get("ARTICLE_MIN_BODY_CHARS", "200"))


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", _CTRL_RE.sub(" ", text or "")).strip()


def _content_root(soup: BeautifulSoup):
    """Pick the element most likely to hold the article body: <article> or <main>
    if present, else the block maximising paragraph-text density (readability-lite)."""
    for sel in ("article", "main"):
        el = soup.find(sel)
        if el and len(el.get_text(" ", strip=True)) > 200:
            return el
    best, best_score = soup.body or soup, 0
    for el in (soup.body or soup).find_all(["div", "section"], recursive=True):
        ps = el.find_all("p", recursive=False)
        score = sum(len(p.get_text(" ", strip=True)) for p in ps)
        if score > best_score:
            best, best_score = el, score
    return best


def reduce_html(html: str, seed_names: Optional[list[str]] = None, *,
                max_sentences: int = 8, max_chars: int = 1200) -> str:
    """PURE: HTML -> a short 'signal' text. Strips boilerplate, then keeps the lede
    plus the sentences that carry the actual news: any naming the seed entity, a
    money/percent/number, or a hard-event verb. ~10x smaller than the full body and
    zero tokens. Returns '' when there is no usable body (paywall / JS stub)."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_BOILERPLATE_TAGS):
        tag.decompose()
    root = _content_root(soup)
    paras = [_clean(p.get_text(" ", strip=True)) for p in root.find_all("p")]
    body = " ".join(p for p in paras if len(p) > 40) or _clean(root.get_text(" ", strip=True))
    if len(body) < _MIN_BODY_CHARS:
        return ""      # paywall / JS stub / non-article — caller falls back to headline+gkg
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(body) if len(s.strip()) > 20]
    if not sentences:
        sentences = [body[:max_chars]]

    seeds = [s.lower() for s in (seed_names or []) if s]
    picked: list[tuple[int, int]] = []   # (priority, index) — lower priority = kept first
    for i, s in enumerate(sentences):
        sl = s.lower()
        has_seed = any(name in sl for name in seeds)
        has_num = bool(_NUMBER_RE.search(s))
        has_verb = bool(_EVENT_VERB_RE.search(s))
        if i < 2:
            pri = 0                         # lede always kept
        elif has_seed and has_num:
            pri = 1
        elif has_seed:
            pri = 2
        elif has_num and has_verb:
            pri = 3
        elif has_verb:
            pri = 4
        else:
            continue                        # no signal — drop
        picked.append((pri, i))

    picked.sort(key=lambda t: (t[0], t[1]))
    chosen_idx = sorted(i for _, i in picked[:max_sentences])
    out, total = [], 0
    for i in chosen_idx:
        s = sentences[i]
        if total + len(s) > max_chars:
            break
        out.append(s)
        total += len(s) + 1
    reduced = " ".join(out)
    # Final injection guard: neutralise instruction-bait phrases in the DATA.
    return _INJECT_RE.sub("[redacted]", reduced)


# ---------------------------------------------------------------------------
# Fetch + cache
# ---------------------------------------------------------------------------

_last_hit: dict[str, float] = {}   # domain -> monotonic ts, for polite rate-limiting


def _paths(url: str) -> tuple[Path, Path]:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    shard = ARTICLE_CACHE_DIR / h[:2]
    return shard / f"{h}.html.gz", shard / f"{h}.meta.json"


def _read_meta(meta_path: Path) -> Optional[dict]:
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_meta(meta_path: Path, status: str) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"status": status, "ts": time.time()}), encoding="utf-8")
    os.replace(tmp, meta_path)


def _rate_limit(domain: str) -> None:
    now = time.monotonic()
    last = _last_hit.get(domain, 0.0)
    wait = _PER_DOMAIN_MIN_INTERVAL_S - (now - last)
    if wait > 0:
        time.sleep(wait)
    _last_hit[domain] = time.monotonic()


def fetch_article(url: Optional[str]) -> tuple[Optional[str], str]:
    """Return (html, status). Cache-first: a cached body is returned without a
    network hit; a remembered negative result (paywall/404/…) short-circuits unless
    it was a transient failure past its TTL. status ∈ {ok, cached, no_url, 404, 403,
    paywall, non_html, timeout, error}."""
    if not url or not url.startswith(("http://", "https://")):
        return None, "no_url"
    html_path, meta_path = _paths(url)
    corrupt_body = False
    if html_path.exists():
        try:
            return gzip.decompress(html_path.read_bytes()).decode("utf-8", "replace"), "cached"
        except Exception:
            # Corrupt / truncated cache body -> drop it and refetch. Without unlinking, the
            # meta short-circuit below (status "ok") would return (None, "ok") forever and
            # the URL would be permanently unreadable.
            corrupt_body = True
            try:
                html_path.unlink()
            except OSError:
                pass
    meta = _read_meta(meta_path)
    # A negative-cache meta suppresses a refetch — but ONLY for genuinely negative statuses.
    # A meta of "ok" means "we have a body"; if we reached here the body is missing or was
    # just found corrupt, so an "ok" meta must NOT short-circuit — fall through and refetch.
    if meta and not corrupt_body and meta.get("status") not in {"ok", "cached"}:
        transient = meta.get("status") in {"timeout", "error", "403"}
        if not transient or (time.time() - meta.get("ts", 0)) < ARTICLE_NEG_TTL_S:
            return None, meta.get("status", "error")

    domain = (urlsplit(url).hostname or "").lower()
    _rate_limit(domain)
    try:
        resp = requests.get(url, headers={"User-Agent": gkg.gkg_user_agent(),
                                          "Accept": "text/html,application/xhtml+xml"},
                            timeout=ARTICLE_TIMEOUT_S, allow_redirects=True)
    except requests.Timeout:
        _write_meta(meta_path, "timeout"); return None, "timeout"
    except Exception as exc:
        log.debug("fetch_article %s: %s", url, exc)
        _write_meta(meta_path, "error"); return None, "error"

    if resp.status_code == 404:
        _write_meta(meta_path, "404"); return None, "404"
    if resp.status_code in (401, 402, 403):
        _write_meta(meta_path, "403"); return None, "403"
    ctype = resp.headers.get("Content-Type", "")
    if "html" not in ctype.lower():
        _write_meta(meta_path, "non_html"); return None, "non_html"
    if not resp.ok:
        _write_meta(meta_path, "error"); return None, "error"

    html = resp.text
    html_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = html_path.with_suffix(".gz.tmp")
    tmp.write_bytes(gzip.compress(html.encode("utf-8", "replace")))
    os.replace(tmp, html_path)
    _write_meta(meta_path, "ok")
    return html, "ok"


def prune_article_cache(keep_days: float = 30.0) -> int:
    """Delete cached bodies + meta older than keep_days. Mirrors gkg.prune_slice_cache."""
    if not ARTICLE_CACHE_DIR.exists():
        return 0
    cutoff = time.time() - keep_days * 24 * 3600
    n = 0
    for p in ARTICLE_CACHE_DIR.rglob("*"):
        if p.is_file() and p.stat().st_mtime < cutoff:
            try:
                p.unlink(); n += 1
            except OSError:
                pass
    return n
