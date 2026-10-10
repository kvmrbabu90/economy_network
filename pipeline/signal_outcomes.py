"""Compute per-signal market outcomes for the reliability layer.

For each propagation signal (an `event_impacts` row on a TICKERED node with a directional
verdict), measure the node's ABNORMAL return (stock minus sector-ETF benchmark) at a panel of
forward horizons, with no look-ahead. Writes `signal_outcomes`. Idempotent + incremental: a
signal x horizon is computed only once enough future price data exists (maturity gating), so
re-running fills in newly-mature outcomes.

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
# Lead days before the first signal so the base-date lookup always has a trading day to land on.
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
    # Refresh a window at least as long as the longest horizon's calendar span so a maturing
    # signal's base and end bars share ONE split/dividend-adjustment baseline (else multi-day
    # returns are biased by a provider restatement between the two fetches). ~1.7 cal/trading day.
    refresh_days = int(max(horizons) * 1.7) + 10
    prices.ensure_prices(conn, sorted(tickers), start, today, refresh_days=refresh_days)

    # In-memory series + date lists, built once.
    series: dict[str, dict] = {}
    dates: dict[str, list] = {}
    for t in tickers:
        ser = store.price_series(conn, t)
        series[t] = {d: c for d, c in ser}
        dates[t] = [d for d, _ in ser]

    direct_days = _direct_news_days(conn)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out_rows = []
    for s in sigs:
        tk, bm = s["ticker"], s["benchmark"]
        tdates, bseries = dates.get(tk, []), series.get(bm, {})
        if not tdates or not bseries:
            summary["no_price"] += 1
            continue
        bi = _base_index(tdates, s["pub"])
        if bi is None:
            summary["immature"] += 1
            continue
        base_date = tdates[bi]
        base_px = series[tk][base_date]
        base_bm = bseries.get(base_date)
        if base_bm is None:
            summary["no_price"] += 1
            continue
        summary["priced"] += 1
        node_direct = direct_days.get(s["node_id"], set())
        for h in horizons:
            ei = bi + h
            if ei >= len(tdates):
                summary["immature"] += 1
                continue
            end_date = tdates[ei]
            end_bm = bseries.get(end_date)
            if end_bm is None:
                continue
            stock_ret = series[tk][end_date] / base_px - 1.0
            bench_ret = end_bm / base_bm - 1.0
            abn = stock_ret - bench_ret
            hit = 1 if ((s["direction"] == "positive" and abn > 0) or
                        (s["direction"] == "negative" and abn < 0)) else 0
            # clean = node had no OWN direct (hop-0) news in the window, other than this event.
            # For a propagated (hop>=1) signal the signal itself is NOT hop-0, so include the base
            # day — same-day own direct news (breaking after the base close) drives the base->end
            # return and must mark the sample contaminated. For a hop-0 signal, exclude the base
            # day so the signal doesn't flag ITSELF (its own event is dated on base_date).
            lo_inclusive = s["hop"] != 0
            clean = 1
            for d in node_direct:
                if ((base_date <= d) if lo_inclusive else (base_date < d)) and d <= end_date:
                    clean = 0
                    break
            out_rows.append({
                "event_id": s["event_id"], "node_id": s["node_id"], "ticker": tk, "hop": s["hop"],
                "direction": s["direction"], "magnitude": s["magnitude"], "base_date": base_date,
                "horizon_days": h, "stock_ret": round(stock_ret, 6), "benchmark": bm,
                "benchmark_ret": round(bench_ret, 6), "abn_ret": round(abn, 6), "hit": hit,
                "clean": clean, "computed_at": now})
    summary["written"] = store.upsert_signal_outcomes(conn, out_rows)
    log.info("outcomes: %s", summary)
    return summary
