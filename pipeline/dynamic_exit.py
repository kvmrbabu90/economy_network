"""Reverse-signal dynamic-exit backtest (Phase B).

Instead of holding to a FIXED horizon, hold while the graph's net consensus on the node still
supports the entry's direction, and EXIT when a reverse signal flips it (or the story decays out).
The node's net signed impact magnitude is replayed with the SAME 7-day window / 3-day half-life
decay the live aggregate uses (no look-ahead: at day t only events published on/before t count):

    net_mag(t) = Σ over events on the node with 0 <= age <= WINDOW:  signed_mag * 0.5**(age/HALFLIFE)

Entry = next session open after the announcement (the daily model). Walk forward trading days;
exit at the first day where thesis strength = net_mag * sign(direction) <= THRESH (reverse/decay),
or at MAX_HOLD (cap). Return is measured entry_open -> exit_close, direction-signed and net of a
round-trip cost. Writes dynamic_outcomes.

    from pipeline import dynamic_exit
    dynamic_exit.compute_dynamic_outcomes(conn)
"""
from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone

from schema import store
from pipeline import prices, signal_outcomes

log = logging.getLogger(__name__)

WIN = int(os.environ.get("IMPACT_WINDOW_DAYS", "7"))          # match the live aggregate
HL = float(os.environ.get("IMPACT_HALFLIFE_DAYS", "3"))
THRESH = float(os.environ.get("RELIABILITY_DYN_THRESH", "0.0"))   # exit when thesis strength <= this
MAX_HOLD = int(os.environ.get("RELIABILITY_DYN_MAXHOLD", "10"))   # trading-day cap
COST_BPS = float(os.environ.get("RELIABILITY_COST_BPS", "10"))
_LOOKBACK_PAD_DAYS = 7


def _node_events(conn) -> dict:
    """node_id -> sorted [(date, signed_magnitude)] over ALL traced directional impacts (any hop):
    the consensus the reverse-exit trigger tracks."""
    out: dict[str, list] = {}
    for r in conn.execute(
        """SELECT ei.node_id, date(COALESCE(NULLIF(e.published_at,''), e.ingested_at)) d,
                  ei.direction, ei.magnitude
           FROM event_impacts ei JOIN events e ON e.id=ei.event_id
           WHERE ei.direction IN ('positive','negative') AND e.status='traced'"""):
        if r[1]:
            signed = r[3] if r[2] == "positive" else -r[3]
            out.setdefault(r[0], []).append((r[1], signed))
    for v in out.values():
        v.sort()
    return out


def _net_mag(events: list, t: date) -> float:
    """Net signed magnitude on day `t` with the window+half-life decay (events on/before t only)."""
    tot = 0.0
    for d_iso, signed in events:
        age = (t - date.fromisoformat(d_iso)).days
        if 0 <= age <= WIN:
            tot += signed * (0.5 ** (age / HL))
    return tot


def compute_dynamic_outcomes(conn, max_hold: int = MAX_HOLD, thresh: float = THRESH) -> dict:
    sigs = signal_outcomes._signals(conn)
    summary = {"signals": len(sigs), "traded": 0, "written": 0, "immature": 0, "no_price": 0}
    if not sigs:
        log.info("dynamic-exit: no signals"); return summary

    start = min(s["pub"] for s in sigs)
    start = datetime.fromordinal(datetime.strptime(start, "%Y-%m-%d").toordinal() - _LOOKBACK_PAD_DAYS).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tickers = {s["ticker"] for s in sigs} | {s["benchmark"] for s in sigs}
    prices.ensure_prices(conn, sorted(tickers), start, today, refresh_days=int(max_hold * 1.7) + 20)

    close: dict[str, dict] = {}
    opens: dict[str, dict] = {}
    dates: dict[str, list] = {}
    for t in tickers:
        ser = store.price_ohlc(conn, t)
        close[t] = {d: c for d, c, _ in ser}
        opens[t] = {d: o for d, _, o in ser if o is not None}
        dates[t] = [d for d, _, _ in ser]

    nevents = _node_events(conn)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cost = COST_BPS / 10000.0
    out_rows = []
    for s in sigs:
        tk, bm, pub = s["ticker"], s["benchmark"], s["pub"]
        sign = 1.0 if s["direction"] == "positive" else -1.0
        tdates, tcl, topen = dates.get(tk, []), close.get(tk, {}), opens.get(tk, {})
        bcl, bopen = close.get(bm, {}), opens.get(bm, {})
        if not tdates or not bcl or not bopen:
            summary["no_price"] += 1
            continue
        ai = signal_outcomes._base_index(tdates, pub)
        if ai is None:
            summary["immature"] += 1
            continue
        entry_i = ai if tdates[ai] > pub else ai + 1
        if entry_i >= len(tdates):
            summary["immature"] += 1
            continue
        entry_date = tdates[entry_i]
        entry_open, b_entry_open = topen.get(entry_date), bopen.get(entry_date)
        if entry_open is None or b_entry_open is None:
            summary["no_price"] += 1
            continue
        nev = nevents.get(s["node_id"], [])
        entry_mag = _net_mag(nev, date.fromisoformat(entry_date)) * sign

        # Walk forward: exit when thesis strength <= thresh (reverse/decay) or MAX_HOLD.
        exit_i = None
        reason = None
        for j in range(1, max_hold + 1):
            ti = entry_i + j
            if ti >= len(tdates):
                break                                  # not enough price data to reach an exit yet
            thesis = _net_mag(nev, date.fromisoformat(tdates[ti])) * sign
            if thesis <= thresh:
                exit_i, reason = ti, ("reverse" if thesis < 0 else "faded")
                break
            if j == max_hold:
                exit_i, reason = ti, "maxhold"
                break
        if exit_i is None:
            summary["immature"] += 1
            continue
        exit_date = tdates[exit_i]
        s_exit, b_exit = tcl.get(exit_date), bcl.get(exit_date)
        if s_exit is None or b_exit is None:
            summary["no_price"] += 1
            continue
        summary["traded"] += 1
        stock_ret = s_exit / entry_open - 1.0
        bench_ret = b_exit / b_entry_open - 1.0
        abn = stock_ret - bench_ret
        dir_abn = abn * sign
        net = dir_abn - cost
        out_rows.append({
            "event_id": s["event_id"], "node_id": s["node_id"], "ticker": tk, "hop": s["hop"],
            "direction": s["direction"], "magnitude": s["magnitude"], "entry_date": entry_date,
            "exit_date": exit_date, "holding_days": exit_i - entry_i, "exit_reason": reason,
            "stock_ret": round(stock_ret, 6), "benchmark": bm, "benchmark_ret": round(bench_ret, 6),
            "abn_ret": round(abn, 6), "hit": 1 if dir_abn > 0 else 0, "net_cap": round(net, 6),
            "net_hit": 1 if net > 0 else 0, "entry_mag": round(entry_mag, 4),
            "cost_bps": COST_BPS, "max_hold": max_hold, "computed_at": now})
    summary["written"] = store.upsert_dynamic_outcomes(conn, out_rows)
    log.info("dynamic-exit: %s", summary)
    return summary
