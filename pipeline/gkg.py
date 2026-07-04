"""GDELT 2.0 GKG (Global Knowledge Graph) bulk client + parser.

The So What? V2 ingestion pulls the 15-minute GKG update files instead of the
per-node DOC 2.0 API. Each GKG record ships GDELT's pre-extracted organizations,
themes, tone, amounts, and sharing-image — the raw material for a cheap, LLM-free
relevance/noise/dedup cascade over the whole firehose at once (see
`pipeline.ingest_news.fetch_gkg_bulk`).

Facts confirmed live (2026-07-03):
- The files are static objects on a Google-Cloud-Storage-backed CDN
  (data.gdeltproject.org serves a *.storage.googleapis.com cert), NOT the
  rate-limited hosted API — so use plain **http** (an https-upgrading client
  fails the TLS SNI check) and there is no meaningful rate limit; only one new
  15-min slice exists at a time.
- A GKG slice is ~3-6 MB zipped / ~600-1,400 records, tab-delimited with 27
  columns (despite the .csv name).

Codebook: http://data.gdeltproject.org/documentation/GDELT-Global_Knowledge_Graph_Codebook-V2.1.pdf
"""
from __future__ import annotations

import html
import logging
import os
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

import requests

log = logging.getLogger(__name__)

GKG_BASE = "http://data.gdeltproject.org/gdeltv2"
LASTUPDATE_URL = f"{GKG_BASE}/lastupdate.txt"

# 0-indexed positions of the columns we use (GKG 2.1, 27 tab-separated fields).
_COL_RECORDID = 0
_COL_DATE = 1
_COL_SRCCOLL = 2       # SourceCollectionIdentifier: 1 == open web (URL in DocumentIdentifier)
_COL_DOMAIN = 3        # SourceCommonName
_COL_URL = 4           # DocumentIdentifier
_COL_THEMES_V1 = 7     # semicolon-delimited theme codes (no offsets)
_COL_THEMES_V2 = 8     # "CODE,offset;..."
_COL_ORGS_V1 = 13      # semicolon-delimited org surface names
_COL_ORGS_V2 = 14      # "Name,offset;..."
_COL_TONE = 15         # "tone,pos,neg,polarity,activity,selfgroup,wordcount"
_COL_SHARINGIMAGE = 18
_COL_AMOUNTS = 24      # "amount,object,offset;..."
_COL_TRANSLATION = 25
_COL_EXTRAS = 26       # XML incl. <PAGE_TITLE>...</PAGE_TITLE>
_MIN_COLS = 27

_TIMEOUT_S = float(os.environ.get("GKG_TIMEOUT_S", "60"))

# Corporate suffixes stripped from both node names and GKG org surface strings so
# "Procter & Gamble Co" and "procter and gamble" collapse to one normalized key.
CORP_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "companies",
    "ltd", "limited", "plc", "llc", "lp", "llp", "group", "holdings", "holding",
    "sa", "ag", "nv", "se", "spa", "ab", "as", "oyj", "kk", "gmbh", "bv", "pjsc",
    "the",
}

# Single-token node names that are ALSO everyday English words: a match on one of
# these needs the offset-proximity gate (org near a business theme/amount) before
# we trust it. Curated (a length rule wrongly flags Sony/IBM/Intel/FedEx).
COMMON_WORDS = {
    "shell", "gap", "block", "match", "apple", "target", "gold", "square", "visa",
    "snap", "box", "core", "arm", "key", "care", "fund", "good", "main", "next",
    "unity", "strategy", "science", "open", "first", "class", "general", "standard",
    "public", "national", "global", "express", "discover", "progress", "oracle",
    "pioneer", "dollar", "guess", "coach", "sage", "sky", "sun", "star", "wave",
    "peak", "edge", "flow", "bond", "trust", "capital", "power", "energy", "motion",
    "vision", "focus", "prime", "delta", "alpha", "nova", "pulse", "spark", "zoom",
    "live", "love", "now", "all", "one", "big", "new", "way", "day", "up", "out",
    "red", "blue", "green", "city", "world", "go", "on", "it", "by", "am", "car",
    "cash", "best", "clear", "fair", "fast", "free", "rich", "smart", "sound",
    "intel", "meta", "public", "state", "united", "national",
}

# Stock exchanges / indices: an org NER match on these is almost always an
# incidental "NASDAQ:TICKER"-style reference, not news about the exchange itself,
# so they flood the matcher with boilerplate. Skip them as seeds.
EXCHANGE_NAMES = {
    "nasdaq", "nyse", "nyse american", "dow jones", "s p", "ftse", "nikkei",
    "cboe", "lse", "otc", "amex",
    # NOTE: bare "dow" is intentionally NOT here — it's the real company Dow Inc.
}

# Themes that signal a market-moving / business event. Prefix families ECON_ and
# EPU_ plus concrete hard-event codes. WB_ is deliberately EXCLUDED (its taxonomy
# is governance/health-heavy and dominated our calibration noise).
_BUSINESS_PREFIXES = ("ECON_", "EPU_")
HARD_EVENT_THEMES = {
    "ECON_BANKRUPTCY", "ECON_EARNINGSREPORT", "ECON_IPO", "ECON_STOCKMARKET",
    "ECON_SUBSIDIES", "ECON_TRADE_DISPUTE", "ECON_MONOPOLY", "ECON_NATIONALIZE",
    "ECON_BOYCOTT", "ECON_MONEYLAUNDERING", "STRIKE", "SANCTIONS", "ARMEDCONFLICT",
    "MANMADE_DISASTER", "NATURAL_DISASTER", "TAX_DISEASE",
}

# Whole-word currency/market tokens. Matched on word boundaries — a substring
# test would class "Boston"/"cotton" (contain "ton") or "eureka" (contains "eur")
# as monetary and poison the materiality signal.
_CURRENCY_WORDS = {
    "usd", "eur", "gbp", "jpy", "cny", "chf", "cad", "aud", "inr", "krw", "brl",
    "dollar", "dollars", "euro", "euros", "yen", "yuan", "renminbi", "won", "rupee",
    "rupees", "franc", "francs", "peso", "pesos", "krona", "krone",
    "pound", "pounds", "sterling", "rand", "ruble", "rouble", "lira", "crore", "lakh",
    # 'real'/'reais' (Brazilian) omitted — collides with "real estate"/"for real".
    "billion", "million", "trillion", "revenue", "sales", "profit", "profits",
    "deal", "fine", "penalty", "writedown", "buyback", "dividend", "dividends",
    "barrel", "barrels", "tonne", "tonnes", "ton", "tons",
}
_WORD_RE = re.compile(r"[a-z]+")
_CURRENCY_SYMBOLS = ("$", "€", "£", "¥", "₩", "₹", "₽", "₨", "﷼")


def gkg_user_agent() -> str:
    """Reuse EDGAR_USER_AGENT (a real name+email) as the contact string, like the
    DOC client and pipeline.wikidata do; static fallback so it never hard-fails."""
    return (
        os.environ.get("GKG_USER_AGENT")
        or os.environ.get("EDGAR_USER_AGENT")
        or "EconGraph/1.0 (kondaru.mk@gmail.com)"
    ).strip()


# ---------------------------------------------------------------------------
# Slice discovery + download
# ---------------------------------------------------------------------------

def latest_gkg_url() -> Optional[tuple[str, str]]:
    """Poll lastupdate.txt → (timestamp, gkg_zip_url) for the newest slice, or None.

    Each line is '<size> <md5> <url>'; we want the line whose url ends gkg.csv.zip.
    """
    resp = requests.get(LASTUPDATE_URL, headers={"User-Agent": gkg_user_agent()}, timeout=_TIMEOUT_S)
    resp.raise_for_status()
    for line in resp.text.splitlines():
        parts = line.split()
        if parts and parts[-1].endswith("gkg.csv.zip"):
            url = parts[-1]
            ts = url.rsplit("/", 1)[-1].split(".", 1)[0]  # YYYYMMDDHHMMSS
            if len(ts) == 14 and ts.isdigit():            # ignore a garbage/HTML line
                return ts, url
    return None


def gkg_slice_url(ts: str) -> str:
    """Deterministic slice URL from a 14-digit YYYYMMDDHHMMSS timestamp."""
    return f"{GKG_BASE}/{ts}.gkg.csv.zip"


def previous_slice_ts(ts: str) -> str:
    """The 15-minutes-earlier slice timestamp (pure integer clock math, no tz libs).

    Works on YYYYMMDDHHMMSS strings aligned to :00/:15/:30/:45. Handles minute,
    hour, and day rollover via a day-length table (months/years roll rarely enough
    that we accept a simple, dependency-free walk back through the calendar)."""
    y, mo, d, h, mi = int(ts[0:4]), int(ts[4:6]), int(ts[6:8]), int(ts[8:10]), int(ts[10:12])
    mi -= 15
    if mi < 0:
        mi += 60
        h -= 1
        if h < 0:
            h += 24
            d -= 1
            if d < 1:
                mo -= 1
                if mo < 1:
                    mo = 12
                    y -= 1
                d = _days_in_month(y, mo)
    return f"{y:04d}{mo:02d}{d:02d}{h:02d}{mi:02d}00"


def _days_in_month(y: int, mo: int) -> int:
    if mo == 2:
        leap = (y % 4 == 0 and y % 100 != 0) or (y % 400 == 0)
        return 29 if leap else 28
    return 30 if mo in (4, 6, 9, 11) else 31


def recent_slice_timestamps(latest_ts: str, n: int) -> list[str]:
    """The newest n slice timestamps, newest first, walking back from latest_ts."""
    out = [latest_ts]
    for _ in range(max(0, n - 1)):
        out.append(previous_slice_ts(out[-1]))
    return out


def download_slice(url: str, cache_dir: Path) -> Optional[Path]:
    """Cache-first download of a .gkg.csv.zip → local path (invariant #6: never
    re-fetch a VALID cached file).

    Hardened for an unattended job: a cached file is trusted only if it is a valid
    zip (a partial/corrupt file left by a killed mid-write is re-downloaded, not
    poisoned forever); the download is written atomically (temp + os.replace) and
    zip-verified before it becomes the cache entry. Returns None on a 404 (slice
    not yet published) or ANY transport failure (caller isolates the slice)."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    name = url.rsplit("/", 1)[-1]
    out = cache_dir / name
    if out.exists() and out.stat().st_size > 0:
        if zipfile.is_zipfile(out):
            return out
        log.warning("gkg: cached slice %s is corrupt; re-downloading", name)
        out.unlink(missing_ok=True)
    try:
        resp = requests.get(url, headers={"User-Agent": gkg_user_agent()}, timeout=_TIMEOUT_S)
        if resp.status_code == 404:
            log.info("gkg: slice not yet published: %s", url)
            return None
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("gkg: download failed for %s: %s", url, exc)
        return None
    tmp = cache_dir / (name + ".part")
    tmp.write_bytes(resp.content)
    if not zipfile.is_zipfile(tmp):
        log.warning("gkg: downloaded slice %s is not a valid zip; discarding", name)
        tmp.unlink(missing_ok=True)
        return None
    os.replace(tmp, out)   # atomic: a killed write leaves only the .part file
    return out


def read_gkg_lines(zip_path: Path) -> list[str]:
    """Unzip a cached .gkg.csv.zip → its rows.

    Splits ONLY on newline (str.splitlines() would also break on a bare CR / NEL /
    line-separator that survives inside a scraped <PAGE_TITLE> or org surface, which
    would shred a valid 27-col row into sub-27-col fragments and silently drop it).
    Raises BadZipFile / ValueError on a corrupt or empty zip — the caller isolates
    the slice and download_slice re-fetches it next cycle."""
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if not names:
            raise ValueError("empty gkg zip")
        raw = z.read(names[0])
    text = raw.decode("utf-8", errors="replace")
    return [ln.rstrip("\r") for ln in text.split("\n") if ln.rstrip("\r")]


# ---------------------------------------------------------------------------
# Record parsing
# ---------------------------------------------------------------------------

@dataclass
class GkgRecord:
    record_id: str
    published_at: Optional[str]           # YYYY-MM-DD from the DATE column
    is_translingual: bool                 # record id has a '-T' block (non-English)
    is_web: bool                          # SourceCollectionIdentifier == 1
    domain: str
    url: str
    themes: list[str] = field(default_factory=list)
    v2themes: list[tuple[str, int]] = field(default_factory=list)
    orgs: list[str] = field(default_factory=list)
    v2orgs: list[tuple[str, int]] = field(default_factory=list)
    tone: float = 0.0
    polarity: float = 0.0
    amounts: list[tuple[float, str, int]] = field(default_factory=list)
    sharing_image: str = ""
    title: Optional[str] = None


def _parse_offset_list(field_val: str) -> list[tuple[str, int]]:
    """'Name,offset;Name,offset;...' → [(name, offset)]. Tolerant of junk."""
    out: list[tuple[str, int]] = []
    for block in (field_val or "").split(";"):
        if not block:
            continue
        parts = block.rsplit(",", 1)
        name = parts[0].strip()
        if not name:
            continue
        off = 0
        if len(parts) == 2:
            try:
                off = int(parts[1])
            except ValueError:
                off = 0
        out.append((name, off))
    return out


def _parse_semicolon(field_val: str) -> list[str]:
    return [x.strip() for x in (field_val or "").split(";") if x.strip()]


def _parse_tone(field_val: str) -> tuple[float, float]:
    """(tone, polarity) from the comma list; 0.0 on missing/garbage."""
    parts = (field_val or "").split(",")

    def _f(i: int) -> float:
        try:
            return float(parts[i])
        except (IndexError, ValueError):
            return 0.0

    return _f(0), _f(3)


def _parse_amounts(field_val: str) -> list[tuple[float, str, int]]:
    """'amount,object,offset;...' → [(amount, object, offset)]. Tolerant."""
    out: list[tuple[float, str, int]] = []
    for block in (field_val or "").split(";"):
        if not block:
            continue
        parts = block.split(",")
        if len(parts) < 3:
            continue
        try:
            amt = float(parts[0])
            off = int(parts[-1])
        except ValueError:
            continue
        obj = ",".join(parts[1:-1]).strip()
        out.append((amt, obj, off))
    return out


_TITLE_RE = re.compile(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", re.IGNORECASE | re.DOTALL)
# Collapse control + line-separator chars: a scraped title is attacker-influenceable
# and flows verbatim into the numbered materiality LLM prompt, where an embedded
# CR/NEL/LS would masquerade as extra numbered items (prompt injection).
_TITLE_CTRL_RE = re.compile(r"[\x00-\x1f\x7f\x85  ]+")


def _extract_title(extras: str) -> Optional[str]:
    """Pull the HTML-escaped <PAGE_TITLE> out of the Extras XML (absent for many
    rows — GKG has no first-class title field). Returns unescaped, control-char-
    sanitized text or None."""
    m = _TITLE_RE.search(extras or "")
    if not m:
        return None
    title = _TITLE_CTRL_RE.sub(" ", html.unescape(m.group(1))).strip()
    return title or None


def _date_to_iso(date_val: str) -> Optional[str]:
    s = (date_val or "").strip()
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 and s[:8].isdigit() else None


def parse_gkg(lines: Iterable[str]) -> Iterator[GkgRecord]:
    """Parse GKG 2.1 tab-delimited rows into GkgRecord objects.

    Rows with fewer than 27 columns (truncated/garbage) are skipped rather than
    raising, so one malformed line never aborts a whole slice."""
    for line in lines:
        if not line:
            continue
        f = line.split("\t")
        if len(f) < _MIN_COLS:
            continue
        rid = f[_COL_RECORDID]
        # Record id form 'YYYYMMDDHHMMSS-X' or '...-TX' (T => translingual).
        suffix = rid.split("-", 1)[1] if "-" in rid else ""
        is_t = suffix[:1] == "T"
        tone, polarity = _parse_tone(f[_COL_TONE])
        yield GkgRecord(
            record_id=rid,
            published_at=_date_to_iso(f[_COL_DATE]),
            is_translingual=is_t,
            is_web=f[_COL_SRCCOLL].strip() == "1",
            domain=f[_COL_DOMAIN].strip(),
            url=f[_COL_URL].strip(),
            themes=_parse_semicolon(f[_COL_THEMES_V1]),
            v2themes=_parse_offset_list(f[_COL_THEMES_V2]),
            orgs=_parse_semicolon(f[_COL_ORGS_V1]),
            v2orgs=_parse_offset_list(f[_COL_ORGS_V2]),
            tone=tone,
            polarity=polarity,
            amounts=_parse_amounts(f[_COL_AMOUNTS]),
            sharing_image=f[_COL_SHARINGIMAGE].strip(),
            title=_extract_title(f[_COL_EXTRAS]),
        )


# ---------------------------------------------------------------------------
# Pure matching / relevance helpers
# ---------------------------------------------------------------------------

_NONALNUM = re.compile(r"[^a-z0-9]+")

# Non-decomposable Latin letters that NFKD does NOT split — must be transliterated
# explicitly, else `ascii-ignore` deletes them (Ørsted→'rsted' would never match
# GDELT's romanized 'Orsted'). Keys are lowercased before lookup.
_TRANSLIT = {
    "ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "ð": "d", "þ": "th", "ł": "l",
    "đ": "d", "ħ": "h", "ı": "i", "ĸ": "k", "ŀ": "l", "ŉ": "n", "ſ": "s",
}


def normalize_name(s: str) -> str:
    """Lowercase, punctuation→space, collapse, strip leading/trailing corp suffixes.

    'Procter & Gamble Co.' → 'procter gamble'; 'AT&T Inc' → 'at t'. Empty if the
    string is only punctuation/suffixes. Transliterates non-decomposable Latin
    letters (Ø→o, Æ→ae, ß→ss…) and ASCII-folds combining accents FIRST so a node
    named 'Ørsted'/'Estée Lauder'/'Telefónica' matches GDELT's romanized surface
    form 'Orsted'/'Estee Lauder'/'Telefonica' (else Ø/é become nothing/space)."""
    lowered = "".join(_TRANSLIT.get(ch, ch) for ch in (s or "").lower())
    folded = unicodedata.normalize("NFKD", lowered).encode("ascii", "ignore").decode("ascii")
    s = _NONALNUM.sub(" ", folded).strip()
    toks = [t for t in s.split() if t]
    while toks and toks[-1] in CORP_SUFFIXES:
        toks.pop()
    while toks and toks[0] in CORP_SUFFIXES:
        toks.pop(0)
    return " ".join(toks)


def is_common_word(normalized: str) -> bool:
    """True when a normalized name is a single everyday English word (needs the
    proximity gate before we trust an org→node match on it)."""
    return " " not in normalized and normalized in COMMON_WORDS


def is_business_theme(code: str) -> bool:
    return code.startswith(_BUSINESS_PREFIXES) or code in HARD_EVENT_THEMES


def is_hard_event_theme(code: str) -> bool:
    return code in HARD_EVENT_THEMES


def amount_is_currency(obj: str) -> bool:
    """True when an Amounts object string looks monetary/market-relevant (the
    Amounts field is dominated by non-monetary quantities like 'months'/'kg').
    Matches currency WORDS on boundaries, plus a literal currency symbol."""
    o = (obj or "").lower()
    if any(sym in o for sym in _CURRENCY_SYMBOLS):
        return True
    return bool(_CURRENCY_WORDS & set(_WORD_RE.findall(o)))
