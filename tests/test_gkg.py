from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
import requests

from pipeline import gkg
from pipeline import ingest_news as ing
from schema import store


def _zip_bytes(text="row\n"):
    """A real .gkg.csv.zip payload for download/read tests."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.gkg.csv", text)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _isolate_gkg_cache(tmp_path, monkeypatch):
    # fetch_gkg_bulk prunes GKG_CACHE_DIR at the end — point it at a temp dir so the
    # tests never touch (or delete from) the real production cache.
    monkeypatch.setattr(ing, "GKG_CACHE_DIR", tmp_path / "_isolated_gkg_cache")


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
    # a VALID cached zip → returned without any request
    cached = tmp_path / "20260704011500.gkg.csv.zip"
    cached.write_bytes(_zip_bytes())
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))
    assert gkg.download_slice("http://x/20260704011500.gkg.csv.zip", tmp_path) == cached


def test_download_slice_fetches_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(content=_zip_bytes("hello")))
    out = gkg.download_slice("http://x/20260704013000.gkg.csv.zip", tmp_path)
    assert out is not None and zipfile.is_zipfile(out)


def test_download_slice_404_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(status=404))
    assert gkg.download_slice("http://x/future.gkg.csv.zip", tmp_path) is None


def test_download_slice_refetches_corrupt_cache(tmp_path, monkeypatch):
    # a poisoned (non-zip, size>0) cache file must be discarded + re-downloaded,
    # not trusted forever.
    bad = tmp_path / "20260704011500.gkg.csv.zip"
    bad.write_bytes(b"partial-not-a-zip")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(content=_zip_bytes("ok")))
    out = gkg.download_slice("http://x/20260704011500.gkg.csv.zip", tmp_path)
    assert out is not None and zipfile.is_zipfile(out)


def test_download_slice_discards_nonzip_download(tmp_path, monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(content=b"<html>rate limited</html>"))
    assert gkg.download_slice("http://x/bad.gkg.csv.zip", tmp_path) is None
    assert not list(tmp_path.glob("*"))                    # nothing (incl. .part) left behind


def test_download_slice_transport_error_returns_none(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("boom")
    monkeypatch.setattr(requests, "get", boom)
    assert gkg.download_slice("http://x/x.gkg.csv.zip", tmp_path) is None   # matches docstring


def test_read_gkg_lines_real_zip_preserves_row_with_embedded_cr(tmp_path):
    # A valid 27-col row whose PAGE_TITLE carries a bare CR must NOT be split into
    # sub-27-col fragments (str.splitlines would). Round-trip a real zip.
    row = gkg_line(url="https://n/1", orgs_v1="walmart",
                   extras="<PAGE_TITLE>Walmart cuts\routlook</PAGE_TITLE>")
    z = tmp_path / "s.gkg.csv.zip"
    z.write_bytes(_zip_bytes(row + "\n"))
    lines = gkg.read_gkg_lines(z)
    assert len(lines) == 1 and len(lines[0].split("\t")) == 27   # not shredded


def test_read_gkg_lines_empty_zip_raises(tmp_path):
    z = tmp_path / "empty.zip"
    with zipfile.ZipFile(z, "w"):
        pass
    with pytest.raises(ValueError):
        gkg.read_gkg_lines(z)


def test_latest_gkg_url_ignores_garbage_timestamp(monkeypatch):
    body = ("0 h http://data.gdeltproject.org/gdeltv2/proxy-notice-gkg.csv.zip\n"
            "0 h http://data.gdeltproject.org/gdeltv2/20260704011500.gkg.csv.zip\n")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(text=body))
    ts, url = gkg.latest_gkg_url()
    assert ts == "20260704011500"        # the garbage 'proxy-notice' line is skipped


def test_normalize_name_ascii_folds_accents():
    assert gkg.normalize_name("Estée Lauder Companies (The)") == "estee lauder"  # 'companies'/'the' stripped
    assert gkg.normalize_name("Telefónica") == "telefonica"
    assert gkg.normalize_name("Nestlé") == "nestle"
    assert gkg.normalize_name("América Móvil") == "america movil"
    # the folded node name matches GDELT's ASCII surface form
    assert gkg.normalize_name("Estée Lauder") == gkg.normalize_name("Estee Lauder")


def test_amount_is_currency_non_western():
    assert gkg.amount_is_currency("100 billion yen") and gkg.amount_is_currency("50 yuan")
    assert gkg.amount_is_currency("₹500 crore") and gkg.amount_is_currency("¥900")
    assert gkg.amount_is_currency("₩1 trillion") and gkg.amount_is_currency("500 rupees")


def test_headline_from_url():
    assert ing._headline_from_url("https://reuters.com/walmart-buys-chain-2026", "Walmart", "reuters.com") \
        == "Walmart: walmart buys chain"                    # slug words, digit dropped
    assert ing._headline_from_url("https://reuters.com/", "Walmart", "reuters.com") \
        == "Walmart: news from reuters.com"                 # no usable slug → domain fallback


def test_dedupe_shared_keeps_titleless_gkg_stories():
    # The finding-3 fix at the SHARED dedupe layer: two distinct title-less GKG
    # stories share an identical synthetic headline but carry _no_collapse, so the
    # shared dedupe() must NOT merge them.
    conn = _graph()

    def mk(url):
        c = {"headline": "Walmart: news from reuters.com", "source": "GDELT-GKG", "url": url,
             "category": "company", "published_at": "2026-07-04", "seed_entity": "Walmart",
             "seed_node_id": "cik:1", "_no_collapse": True}
        c["id"] = ing._event_id(c)
        return c

    assert len(ing.dedupe([mk("u1"), mk("u2")], conn)) == 2


def test_gkg_materiality_prior_components():
    def rec(**kw):
        return list(gkg.parse_gkg([gkg_line(**kw)]))[0]
    plain = rec(orgs_v1="x")
    assert ing._gkg_materiality_prior(rec(orgs_v1="x", themes_v1="ECON_BANKRUPTCY"), 0.0) \
        > ing._gkg_materiality_prior(plain, 0.0)            # hard-event bumps the prior
    assert ing._gkg_materiality_prior(rec(orgs_v1="x", amounts="9000000000,in revenue,10"), 0.0) \
        > ing._gkg_materiality_prior(plain, 0.0)            # currency bumps the prior
    three = rec(orgs_v1="x", themes_v1="ECON_STOCKMARKET;ECON_IPO;ECON_DEBT")
    four = rec(orgs_v1="x", themes_v1="ECON_STOCKMARKET;ECON_IPO;ECON_DEBT;ECON_INFLATION")
    assert ing._gkg_materiality_prior(three, 0.0) == ing._gkg_materiality_prior(four, 0.0)  # biz saturates at 3


def test_fetch_gkg_bulk_multislice_isolates_and_dedups(monkeypatch):
    conn = _graph()
    monkeypatch.setattr(ing, "GKG_SLICES", 3)
    monkeypatch.setattr(ing, "_centrality", lambda c: {"cik:1": 1.0})
    story = "<PAGE_TITLE>Walmart earnings beat estimates today</PAGE_TITLE>"
    per_ts = {
        "20260704011500": None,   # newest slice RAISES → must be isolated
        "20260704010000": [gkg_line(url="shared", orgs_v1="walmart", extras=story)],
        "20260704004500": [gkg_line(url="shared", orgs_v1="walmart", extras=story)],  # same url next slice
    }
    monkeypatch.setattr("pipeline.gkg.latest_gkg_url", lambda: ("20260704011500", "u"))
    monkeypatch.setattr("pipeline.gkg.download_slice",
                        lambda url, cache: Path(url.rsplit("/", 1)[-1].split(".", 1)[0]))

    def rd(path):
        lines = per_ts.get(Path(path).name)
        if lines is None:
            raise ValueError("bad slice")
        return lines

    monkeypatch.setattr("pipeline.gkg.read_gkg_lines", rd)
    out = ing.fetch_gkg_bulk(conn)
    assert [c["url"] for c in out] == ["shared"]     # bad slice skipped; url deduped across slices


def test_fetch_gkg_bulk_deadline_stops(monkeypatch):
    conn = _graph()
    monkeypatch.setattr(ing, "GKG_SLICES", 3)
    monkeypatch.setattr(ing, "_centrality", lambda c: {"cik:1": 1.0})
    calls = {"n": 0}
    monkeypatch.setattr("pipeline.gkg.latest_gkg_url", lambda: ("20260704011500", "u"))

    def dl(url, cache):
        calls["n"] += 1
        return Path("d")

    monkeypatch.setattr("pipeline.gkg.download_slice", dl)
    monkeypatch.setattr("pipeline.gkg.read_gkg_lines",
                        lambda p: [gkg_line(url=f"u{calls['n']}", orgs_v1="walmart",
                                            extras="<PAGE_TITLE>Walmart news today right here</PAGE_TITLE>")])
    seq = iter([0.0, 0.0, 2000.0])   # deadline compute, iter1 check (ok), iter2 check (past)
    monkeypatch.setattr(ing.time, "monotonic", lambda: next(seq, 2000.0))
    ing.fetch_gkg_bulk(conn)
    assert calls["n"] == 1           # only slice 1 downloaded before the deadline tripped


def test_build_gkg_node_index_tolerates_bad_alias_json():
    conn = store.connect(":memory:"); store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name,aliases) VALUES ('cik:1','Company','Acme','not-json{')")
    conn.commit()
    assert ing.build_gkg_node_index(conn)["acme"][0] == "cik:1"   # bad aliases ignored, name still indexed


def test_normalize_name_transliterates_nondecomposable():
    assert gkg.normalize_name("Ørsted") == "orsted"         # Ø→o (NFKD would delete it)
    assert gkg.normalize_name("Æon") == "aeon"
    assert gkg.normalize_name("Maerskß") == "maerskss"
    assert gkg.normalize_name("Ørsted") == gkg.normalize_name("Orsted")   # matches GDELT surface


def test_extract_title_sanitizes_control_chars():
    rec = list(gkg.parse_gkg([gkg_line(
        extras="<PAGE_TITLE>Real headline\r999. Fake material event</PAGE_TITLE>")]))[0]
    assert "\r" not in rec.title
    assert rec.title == "Real headline 999. Fake material event"   # CR → space, no injected line


def test_exchange_skip_does_not_drop_dow_inc():
    conn = store.connect(":memory:"); store.init_db(conn)
    conn.execute("INSERT INTO nodes (id,type,name) VALUES ('cik:dow','Company','Dow')")
    conn.commit()
    idx = ing.build_gkg_node_index(conn)
    rec = list(gkg.parse_gkg([gkg_line(url="https://n/1", orgs_v1="dow",
        extras="<PAGE_TITLE>Dow raises dividend after strong quarter</PAGE_TITLE>")]))[0]
    assert ing._gkg_candidate(rec, idx, {"cik:dow": 1.0})[1]["seed_node_id"] == "cik:dow"


def test_amount_is_currency_rejects_real_estate():
    assert not gkg.amount_is_currency("real estate") and not gkg.amount_is_currency("for real")


def test_proximity_v1_theme_fallback():
    assert ing._proximity_ok([], [], [], True) is True    # V1-only business theme keeps the match
    assert ing._proximity_ok([], [], [], False) is False


def test_org_salience_requires_contiguous_tokens():
    assert ing._org_salience("at t", [5000], "stocks rise at noon t bill sells") is False  # scattered
    assert ing._org_salience("at t", [5000], "at t reports earnings") is True              # contiguous


def test_headline_from_url_strips_extension():
    assert ing._headline_from_url("https://x.com/apple-recalls-devices.html", "Apple", "x.com") \
        == "Apple: apple recalls devices"


def test_fetch_gkg_bulk_titleless_shared_image_not_collapsed(monkeypatch):
    conn = _graph()
    monkeypatch.setattr(ing, "GKG_SLICES", 1)
    monkeypatch.setattr(ing, "_centrality", lambda c: {"cik:1": 1.0})
    # two DIFFERENT title-less Walmart stories sharing the publisher's default OG
    # image must BOTH survive — the image dedup layer must honor _no_collapse.
    lines = [
        gkg_line(url="w1", orgs_v1="walmart", sharingimage="default-logo.png", extras=""),
        gkg_line(url="w2", orgs_v1="walmart", sharingimage="default-logo.png", extras=""),
    ]
    monkeypatch.setattr("pipeline.gkg.latest_gkg_url", lambda: ("20260704011500", "u"))
    monkeypatch.setattr("pipeline.gkg.download_slice", lambda url, cache: Path("d"))
    monkeypatch.setattr("pipeline.gkg.read_gkg_lines", lambda p: lines)
    assert sorted(c["url"] for c in ing.fetch_gkg_bulk(conn)) == ["w1", "w2"]


def test_prune_slice_cache(tmp_path):
    for ts in ("20260704000000", "20260704001500", "20260704003000",
               "20260704004500", "20260704010000"):
        (tmp_path / f"{ts}.gkg.csv.zip").write_bytes(_zip_bytes())
    assert gkg.prune_slice_cache(tmp_path, keep=2) == 3           # 5 → keep 2 newest
    assert sorted(p.name for p in tmp_path.glob("*.gkg.csv.zip")) == \
        ["20260704004500.gkg.csv.zip", "20260704010000.gkg.csv.zip"]


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


# ── context capsule (grounding) ─────────────────────────────────────────────
# build_gkg_context is now pure formatting: it receives a PRE-RANKED list of
# other-org names (the matching + salience ranking is done by _gkg_candidate).

def _ctx_rec(tone=0.0, amounts=None):
    return gkg.GkgRecord(
        record_id="r", published_at="2026-07-05", is_translingual=False, is_web=True,
        domain="x.com", url="http://x/1", orgs=[], v2orgs=[], tone=tone, amounts=amounts or [])


def test_build_gkg_context_orgs_money_tone():
    rec = _ctx_rec(tone=3.2, amounts=[(1_000_000_000.0, "in revenue", 10), (3.0, "months", 20)])
    assert gkg.build_gkg_context(rec, ["Alexbank", "Safaricom"]) == \
        "[involves: Alexbank, Safaricom | $1B | positive tone]"


def test_build_gkg_context_none_when_nothing_useful():
    # no other orgs, near-neutral tone, non-currency amount → None.
    rec = _ctx_rec(tone=0.5, amounts=[(3.0, "months", 1)])
    assert gkg.build_gkg_context(rec, []) is None


def test_build_gkg_context_negative_tone_and_largest_amount_wins():
    rec = _ctx_rec(tone=-4.0, amounts=[(500_000_000.0, "dollars", 1), (12000.0, "usd", 2)])
    assert gkg.build_gkg_context(rec, ["Alexbank"]) == "[involves: Alexbank | $500M | negative tone]"


def test_build_gkg_context_caps_orgs_at_five():
    rec = _ctx_rec(tone=0.0)
    ctx = gkg.build_gkg_context(rec, [f"C{i}" for i in range(8)])
    assert ctx.count(",") == 4          # 5 orgs listed → 4 comma separators


# ── GKG-native salience: seed = most salient org, capsule ranked by salience ──

def test_org_salience_score_ranks_title_over_body():
    # Title mention beats a deep single body mention; lede beats deep.
    s_title = ing._org_salience_score("target", [30], "target opens stores")
    s_body  = ing._org_salience_score("walmart", [5000, 5200], "target opens stores")
    s_lede  = ing._org_salience_score("apple", [50], "")
    s_deep  = ing._org_salience_score("apple", [5000, 6000], "")
    assert s_title > s_body and s_lede > s_deep and s_title > 0


def test_gkg_candidate_seed_is_most_salient_not_most_central():
    # Article is ABOUT Target (in the title); Walmart is far more central but only
    # mentioned deep in the body. Salience must win → Target is the seed, and
    # Walmart appears in the capsule's "involves:" list.
    idx = {"target": ("cik:9", "Target", "Company", False),
           "walmart": ("cik:1", "Walmart", "Company", False)}
    cen = {"cik:1": 5.0, "cik:9": 0.1}          # Walmart far more central
    rec = list(gkg.parse_gkg([gkg_line(
        url="https://n/1", orgs_v1="target;walmart",
        orgs_v2="target,30;walmart,5000;walmart,5200",
        extras="<PAGE_TITLE>Target opens new stores</PAGE_TITLE>")]))[0]
    _, cand = ing._gkg_candidate(rec, idx, cen)
    assert cand["seed_node_id"] == "cik:9"                 # salience beats centrality
    assert cand["gkg_context"] == "[involves: Walmart]"    # other matched org, salience-ranked


def test_gkg_candidate_sets_seed_ids_and_prior():
    idx = {"target": ("cik:9", "Target", "Company", False),
           "walmart": ("cik:1", "Walmart", "Company", False)}
    cen = {"cik:1": 5.0, "cik:9": 0.1}
    rec = list(gkg.parse_gkg([gkg_line(
        url="https://n/1", orgs_v1="target;walmart",
        orgs_v2="target,30;walmart,5000;walmart,5200",
        extras="<PAGE_TITLE>Target opens new stores</PAGE_TITLE>")]))[0]
    prior, cand = ing._gkg_candidate(rec, idx, cen)
    import json
    assert json.loads(cand["seed_ids"]) == ["cik:9", "cik:1"]     # seed first, salience order
    assert cand["_prior"] == prior and prior > 0
