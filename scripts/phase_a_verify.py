"""Phase A smoke test: assert the foreign 20-F filers landed in the
rebuilt graph with edges + HQ coords.

Usage:  python scripts/phase_a_verify.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB = REPO_ROOT / "econgraph.db"

# (cik, expected name fragment, expected HQ city fragment / "" if any)
SAMPLE = [
    ("cik:0001094517", "Toyota",  "Toyota"),     # Toyota City, Aichi, Japan
    ("cik:0001046179", "Taiwan",  "Hsinchu"),    # TSMC, Hsinchu, Taiwan
    ("cik:0001577552", "Alibaba", "Hangzhou"),   # Alibaba, Hangzhou
    ("cik:0001306965", "Shell",   ""),           # Shell, The Hague (any non-empty)
    ("cik:0000937966", "ASML",    "Veldhoven"),
    ("cik:0001067491", "Infosys", "Bangalore"),  # or Bengaluru
    ("cik:0001119639", "Petrobras", "Rio"),
]


def main() -> int:
    if not DB.exists():
        print(f"ERROR: db not found at {DB}", file=sys.stderr)
        return 2
    con = sqlite3.connect(DB)
    fail = 0
    ok = 0
    for cik, expected_name, expected_hq in SAMPLE:
        row = con.execute(
            "SELECT id, name, country, metadata FROM nodes WHERE id=?", (cik,)
        ).fetchone()
        if not row:
            print(f"FAIL: {cik} -- node missing")
            fail += 1
            continue
        nid, name, country, md_raw = row
        if expected_name.lower() not in (name or "").lower():
            print(f"FAIL: {cik} -- name {name!r} doesn't contain {expected_name!r}")
            fail += 1
            continue
        try:
            md = json.loads(md_raw) if md_raw else {}
        except Exception:
            md = {}
        wd = (md.get("wikidata") or {}) if isinstance(md, dict) else {}
        hq = wd.get("hq") if isinstance(wd, dict) else None
        lat = wd.get("lat") if isinstance(wd, dict) else None
        lon = wd.get("lon") if isinstance(wd, dict) else None
        coord_ok = isinstance(lat, (int, float)) and isinstance(lon, (int, float))
        hq_ok = (not expected_hq) or (hq and expected_hq.lower() in hq.lower())

        # Connectivity: at least 1 supplies + at least 1 regulated_by
        n_sup = con.execute(
            "SELECT COUNT(*) FROM edges WHERE (source=? OR target=?) AND type='supplies' AND below_threshold=0",
            (cik, cik),
        ).fetchone()[0]
        n_reg = con.execute(
            "SELECT COUNT(*) FROM edges WHERE source=? AND type='regulated_by'",
            (cik,),
        ).fetchone()[0]

        if not (coord_ok and hq_ok and n_sup >= 1 and n_reg >= 1):
            print(
                f"PARTIAL: {cik} {name:<25} country={country!r}  hq={hq!r} "
                f"coords={(lat, lon)}  supplies={n_sup}  regulated_by={n_reg}"
            )
            fail += 1
        else:
            print(
                f"PASS: {cik} {name:<25} hq={hq!r} lat={lat:.2f} lon={lon:.2f} "
                f"supplies={n_sup}  regulated_by={n_reg}"
            )
            ok += 1
    print()
    print(f"--- {ok} pass / {fail} fail / {len(SAMPLE)} sample ---")
    # Wider stats
    total_companies = con.execute(
        "SELECT COUNT(*) FROM nodes WHERE type='Company'"
    ).fetchone()[0]
    by_country = con.execute(
        "SELECT country, COUNT(*) FROM nodes WHERE type='Company' GROUP BY country ORDER BY 2 DESC"
    ).fetchall()
    print(f"Total Company nodes: {total_companies}")
    print("Top countries:")
    for c, n in by_country[:12]:
        print(f"  {(c or '(none)'):<8} {n}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
