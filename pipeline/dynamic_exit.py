"""Reverse-signal dynamic-exit backtest (Phase B).

Instead of holding to a FIXED horizon, hold while the graph's net consensus on the node still
supports the entry's direction, and EXIT when a reverse signal flips it (or the story decays out).
The node's net signed impact magnitude is replayed to MATCH the live aggregate (same 7-day window,
3-day half-life decay, hop-distance attenuation, and same-source dedup), with no look-ahead (at
day t only events dated on/before t count):

    contrib = magnitude * IMPACT_HOP_WEIGHT**hop * 0.5**(age/HALFLIFE),  collapsed to the strongest
    copy per (seed_node, day, direction); net_mag(t) = Σ signed contribs over 0 <= age <= WINDOW.

Entry = next session OPEN after the announcement. Walk forward trading days; the thesis strength =
net_mag * sign(direction) crossing <= THRESH (reverse/decay) is the trigger, or MAX_HOLD caps it.
Then EXIT at the NEXT session's open after the trigger day (same no-look-ahead discipline as the
entry — a reverse event dated the trigger day may publish after its close). Return is measured
entry_open -> exit_open, direction-signed and net of a round-trip cost. Writes dynamic_outcomes.

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
HOP_WEIGHT = float(os.environ.get("IMPACT_HOP_WEIGHT", "0.5"))   # match aggregate's hop attenuation
THRESH = float(os.environ.get("RELIABILITY_DYN_THRESH", "0.0"))   # exit when thesis strength <= this
MAX_HOLD = int(os.environ.get("RELIABILITY_DYN_MAXHOLD", "10"))   # trading-day cap
COST_BPS = float(os.environ.get("RELIABILITY_COST_BPS", "10"))
_LOOKBACK_PAD_DAYS = 7


def _node_events(conn) -> dict:
    """node_id -> [(date, direction, magnitude, hop, seed_node_id)] over ALL traced directional
    impacts — the consensus the reverse-exit trigger tracks. Carries hop + seed so the trajectory
    can apply the SAME hop attenuation and same-source dedup the live aggregate does."""
    out: dict[str, list] = {}
    for r in conn.execute(
        """SELECT ei.node_id, date(COALESCE(NULLIF(e.published_at,''), e.ingested_at)) d,
                  ei.direction, ei.magnitude, ei.hop, e.seed_node_id
           FROM event_impacts ei JOIN events e ON e.id=ei.event_id
           WHERE ei.direction IN ('positive','negative') AND e.status='traced'"""):
        if r[1]:
            out.setdefault(r[0], []).append((r[1], r[2], r[3], r[4] or 0, r[5] or ""))
    return out


def _net_mag(events: list, t: date) -> float:
    """Net signed magnitude on day `t`, matching the live aggregate: hop-distance attenuation
    (IMPACT_HOP_WEIGHT**hop) + half-life decay, with same-source (seed, day, direction) duplicate
    stories collapsed to the single strongest copy. Events dated on/before `t` only (no look-ahead)."""
    groups: dict[tuple, tuple] = {}     # (seed, day, direction) -> (strongest contrib, direction)
    for d_iso, direction, mag, hop, seed in events:
        age = (t - date.fromisoformat(d_iso)).days
        if not (0 <= age <= WIN):
            continue
        contrib = mag * (HOP_WEIGHT ** hop) * (0.5 ** (age / HL))
        key = (seed, d_iso, direction)
        if contrib > groups.get(key, (0.0, direction))[0]:
            groups[key] = (contrib, direction)
    return sum(c if d == "positive" else -c for c, d in groups.values())


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

        # Walk forward: the thesis crossing <= thresh (reverse/decay) is the TRIGGER, capped at
        # MAX_HOLD. We then EXIT at the NEXT session's open after the trigger day (no look-ahead:
        # a reverse event dated the trigger day may publish after its close).
        trigger_i = None
        reason = None
        for j in range(1, max_hold + 1):
            ti = entry_i + j
            if ti >= len(tdates):
                break                                  # not enough data to evaluate the trigger yet
            thesis = _net_mag(nev, date.fromisoformat(tdates[ti])) * sign
            if thesis <= thresh:
                trigger_i, reason = ti, ("reverse" if thesis < 0 else "faded")
                break
            if j == max_hold:
                trigger_i, reason = ti, "maxhold"
                break
        if trigger_i is None:
            summary["immature"] += 1
            continue
        exit_i = trigger_i + 1                          # act the session after the trigger, at the open
        if exit_i >= len(tdates):
            summary["immature"] += 1
            continue
        exit_date = tdates[exit_i]
        s_exit, b_exit = topen.get(exit_date), bopen.get(exit_date)
        if s_exit is None or b_exit is None:
            summary["no_price"] += 1
            continue
        summary["traded"] += 1
        stock_ret = s_exit / entry_open - 1.0           # entry open -> exit open
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
