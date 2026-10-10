"""Compute per-signal market outcomes for the reliability layer, framed for a RETAIL trader.

First-order news is priced in before a retail trader can act (quant/algo, often the open gap), so
we measure what is actually CAPTURABLE: enter at the NEXT session's OPEN after the announcement
(the first trading day >= published_at), and decompose each signal into

  * ann_abn  — the priced-in abnormal move on the announcement session (direction check only; NOT
               capturable by anyone entering after),
  * gap_abn  — the overnight from the announcement close to the entry open (also missed),
  * abn_ret  — the CAPTURABLE abnormal drift, entry_open -> exit_close at +h trading days,
  * net_cap  — that, direction-signed and net of a flat round-trip cost (the retail bar).

No look-ahead (entry is strictly after the announcement session's open) and maturity-gated.

    from pipeline import signal_outcomes
    signal_outcomes.compute_outcomes(conn)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from schema import store
from pipeline import prices

log = logging.getLogger(__name__)

HORIZONS = tuple(int(h) for h in os.environ.get("RELIABILITY_HORIZONS", "1,5,20,60").split(","))
# Flat round-trip transaction cost (bps) deducted ONCE per signal (per-trade, not per-day). Default
# 10 bps is a conservative retail estimate for liquid S&P names at the open (open = widest spread);
# ~5 bps is optimistic, stress at 15-20. Per Rule-605 / microstructure research.
COST_BPS = float(os.environ.get("RELIABILITY_COST_BPS", "10"))
# Lead days before the first signal so the announcement-day lookup always has a prior bar.
_LOOKBACK_PAD_DAYS = 7


def _ticker_of(tickers_json) -> str | None:
    try:
        arr = json.loads(tickers_json or "[]")
        return arr[0] if arr else None
    except Exception:
        return None


def _date_part(s: str) -> str:
    return (s or "")[:10]


def _signals(conn) -> list[dict]:
    """Directional signals on tickered Company nodes, with the event's publish date + sector."""
    rows = conn.execute(
        """
        SELECT ei.event_id, ei.node_id, ei.hop, ei.direction, ei.magnitude,
               COALESCE(NULLIF(e.published_at,''), e.ingested_at) AS pub, n.tickers, n.sector
        FROM event_impacts ei
        JOIN events e ON e.id = ei.event_id
        JOIN nodes  n ON n.id = ei.node_id
        WHERE ei.direction IN ('positive','negative')
          AND e.status = 'traced'
          AND n.tickers IS NOT NULL AND n.tickers != '[]' AND n.tickers != ''
        """).fetchall()
    out = []
    for r in rows:
        tk = _ticker_of(r["tickers"])
        if tk:
            out.append({"event_id": r["event_id"], "node_id": r["node_id"], "hop": r["hop"],
                        "direction": r["direction"], "magnitude": r["magnitude"],
                        "pub": _date_part(r["pub"]), "ticker": tk,
                        "benchmark": prices.benchmark_for_sector(r["sector"])})
    return out


def _direct_news_days(conn) -> dict:
    """node_id -> set of dates on which that node had its OWN direct (hop-0) traced news.
    Used for the clean-subset flag (isolating propagated effects from a node's own news)."""
    days: dict[str, set] = {}
    for r in conn.execute(
        """SELECT ei.node_id, date(COALESCE(NULLIF(e.published_at,''), e.ingested_at)) d
           FROM event_impacts ei JOIN events e ON e.id=ei.event_id
           WHERE ei.hop=0 AND e.status='traced'"""):
        days.setdefault(r[0], set()).add(r[1])
    return days


def _base_index(dates: list, pub: str) -> int | None:
    """Index of the first trading day >= pub (the day we'd know the news). None if beyond data."""
    lo, hi = 0, len(dates)
    while lo < hi:
        mid = (lo + hi) // 2
        if dates[mid] < pub:
            lo = mid + 1
        else:
            hi = mid
    return lo if lo < len(dates) else None


def compute_outcomes(conn, horizons=HORIZONS) -> dict:
    sigs = _signals(conn)
    summary = {"signals": len(sigs), "priced": 0, "written": 0, "immature": 0, "no_price": 0}
    if not sigs:
        log.info("outcomes: no signals"); return summary

    start = min(s["pub"] for s in sigs)
    start = (datetime.strptime(start, "%Y-%m-%d").toordinal() - _LOOKBACK_PAD_DAYS)
    start = datetime.fromordinal(start).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tickers = {s["ticker"] for s in sigs} | {s["benchmark"] for s in sigs}
    refresh_days = int(max(horizons) * 1.7) + 10
    prices.ensure_prices(conn, sorted(tickers), start, today, refresh_days=refresh_days)

    # In-memory close + open dicts and the per-ticker trading-day list, built once.
    close: dict[str, dict] = {}
    opens: dict[str, dict] = {}
    dates: dict[str, list] = {}
    for t in tickers:
        ser = store.price_ohlc(conn, t)                       # (date, adj_close, adj_open)
        close[t] = {d: c for d, c, _ in ser}
        opens[t] = {d: o for d, _, o in ser if o is not None}
        dates[t] = [d for d, _, _ in ser]

    direct_days = _direct_news_days(conn)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cost = COST_BPS / 10000.0
    out_rows = []
    for s in sigs:
        tk, bm = s["ticker"], s["benchmark"]
        pub, pos = s["pub"], s["direction"] == "positive"
        tdates, tclose, topen = dates.get(tk, []), close.get(tk, {}), opens.get(tk, {})
        bclose, bopen = close.get(bm, {}), opens.get(bm, {})
        if not tdates or not bclose:
            summary["no_price"] += 1
            continue
        ai = _base_index(tdates, pub)                         # announcement session (first >= pub)
        if ai is None:
            summary["immature"] += 1
            continue
        ann_date = tdates[ai]
        # Enter at the NEXT session's open: strictly AFTER the published date (no look-ahead). For
        # a trading-day pub that is ai+1; for weekend/holiday news (ann_date already > pub) the
        # first open after the news IS ann_date, so entry == announcement session.
        entry_i = ai if ann_date > pub else ai + 1
        if entry_i >= len(tdates):
            summary["immature"] += 1
            continue
        entry_date = tdates[entry_i]
        entry_open, b_entry_open = topen.get(entry_date), bopen.get(entry_date)
        if entry_open is None or b_entry_open is None:        # need the entry open for both legs
            summary["no_price"] += 1
            continue
        summary["priced"] += 1

        # Priced-in / missed decomposition — a DISJOINT partition of the move before you can act
        # (NOT capturable by a next-open entrant). Trading-day pub: ann_abn = the announcement
        # SESSION move (close[ai-1]->close[ai]) and gap_abn = the overnight close[ai]->entry open.
        # Weekend/holiday pub (entry == the announcement session): the whole missed move is ONE
        # gap, the last pre-pub close -> entry open; record it as gap_abn with ann_abn = None (so
        # it never overlaps the capturable leg, which also starts at that session's open).
        ann_abn = gap_abn = None
        prev_c = tclose.get(tdates[ai - 1]) if ai - 1 >= 0 else None
        b_prev_c = bclose.get(tdates[ai - 1]) if ai - 1 >= 0 else None
        if entry_i > ai:
            ac, bac = tclose.get(ann_date), bclose.get(ann_date)
            if prev_c and ac is not None and b_prev_c and bac is not None:
                ann_abn = round((ac / prev_c - 1.0) - (bac / b_prev_c - 1.0), 6)
            if ac and bac:
                gap_abn = round((entry_open / ac - 1.0) - (b_entry_open / bac - 1.0), 6)
        elif prev_c and b_prev_c:
            gap_abn = round((entry_open / prev_c - 1.0) - (b_entry_open / b_prev_c - 1.0), 6)

        node_direct = direct_days.get(s["node_id"], set())
        lo_inclusive = s["hop"] != 0
        for h in horizons:
            xi = entry_i + h
            if xi >= len(tdates):
                summary["immature"] += 1
                continue
            exit_date = tdates[xi]
            s_exit, b_exit = tclose.get(exit_date), bclose.get(exit_date)
            if s_exit is None or b_exit is None:
                continue
            cap_ret = s_exit / entry_open - 1.0               # capturable: entry open -> exit close
            bench_cap = b_exit / b_entry_open - 1.0
            cap_abn = cap_ret - bench_cap
            hit = 1 if ((pos and cap_abn > 0) or (not pos and cap_abn < 0)) else 0
            dir_cap = cap_abn if pos else -cap_abn            # return in the predicted direction
            net = dir_cap - cost                              # net of one round-trip cost
            # clean = node had no OWN direct (hop-0) news across the measured window
            # [ann_date, exit_date]; base-inclusive for propagated signals, exclusive for hop-0
            # (so a hop-0 signal never flags itself on its own announcement day).
            clean = 1
            for d in node_direct:
                if ((ann_date <= d) if lo_inclusive else (ann_date < d)) and d <= exit_date:
                    clean = 0
                    break
            out_rows.append({
                "event_id": s["event_id"], "node_id": s["node_id"], "ticker": tk, "hop": s["hop"],
                "direction": s["direction"], "magnitude": s["magnitude"], "base_date": entry_date,
                "horizon_days": h, "stock_ret": round(cap_ret, 6), "benchmark": bm,
                "benchmark_ret": round(bench_cap, 6), "abn_ret": round(cap_abn, 6), "hit": hit,
                "clean": clean, "ann_abn": ann_abn, "gap_abn": gap_abn,
                "net_cap": round(net, 6), "net_hit": 1 if net > 0 else 0, "cost_bps": COST_BPS,
                "computed_at": now})
    summary["written"] = store.upsert_signal_outcomes(conn, out_rows)
    log.info("outcomes: %s", summary)
    return summary
