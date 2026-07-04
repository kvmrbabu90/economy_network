from __future__ import annotations

from pathlib import Path

import pytest
import requests

from pipeline import gkg
from pipeline import ingest_news as ing
from schema import store


# ── helper: build a synthetic 27-column GKG 2.1 row ─────────────────────────

def gkg_line(record_id="20260704011500-0", date="20260704011500", srccoll="1",
             domain="reuters.com", url="https://reuters.com/x", themes_v1="",
             themes_v2="", orgs_v1="", orgs_v2="", tone="0,0,0,0,0,0,100",
             sharingimage="", amounts="", extras=""):
    cols = [""] * 27
    cols[0], cols[1], cols[2], cols[3], cols[4] = record_id, date, srccoll, domain, url
    cols[7], cols[8] = themes_v1, themes_v2
    cols[13], cols[14] = orgs_v1, orgs_v2
    cols[15] = tone
    cols[18] = sharingimage
    cols[24] = amounts
    cols[26] = extras
    return "\t".join(cols)


# ── parser ──────────────────────────────────────────────────────────────────

def test_parse_gkg_extracts_all_fields():
    line = gkg_line(
        domain="wsj.com", url="https://wsj.com/a",
        themes_v1="ECON_STOCKMARKET;WB_696_PUBLIC_SECTOR",
        themes_v2="ECON_STOCKMARKET,120;WB_696_PUBLIC_SECTOR,300",
        orgs_v1="walmart;united states",
        orgs_v2="walmart,118;united states,405",
        tone="-6.5,2.1,8.6,10.7,22.5,1.2,412",
        sharingimage="https://img/x.jpg",
        amounts="5000000000,in revenue,90;3,months,600",
        extras="<PAGE_TITLE>Walmart&#39;s big day</PAGE_TITLE>")
    rec = list(gkg.parse_gkg([line]))[0]
    assert rec.is_web and not rec.is_translingual
    assert rec.published_at == "2026-07-04" and rec.domain == "wsj.com" and rec.url == "https://wsj.com/a"
    assert rec.themes == ["ECON_STOCKMARKET", "WB_696_PUBLIC_SECTOR"]
    assert rec.v2themes == [("ECON_STOCKMARKET", 120), ("WB_696_PUBLIC_SECTOR", 300)]
    assert rec.orgs == ["walmart", "united states"]
    assert rec.v2orgs == [("walmart", 118), ("united states", 405)]
    assert rec.tone == -6.5 and rec.polarity == 10.7
    assert rec.sharing_image == "https://img/x.jpg"
    assert (5000000000.0, "in revenue", 90) in rec.amounts
    assert rec.title == "Walmart's big day"          # HTML-unescaped


def test_parse_gkg_flags_translingual_and_nonweb():
    t = list(gkg.parse_gkg([gkg_line(record_id="20260704011500-T3")]))[0]
    assert t.is_translingual
    nonweb = list(gkg.parse_gkg([gkg_line(srccoll="2")]))[0]
    assert not nonweb.is_web


def test_parse_gkg_skips_short_lines():
    assert list(gkg.parse_gkg(["", "a\tb\tc", gkg_line(url="https://ok/1")]))[0].url == "https://ok/1"


def test_parse_gkg_tolerates_garbage_tone_and_amounts():
    rec = list(gkg.parse_gkg([gkg_line(tone="", amounts="junk;5,,;,x,")]))[0]
    assert rec.tone == 0.0 and rec.polarity == 0.0 and rec.amounts == []


def test_extract_title_absent():
    assert list(gkg.parse_gkg([gkg_line(extras="<OTHER>x</OTHER>")]))[0].title is None


# ── matching helpers ─────────────────────────────────────────────────────────

def test_normalize_name_strips_suffix_and_punct():
    assert gkg.normalize_name("Procter & Gamble Co.") == "procter gamble"
    assert gkg.normalize_name("AT&T Inc") == "at t"
    assert gkg.normalize_name("The Boeing Company") == "boeing"
    assert gkg.normalize_name("Apple Inc.") == "apple"
    assert gkg.normalize_name("!!!") == ""


def test_is_common_word():
    assert gkg.is_common_word("shell") and gkg.is_common_word("apple") and gkg.is_common_word("gap")
    assert not gkg.is_common_word("sony") and not gkg.is_common_word("nvidia")
    assert not gkg.is_common_word("lockheed martin")     # multi-token never ambiguous


def test_is_business_theme():
    assert gkg.is_business_theme("ECON_STOCKMARKET") and gkg.is_business_theme("EPU_POLICY_TAX")
    assert gkg.is_business_theme("STRIKE") and gkg.is_hard_event_theme("ECON_BANKRUPTCY")
    assert not gkg.is_business_theme("WB_696_PUBLIC_SECTOR")   # WB_ excluded
    assert not gkg.is_business_theme("TAX_FNCACT_CEO")


def test_amount_is_currency():
    assert gkg.amount_is_currency("in revenue") and gkg.amount_is_currency("billion deal")
    assert gkg.amount_is_currency("$5") and gkg.amount_is_currency("5 million dollars")
    assert gkg.amount_is_currency("50 tons of steel")            # whole word 'tons'
    assert not gkg.amount_is_currency("months") and not gkg.amount_is_currency("combat soldiers")
    # word-boundary: currency substrings inside other words must NOT match
    assert not gkg.amount_is_currency("Boston voters")           # contains 'ton'
    assert not gkg.amount_is_currency("cotton bales")            # contains 'ton'
    assert not gkg.amount_is_currency("eureka moment")           # contains 'eur'


# ── slice discovery ──────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, text="", content=b"", status=200):
        self.text = text; self.content = content; self.status_code = status
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def test_latest_gkg_url_parses_lastupdate(monkeypatch):
    body = ("21175 h1 http://data.gdeltproject.org/gdeltv2/20260704011500.export.CSV.zip\n"
            "32094 h2 http://data.gdeltproject.org/gdeltv2/20260704011500.mentions.CSV.zip\n"
            "2539293 h3 http://data.gdeltproject.org/gdeltv2/20260704011500.gkg.csv.zip\n")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(text=body))
    ts, url = gkg.latest_gkg_url()
    assert ts == "20260704011500" and url.endswith("20260704011500.gkg.csv.zip")


def test_previous_slice_ts_rollovers():
    assert gkg.previous_slice_ts("20260704011500") == "20260704010000"
    assert gkg.previous_slice_ts("20260704010000") == "20260704004500"   # minute
    assert gkg.previous_slice_ts("20260704000000") == "20260703234500"   # hour+day
    assert gkg.previous_slice_ts("20260701000000") == "20260630234500"   # month (June=30)
    assert gkg.previous_slice_ts("20260301000000") == "20260228234500"   # non-leap Feb
    assert gkg.previous_slice_ts("20240301000000") == "20240229234500"   # leap Feb
    assert gkg.previous_slice_ts("20260101000000") == "20251231234500"   # year


def test_recent_slice_timestamps():
    assert gkg.recent_slice_timestamps("20260704011500", 4) == [
        "20260704011500", "20260704010000", "20260704004500", "20260704003000"]


def test_gkg_slice_url():
    assert gkg.gkg_slice_url("20260704011500") == \
        "http://data.gdeltproject.org/gdeltv2/20260704011500.gkg.csv.zip"


def test_download_slice_cache_first(tmp_path, monkeypatch):
    # pre-existing cached file → returned without any request
    cached = tmp_path / "20260704011500.gkg.csv.zip"
    cached.write_bytes(b"zip")
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))
    assert gkg.download_slice("http://x/20260704011500.gkg.csv.zip", tmp_path) == cached


def test_download_slice_fetches_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(content=b"payload"))
    out = gkg.download_slice("http://x/20260704013000.gkg.csv.zip", tmp_path)
    assert out.read_bytes() == b"payload"


def test_download_slice_404_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(status=404))
    assert gkg.download_slice("http://x/future.gkg.csv.zip", tmp_path) is None


# ── ingest_news: node index + candidate + fetch ──────────────────────────────

def _graph():
    conn = store.connect(":memory:")
    store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name,tickers,aliases) VALUES "
                 "('cik:1','Company','Walmart','[\"WMT\"]','[\"Wal-Mart\"]')")
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('cik:2','Company','Apple')")
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('commodity:oil','Commodity','Crude Oil')")
    conn.commit()
    return conn


def test_build_gkg_node_index_includes_aliases_and_flags_ambiguous():
    idx = ing.build_gkg_node_index(_graph())
    assert idx["walmart"][0] == "cik:1" and idx["wal mart"][0] == "cik:1"   # name + alias
    assert idx["apple"][3] is True          # ambiguous (common word)
    assert idx["walmart"][3] is False


def test_build_gkg_node_index_drops_collisions():
    conn = store.connect(":memory:"); store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('cik:1','Company','Acme Inc')")
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('cik:2','Company','Acme Corp')")  # both → 'acme'
    conn.commit()
    assert "acme" not in ing.build_gkg_node_index(conn)     # ambiguous collision dropped


def test_proximity_ok():
    assert ing._proximity_ok([100], [300], []) is True         # theme within 400
    assert ing._proximity_ok([100], [700], []) is False        # too far
    assert ing._proximity_ok([100], [], [250]) is True         # amount within range
    assert ing._proximity_ok([], [10], []) is True             # no offsets → any biz signal
    assert ing._proximity_ok([], [], []) is False              # no offsets, no signal
    assert ing._proximity_ok([5000, 100], [150], []) is True   # 2nd mention near theme (not just offs[0])


def test_org_salience_token_boundary():
    assert ing._org_salience("at t", [5000], "flat terrain report") is False   # substring, not a token
    assert ing._org_salience("walmart", [5000], "walmart cuts outlook") is True
    assert ing._org_salience("apple", [100], "") is True        # lede offset
    assert ing._org_salience("apple", [5000, 6000], "") is True  # >=2 mentions
    assert ing._org_salience("apple", [5000], "") is False       # single deep mention
    assert ing._org_salience("apple", None, "") is None          # no offsets → unknown/allow


def _idx(conn):
    return ing.build_gkg_node_index(conn)


def test_gkg_candidate_matches_and_builds(monkeypatch):
    conn = _graph(); idx = _idx(conn); cen = {"cik:1": 1.0}
    rec = list(gkg.parse_gkg([gkg_line(
        url="https://n/1", domain="wsj.com", orgs_v1="walmart",
        themes_v1="ECON_EARNINGSREPORT", tone="-5,0,0,8,0,0,100",
        extras="<PAGE_TITLE>Walmart cuts outlook</PAGE_TITLE>")]))[0]
    score, cand = ing._gkg_candidate(rec, idx, cen)
    assert cand["seed_node_id"] == "cik:1" and cand["source"] == "GDELT-GKG"
    assert cand["headline"] == "Walmart cuts outlook" and cand["category"] == "company"
    assert cand["published_at"] == "2026-07-04" and cand["id"].startswith("ev:")
    assert score > 0


def test_gkg_candidate_title_fallback(monkeypatch):
    conn = _graph(); rec = list(gkg.parse_gkg([gkg_line(orgs_v1="walmart", domain="x.com", extras="")]))[0]
    _, cand = ing._gkg_candidate(rec, _idx(conn), {})
    assert cand["headline"] == "Walmart: news from x.com"      # synthesized when no PAGE_TITLE


def test_gkg_candidate_ambiguous_needs_proximity(monkeypatch):
    conn = _graph(); idx = _idx(conn)
    # "apple" is ambiguous; no business theme nearby → dropped
    plain = list(gkg.parse_gkg([gkg_line(url="https://n/a", orgs_v1="apple", orgs_v2="apple,50")]))[0]
    assert ing._gkg_candidate(plain, idx, {}) is None
    # same but with an ECON theme near the org offset → kept
    ok = list(gkg.parse_gkg([gkg_line(url="https://n/b", orgs_v1="apple", orgs_v2="apple,50",
                                      themes_v2="ECON_STOCKMARKET,120")]))[0]
    assert ing._gkg_candidate(ok, idx, {})[1]["seed_node_id"] == "cik:2"


def test_gkg_candidate_skips_nonweb_translingual_and_unmatched(monkeypatch):
    conn = _graph(); idx = _idx(conn)
    assert ing._gkg_candidate(list(gkg.parse_gkg([gkg_line(srccoll="2", orgs_v1="walmart")]))[0], idx, {}) is None
    assert ing._gkg_candidate(list(gkg.parse_gkg([gkg_line(record_id="2026-T1", orgs_v1="walmart")]))[0], idx, {}) is None
    assert ing._gkg_candidate(list(gkg.parse_gkg([gkg_line(orgs_v1="acme unmatched")]))[0], idx, {}) is None


def test_fetch_gkg_bulk_dedups_precaps_and_isolates(monkeypatch):
    conn = _graph()
    monkeypatch.setattr(ing, "GKG_SLICES", 1)
    monkeypatch.setattr(ing, "GKG_PRECAP", 2)
    monkeypatch.setattr(ing, "_centrality", lambda c: {"cik:1": 1.0, "cik:2": 0.5})
    lines = [
        gkg_line(url="u1", orgs_v1="walmart", themes_v1="ECON_EARNINGSREPORT",
                 amounts="9000000000,in revenue,50", sharingimage="img1"),
        gkg_line(url="u1", orgs_v1="walmart"),                       # dup URL → dropped
        gkg_line(url="u2", orgs_v1="walmart", sharingimage="img1"),  # dup image → dropped
        gkg_line(url="u3", orgs_v1="netflix"),                       # no node match → dropped
        gkg_line(url="u4", orgs_v1="apple", orgs_v2="apple,10",
                 themes_v1="ECON_STOCKMARKET", themes_v2="ECON_STOCKMARKET,20"),  # ambiguous but gated OK
    ]
    monkeypatch.setattr("pipeline.gkg.latest_gkg_url", lambda: ("20260704011500", "u"))
    monkeypatch.setattr("pipeline.gkg.download_slice", lambda url, cache: Path("dummy"))
    monkeypatch.setattr("pipeline.gkg.read_gkg_lines", lambda p: lines)
    out = ing.fetch_gkg_bulk(conn)
    urls = sorted(c["url"] for c in out)
    assert urls == ["u1", "u4"]                       # dups + unmatched gone; walmart(u1) + apple(u4)
    assert out[0]["url"] == "u1"                       # higher materiality prior ranked first


def test_fetch_gkg_bulk_collapses_syndication(monkeypatch):
    conn = _graph()
    monkeypatch.setattr(ing, "GKG_SLICES", 1)
    monkeypatch.setattr(ing, "_centrality", lambda c: {"cik:1": 1.0})
    # Same Walmart story from 3 outlets: distinct urls, distinct/absent images,
    # same title → the (seed,date,first-6-words) collapse key merges them to one
    # BEFORE the pre-cap, so syndication can't crowd out other candidates.
    t = "Walmart acquires regional grocery chain today"
    lines = [
        gkg_line(url="w1", orgs_v1="walmart", sharingimage="ia", extras=f"<PAGE_TITLE>{t}</PAGE_TITLE>"),
        gkg_line(url="w2", orgs_v1="walmart", sharingimage="ib", extras=f"<PAGE_TITLE>{t} - Reuters</PAGE_TITLE>"),
        gkg_line(url="w3", orgs_v1="walmart", sharingimage="", extras=f"<PAGE_TITLE>{t}, sources say</PAGE_TITLE>"),
    ]
    monkeypatch.setattr("pipeline.gkg.latest_gkg_url", lambda: ("20260704011500", "u"))
    monkeypatch.setattr("pipeline.gkg.download_slice", lambda url, cache: Path("d"))
    monkeypatch.setattr("pipeline.gkg.read_gkg_lines", lambda p: lines)
    out = ing.fetch_gkg_bulk(conn)
    assert len(out) == 1 and out[0]["url"] == "w1"     # 3 syndicated copies → one


def test_fetch_gkg_bulk_keeps_highest_score_copy(monkeypatch):
    conn = _graph()
    monkeypatch.setattr(ing, "GKG_SLICES", 1)
    monkeypatch.setattr(ing, "_centrality", lambda c: {"cik:1": 0.0})
    t = "Walmart reports record quarterly earnings beat"
    # bare copy iterated FIRST, rich copy (ECON theme + $ amount) SECOND, same
    # collapse key → the RICHER copy must win its pre-cap slot, not the first-seen.
    lines = [
        gkg_line(url="poor", orgs_v1="walmart", extras=f"<PAGE_TITLE>{t}</PAGE_TITLE>"),
        gkg_line(url="rich", orgs_v1="walmart", themes_v1="ECON_EARNINGSREPORT",
                 amounts="9000000000,in revenue,50", extras=f"<PAGE_TITLE>{t}</PAGE_TITLE>"),
    ]
    monkeypatch.setattr("pipeline.gkg.latest_gkg_url", lambda: ("20260704011500", "u"))
    monkeypatch.setattr("pipeline.gkg.download_slice", lambda url, cache: Path("d"))
    monkeypatch.setattr("pipeline.gkg.read_gkg_lines", lambda p: lines)
    out = ing.fetch_gkg_bulk(conn)
    assert len(out) == 1 and out[0]["url"] == "rich"


def test_fetch_gkg_bulk_image_dedup_scoped_to_seed(monkeypatch):
    conn = _graph()
    monkeypatch.setattr(ing, "GKG_SLICES", 1)
    monkeypatch.setattr(ing, "_centrality", lambda c: {"cik:1": 1.0, "cik:2": 0.9})
    # Two DIFFERENT companies sharing one publisher default image must BOTH survive
    # (a global image key would wrongly collapse them to one).
    lines = [
        gkg_line(url="wa", orgs_v1="walmart", sharingimage="logo.png",
                 extras="<PAGE_TITLE>Walmart acquires grocery chain</PAGE_TITLE>"),
        gkg_line(url="ap", orgs_v1="apple", orgs_v2="apple,20", themes_v2="ECON_STOCKMARKET,30",
                 sharingimage="logo.png", extras="<PAGE_TITLE>Apple beats earnings</PAGE_TITLE>"),
    ]
    monkeypatch.setattr("pipeline.gkg.latest_gkg_url", lambda: ("20260704011500", "u"))
    monkeypatch.setattr("pipeline.gkg.download_slice", lambda url, cache: Path("d"))
    monkeypatch.setattr("pipeline.gkg.read_gkg_lines", lambda p: lines)
    assert sorted(c["seed_node_id"] for c in ing.fetch_gkg_bulk(conn)) == ["cik:1", "cik:2"]


def test_fetch_gkg_bulk_titleless_not_overcollapsed(monkeypatch):
    conn = _graph()
    monkeypatch.setattr(ing, "GKG_SLICES", 1)
    monkeypatch.setattr(ing, "_centrality", lambda c: {"cik:1": 1.0})
    # Two DIFFERENT title-less Walmart stories from the same domain/day synthesize
    # an identical headline; they must NOT collapse (distinct urls → both kept).
    lines = [
        gkg_line(url="w-buy", orgs_v1="walmart", domain="reuters.com", extras=""),
        gkg_line(url="w-recall", orgs_v1="walmart", domain="reuters.com", extras=""),
    ]
    monkeypatch.setattr("pipeline.gkg.latest_gkg_url", lambda: ("20260704011500", "u"))
    monkeypatch.setattr("pipeline.gkg.download_slice", lambda url, cache: Path("d"))
    monkeypatch.setattr("pipeline.gkg.read_gkg_lines", lambda p: lines)
    assert sorted(c["url"] for c in ing.fetch_gkg_bulk(conn)) == ["w-buy", "w-recall"]


def test_gkg_candidate_drops_incidental_and_exchange(monkeypatch):
    conn = _graph(); idx = _idx(conn)
    # Walmart mentioned once, deep in the body, not in title → incidental → dropped.
    incidental = list(gkg.parse_gkg([gkg_line(url="https://n/z", orgs_v1="walmart",
        orgs_v2="walmart,5000", extras="<PAGE_TITLE>Local dog show draws crowds</PAGE_TITLE>")]))[0]
    assert ing._gkg_candidate(incidental, idx, {"cik:1": 1.0}) is None
    # An exchange reference ("nasdaq") is never a seed even if a Nasdaq node exists.
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('cik:9','Company','Nasdaq')")
    conn.commit()
    idx2 = _idx(conn)
    exch = list(gkg.parse_gkg([gkg_line(url="https://n/y", orgs_v1="nasdaq",
        orgs_v2="nasdaq,20", extras="<PAGE_TITLE>ACME (NASDAQ:ACM) rises</PAGE_TITLE>")]))[0]
    assert ing._gkg_candidate(exch, idx2, {"cik:9": 1.0}) is None


def test_fetch_gkg_bulk_disabled(monkeypatch):
    monkeypatch.setenv("INGEST_GKG", "0")
    monkeypatch.setattr("pipeline.gkg.latest_gkg_url",
                        lambda: (_ for _ in ()).throw(AssertionError("must not poll")))
    assert ing.fetch_gkg_bulk(_graph()) == []


def test_fetch_gkg_bulk_precap_limits(monkeypatch):
    conn = _graph()
    monkeypatch.setattr(ing, "GKG_SLICES", 1)
    monkeypatch.setattr(ing, "GKG_PRECAP", 1)
    monkeypatch.setattr(ing, "_centrality", lambda c: {"cik:1": 1.0, "commodity:oil": 0.9})
    lines = [
        gkg_line(url="ua", orgs_v1="walmart", themes_v1="ECON_EARNINGSREPORT",
                 amounts="9000000000,in revenue,50"),                 # high score
        gkg_line(url="ub", orgs_v1="crude oil"),                       # low score
    ]
    monkeypatch.setattr("pipeline.gkg.latest_gkg_url", lambda: ("20260704011500", "u"))
    monkeypatch.setattr("pipeline.gkg.download_slice", lambda url, cache: Path("d"))
    monkeypatch.setattr("pipeline.gkg.read_gkg_lines", lambda p: lines)
    out = ing.fetch_gkg_bulk(conn)
    assert len(out) == 1 and out[0]["url"] == "ua"     # pre-cap keeps only the top-scored
