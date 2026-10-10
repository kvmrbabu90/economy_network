"""Intraday-entry outcomes: model a trader who runs the hourly ingest cycle.

A signal becomes actionable when we'd have it in hand = its news timestamp (GKG batch stamp, UTC)
+ a processing latency (fetch cadence + trace). We ENTER at the first hourly bar whose open is at
or after that moment (no look-ahead), and EXIT at the daily close +h trading days later (h=0 is the
same-session close). This isolates the value of the earlier, intraday entry vs the daily
next-open model. Only events that carry a precise `published_ts` (GKG) are eligible.

    from pipeline import intraday_outcomes
    intraday_outcomes.compute_intraday_outcomes(conn)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from schema import store
from pipeline import prices, intraday_prices, signal_outcomes

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")
HORIZONS = tuple(int(h) for h in os.environ.get("INTRADAY_HORIZONS", "0,1,5,20,60").split(","))
# Modeled news->action latency: conservative ~1 cycle (the GKG batch stamp can be up to a fetch
# interval stale before we act). Tunable; the dashboard states the assumption.
LATENCY_MIN = int(os.environ.get("INTRADAY_LATENCY_MIN", "60"))
COST_BPS = float(os.environ.get("RELIABILITY_COST_BPS", "10"))


def _signals(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT ei.event_id, ei.node_id, ei.hop, ei.direction, ei.magnitude,
               e.published_ts, n.tickers, n.sector
        FROM event_impacts ei
        JOIN events e ON e.id = ei.event_id
        JOIN nodes  n ON n.id = ei.node_id
        WHERE ei.direction IN ('positive','negative') AND e.status='traced'
          AND e.published_ts IS NOT NULL AND e.published_ts != ''
          AND n.tickers IS NOT NULL AND n.tickers != '[]' AND n.tickers != ''
        """).fetchall()
    out = []
    for r in rows:
        tk = signal_outcomes._ticker_of(r["tickers"])
        if tk:
            out.append({"event_id": r["event_id"], "node_id": r["node_id"], "hop": r["hop"],
                        "direction": r["direction"], "magnitude": r["magnitude"],
                        "published_ts": r["published_ts"], "ticker": tk,
                        "benchmark": prices.benchmark_for_sector(r["sector"])})
    return out


def _actionable_et(published_ts: str) -> datetime:
    dt = datetime.fromisoformat(published_ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(_ET) + timedelta(minutes=LATENCY_MIN)


def compute_intraday_outcomes(conn, horizons=HORIZONS) -> dict:
    sigs = _signals(conn)
    summary = {"signals": len(sigs), "priced": 0, "written": 0, "immature": 0, "no_price": 0}
    if not sigs:
        log.info("intraday-outcomes: no signals (none carry a precise published_ts yet)")
        return summary
    for s in sigs:
        s["act"] = _actionable_et(s["published_ts"])

    tickers = {s["ticker"] for s in sigs} | {s["benchmark"] for s in sigs}
    acts = [s["act"] for s in sigs]
    idmin = min(acts).strftime("%Y-%m-%d")
    idmax = (max(acts) + timedelta(days=4)).strftime("%Y-%m-%d")
    intraday_prices.ensure_intraday(conn, sorted(tickers), idmin, idmax)
    # Daily closes for the exits.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dstart = (min(acts) - timedelta(days=7)).strftime("%Y-%m-%d")
    prices.ensure_prices(conn, sorted(tickers), dstart, today, refresh_days=int(max(horizons) * 1.7) + 10)

    intra: dict[str, list] = {}
    bench_open: dict[str, dict] = {}
    for t in tickers:
        bars = [(datetime.fromisoformat(ts), o, c) for ts, o, c in store.intraday_bars(conn, t)]
        intra[t] = bars
        bench_open[t] = {dt: o for dt, o, _ in bars}
    dclose: dict[str, dict] = {}
    ddates: dict[str, list] = {}
    for t in tickers:
        ser = store.price_ohlc(conn, t)                 # (date, adj_close, adj_open)
        dclose[t] = {d: c for d, c, _ in ser}
        ddates[t] = [d for d, _, _ in ser]

    direct = signal_outcomes._direct_news_days(conn)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cost = COST_BPS / 10000.0
    out_rows = []
    for s in sigs:
        tk, bm, pos = s["ticker"], s["benchmark"], s["direction"] == "positive"
        sbars = intra.get(tk, [])
        if not sbars or not intra.get(bm):
            summary["no_price"] += 1
            continue
        entry = next(((dt, o) for dt, o, _ in sbars if dt >= s["act"]), None)
        if entry is None:                               # no bar yet after the action time
            summary["immature"] += 1
            continue
        entry_dt, entry_open = entry
        b_entry_open = bench_open[bm].get(entry_dt)
        if b_entry_open is None:                         # benchmark has no bar at that hour
            summary["no_price"] += 1
            continue
        entry_date = entry_dt.date().isoformat()
        dd, tcl, bcl = ddates.get(tk, []), dclose.get(tk, {}), dclose.get(bm, {})
        if entry_date not in tcl:                        # entry day's daily close not available yet
            summary["immature"] += 1
            continue
        ei = dd.index(entry_date)
        summary["priced"] += 1
        node_direct = direct.get(s["node_id"], set())
        lo_inclusive = s["hop"] != 0
        for h in horizons:
            xi = ei + h
            if xi >= len(dd):
                summary["immature"] += 1
                continue
            exit_date = dd[xi]
            s_exit, b_exit = tcl.get(exit_date), bcl.get(exit_date)
            if s_exit is None or b_exit is None:
                continue
            cap = s_exit / entry_open - 1.0
            bench = b_exit / b_entry_open - 1.0
            abn = cap - bench
            hit = 1 if ((pos and abn > 0) or (not pos and abn < 0)) else 0
            net = (abn if pos else -abn) - cost
            clean = 1
            for d in node_direct:
                if ((entry_date <= d) if lo_inclusive else (entry_date < d)) and d <= exit_date:
                    clean = 0
                    break
            out_rows.append({
                "event_id": s["event_id"], "node_id": s["node_id"], "ticker": tk, "hop": s["hop"],
                "direction": s["direction"], "magnitude": s["magnitude"], "entry_ts": entry_dt.isoformat(),
                "entry_date": entry_date, "horizon_days": h, "stock_ret": round(cap, 6),
                "benchmark": bm, "benchmark_ret": round(bench, 6), "abn_ret": round(abn, 6),
                "hit": hit, "net_cap": round(net, 6), "net_hit": 1 if net > 0 else 0,
                "cost_bps": COST_BPS, "latency_min": LATENCY_MIN, "clean": clean, "computed_at": now})
    summary["written"] = store.upsert_intraday_outcomes(conn, out_rows)
    log.info("intraday-outcomes: %s", summary)
    return summary
