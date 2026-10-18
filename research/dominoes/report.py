"""Stage: the readout (markdown tables) and the average CAR-path figure (spec §10 Output).

Reads results_<period>*.json, power*.json and paths_<period>.parquet from the research store and writes
docs/research/domino-study/readout-<period>.md plus figures/paths-<period>.png. It formats what measure
computed and decides nothing: every pass/fail shown here was set by measure under the frozen rules.

The exploration readout ends with an integrity table (sha256 of every exploration results file and power file).
The holdout gate reads that table from the COMMITTED readout and opens only if the store still matches it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import subprocess
from pathlib import Path

import pandas as pd

from research.dominoes import config, io, measure, snapshot_links

DOC_DIR = io.REPO_ROOT / "docs" / "research" / "domino-study"
WLABEL = {"pre": "[-5,-1]", "d0": "[0,0]", "w5": "[+1,+5]", "w20": "[+1,+20]", "w60": "[+1,+60]"}
ROLE_COLOR = {"supplier": "#1a9e8f", "customer": "#7b5ea7", "competitor": "#f07c6c"}   # CLAUDE.md palette
SUBSETS = (
    ("r4_ex_covid", "R4 excluding 2020-02-15 to 2020-06-30 (COVID crash)"),
    ("r5_ex_industry_shocks", "R5 excluding industry-wide shocks (|sector ETF z| ≥ 2)"),
    ("r7_k3", "R7 K = 3 controls"),
    ("r7_k10", "R7 K = 10 controls"),
    ("r8_llm_only", "R8 LLM-extracted links only"),
    ("r8_manual_only", "R8 manual links only"),
)
_RERUN = ("strengthen the map (10-K supplier/customer links with revenue weights) and re-run under this same "
          "frozen protocol")
OVERALL = {
    "slow_ripple": ("Slow ripple confirmed",
                    "Slow ripples are real and large enough to study. Next: Stage 2 (predict the ripple) and "
                    "Stage 3 (measure how fast prices absorb it, with intraday data)."),
    "same_day_only": ("Same-day ripple only",
                      "The market prices the link on the same day. Any value sits in a latency race where faster "
                      "data alone is unlikely to be enough; Stage 2 is not justified for trading."),
    "same_day_some_inconclusive": ("Same-day ripple; slow ripple inconclusive for some roles",
                                   "For the roles where the slow test had power, the market prices the link on the "
                                   "same day and Stage 2 is not justified for trading. The other roles are "
                                   f"inconclusive: {_RERUN}."),
    "no_ripple": ("No ripple",
                  "The map's links don't carry measurable price ripples. Close the price-ripple thesis for these "
                  "link types."),
    "no_ripple_powered_roles": ("No ripple where the test could decide; other roles inconclusive",
                                "No ripple among the roles where the test had power and could be evaluated. The "
                                f"other roles are inconclusive: {_RERUN}."),
    "inconclusive": ("Inconclusive", f"No role could be decided (too little power or an unevaluable check): {_RERUN}."),
}
ROLE_STATUS = {
    "slow_ripple": "slow ripple (confirmed)",
    "same_day_slow_inconclusive": "same-day ripple confirmed; slow ripple inconclusive",
    "inconclusive_underpowered": "underpowered / inconclusive",
    "inconclusive_unevaluable": "inconclusive (a required check could not be evaluated)",
    "same_day_only": "same-day only",
    "no_ripple": "no ripple",
}


def _fin(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _pct(x, signed: bool = True) -> str:
    if not _fin(x):
        return "n/a"
    return f"{x * 100:+.2f}%" if signed else f"{x * 100:.2f}%"


def _t(x) -> str:
    return f"{x:.2f}" if _fin(x) else "n/a"


def _g(x) -> str:
    return f"{x:.4f}" if _fin(x) else "n/a"


def _p(x) -> str:
    if not _fin(x):
        return "n/a"
    return "<0.0001" if x < 1e-4 else f"{x:.4f}"


def _yn(b) -> str:
    return "n/a" if b is None else ("yes" if b else "no")


def _mt(c: dict) -> str:
    return f"{_pct(c.get('mean'))} (t {_t(c.get('t'))})"


def _cellstr(x) -> str:
    return str(x).replace("|", "\\|")        # a raw '|' inside a cell would split it (GFM)


def _table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(_cellstr(h) for h in header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(_cellstr(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


_ROLE_ACTION = {
    "slow_ripple": "slow ripple confirmed, real and large enough to study: go to Stage 2 (predict the ripple) and "
                   "Stage 3 (how fast prices absorb it, with intraday data)",
    "same_day_only": "the market prices the link on the same day; any value sits in a latency race, so Stage 2 is "
                     "not justified for trading",
    "same_day_slow_inconclusive": "B follows A on the same day (confirmed), but the slow-ripple test is inconclusive "
                                  f"(too little power or an unevaluable check): {_RERUN}",
    "no_ripple": "no ripple: close the price-ripple thesis for these link types",
    "inconclusive_underpowered": f"inconclusive (too little power): {_RERUN}",
    "inconclusive_unevaluable": f"inconclusive (a required check could not be evaluated): {_RERUN}",
}


def overall_meaning(v: dict) -> str:
    """§7 consequences per group of roles, built from the role states (never a fixed sentence that could
    contradict the per-role table)."""
    groups: dict[str, list] = {}
    for r in config.ROLES:
        groups.setdefault(v["by_role"][r], []).append(r)
    return " ".join(f"{', '.join(groups[s])}: {_ROLE_ACTION[s]}." for s in _ROLE_ACTION if s in groups)


def h3_holdout_text(h3: dict, t_exploration) -> str:
    """Combined pre-trend status, naming the period(s) that flagged or could not be tested."""
    def per(status_key: str, label: str, t) -> str | None:
        s = h3.get(status_key)
        if s == "flag":
            return f"{label} t {_t(t)}"
        if s == "untestable":
            return f"{label} untestable"
        return None
    parts = [p for p in (per("status_exploration", "exploration", t_exploration),
                         per("status_holdout", "holdout", h3.get("t_pre"))) if p]
    s = {"ok": "ok", "flag": "FLAG", "untestable": "could not be tested"}.get(h3["status"], h3["status"])
    if parts and h3["status"] != "ok":
        s += f" ({'; '.join(parts)})"
    return s + ("" if h3.get("applies") else " (not required)")


def _commit() -> str:
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=io.REPO_ROOT, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def _load(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _mde(power: dict | None, role: str, w: str = "w20", key: str = "mde_bootstrap") -> str:
    if not power:
        return "n/a"
    m = power["roles"][role][w].get(key, math.nan)
    if key == "mde_bootstrap" and not _fin(m):
        return f"> {config.POWER_DELTAS[-1] * 100:.2f}%"
    return _pct(m, signed=False)


def _power_note(power: dict, role: str) -> str:
    r = power["roles"][role]
    if r["underpowered"]:
        return " (underpowered)"
    if r.get("borderline"):
        return " (power borderline: " + "; ".join(r.get("borderline_reasons", [])) + ")"
    return ""


def integrity_lines(store: Path) -> list[str]:
    """Machine-readable sha256 table of the exploration results and power files (read back by the gate)."""
    files = sorted(p for p in Path(store).glob("*.json") if p.name.startswith(("results_exploration", "power")))
    rows = [f"| `{p.name}` | {hashlib.sha256(p.read_bytes()).hexdigest()} |" for p in files]
    return ["| File | sha256 |", "|---|---|", *rows]


# ----------------------------------------------------------------------------------------------- figures
def plot_paths(paths: pd.DataFrame, png: Path, title: str) -> Path:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    png.parent.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), sharex=True)
    panels = [("all", "First domino A: s·CAR(A)")] + [(r, f"{r}: s·CAR, linked B vs look-alikes") for r in config.ROLES]
    for ax, (role, label) in zip(axes.ravel(), panels):
        sub = paths[paths["role"] == role]
        for series, style in (("A", {"color": "#555555", "lw": 2}),
                              ("B", {"color": ROLE_COLOR.get(role, "#555555"), "lw": 1.6}),
                              ("controls", {"color": "#9a9a9a", "lw": 1.2}),
                              ("ripple", {"color": "#111111", "lw": 2.2, "ls": "--"})):
            s = sub[sub["series"] == series].sort_values("day")
            if len(s):
                n = int(s["n"].max())
                name = {"A": "A", "B": "linked B", "controls": "controls (mean of K=5)",
                        "ripple": "ripple = B − controls"}[series]
                ax.plot(s["day"], s["car"] * 100, label=f"{name} (n≤{n})", **style)
        ax.axvline(0, color="#cccccc", lw=0.8)
        ax.axhline(0, color="#cccccc", lw=0.8)
        ax.set_title(label, fontsize=10)
        ax.set_ylabel("cumulative abnormal return, %")
        if ax.lines:
            ax.legend(fontsize=8, loc="best")
    role_axes = [ax for ax in axes.ravel()[1:] if ax.lines]
    if role_axes:                    # one y-scale across roles, so noise in a thin role can't look like signal
        lo = min(ax.get_ylim()[0] for ax in role_axes)
        hi = max(ax.get_ylim()[1] for ax in role_axes)
        for ax in role_axes:
            ax.set_ylim(lo, hi)
    for ax in axes[-1]:
        ax.set_xlabel("trading days relative to A's shock (day 0)")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(png, dpi=110)
    plt.close(fig)
    return png


# ------------------------------------------------------------------------------------------- data notes
def _data_notes(store: Path, counts: dict) -> list[str]:
    lines = []
    man = _load(store / "fetch_manifest.json")
    if man:
        lines.append(f"- Prices: yfinance, total-return adjusted for returns; {man['start']} to {man['end']} "
                     f"(end exclusive); {len(man['fetched'])} symbols fetched, unavailable: "
                     f"{', '.join(man['failed']) or 'none'}.")
    if io.frame_path("prices").exists():
        from research.dominoes import fetch_prices
        p = fetch_prices.load_prices()
        n = p.groupby("symbol").size()
        thin = sorted(s for s, k in n.items() if k < config.MIN_EST_DAYS and s not in config.SECTOR_ETFS)
        if thin:
            lines.append(f"- Symbols with fewer than {config.MIN_EST_DAYS} price rows in the Yahoo snapshot (never "
                         f"usable; likely delisted, taken private or re-tickered — the cause was not verified): "
                         f"{', '.join(thin)}. Survivorship: firms missing from the snapshot are missing here (§11).")
        first = p[p["symbol"].isin(config.SECTOR_ETFS)].groupby("symbol")["date"].min()
        late = {s: pd.Timestamp(d).date().isoformat() for s, d in first.items()
                if pd.Timestamp(d) > pd.Timestamp(config.PRICE_START) + pd.Timedelta(days=30)}
        if late:
            lines.append("- Sector ETFs that start late (robustness 1 cannot use those sectors before then plus a "
                         "~1-year estimation window): " + ", ".join(f"{s} from {d}" for s, d in sorted(late.items()))
                         + ".")
    sanity = _load(store / "raw_sanity.json")
    if sanity:
        lines.append(f"- Dollar volume uses a separate unadjusted-close snapshot (no dividend look-ahead): "
                     f"{sanity['common_rows']} common rows; volume mismatches {sanity['volume_mismatch_rows']}; "
                     f"adjusted-above-raw rows {sanity['ratio_violation_rows']}; symbols without raw history "
                     f"{len(sanity['symbols_missing_raw'])} (their dollar volume is missing, never imputed).")
    if io.frame_path("links").exists():
        links = io.read_frame("links", snapshot_links.LINK_SCHEMA)
        by_role = links["role"].value_counts().to_dict()
        multi = int((links.groupby(["a_id", "b_id"])["role"].nunique() > 1).sum())
        lines.append("- Study links (directed, 'if A shocks, B is A's role'): "
                     + ", ".join(f"{r} {by_role.get(r, 0)}" for r in config.ROLES)
                     + f"; ordered pairs holding more than one role: {multi}.")
    ev = counts["events"]
    lines.append("- Events (shock at A, linked B, role): " + ", ".join(f"{r} {ev[r]}" for r in config.ROLES)
                 + f"; distinct event dates {counts['event_dates']}"
                 + (f"; first-domino shocks {counts['shocks']}." if "shocks" in counts else "."))
    return lines


def _h3_text(status: str, t) -> str:
    return {"ok": "ok", "flag": f"FLAG (t {_t(t)})", "untestable": "could not be tested"}.get(status, status)


# ------------------------------------------------------------------------------------------ exploration
def _verdict_rows_exploration(res: dict, power: dict) -> list[list]:
    rows = []
    for role in config.ROLES:
        cands = [w for w in config.SLOW_WINDOWS if res["H2"][f"{role}|{w}"]["pass_exploration"]]
        h1 = res["H1"][role]["pass_exploration"]
        under = power["roles"][role]["underpowered"]
        if cands:
            status = "slow-ripple candidate → holdout"
        elif h1:
            status = "same-day candidate → holdout" + (" (slow ripple underpowered)" if under else "")
        elif under:
            status = "underpowered / inconclusive"
        else:
            status = "no candidate"
        pre = res["H3"][role]
        rows.append([role, _mde(power, role) + _power_note(power, role), "candidate" if h1 else "—",
                     ", ".join(WLABEL[w] for w in cands) or "—",
                     _h3_text(pre["status_exploration"], pre["t_pre"]) + ("" if pre["applies"] else " (not required)"),
                     status])
    return rows


def _summary_exploration(res: dict, power: dict) -> list[str]:
    c = res["counts"]
    total = sum(c["events"].values())
    out = [f"- Exploration years {config.EXPLORATION[0][:4]}–{config.EXPLORATION[1][:4]}: {c.get('shocks', 'n/a')} "
           f"first-domino price shocks gave {total} shock → linked-company events on {c['event_dates']} days."]
    cands = [(k, v) for k, v in res["H2"].items() if v["pass_exploration"]]
    if cands:
        out.append("- Slow-ripple candidates that cleared the strict exploration bar (Holm + |t| > 3): "
                   + "; ".join(f"{k.split('|')[0]} {WLABEL[k.split('|')[1]]} {_pct(v['mean'])} (t {_t(v['t'])})"
                               for k, v in cands) + ".")
        weak = [k for k, v in cands if v.get("flag_2w")]
        if weak:
            out.append("- Caution (exploratory diagnostic): with standard errors clustered by both date and linked "
                       f"firm, these candidates fall below |t| = 3: {', '.join(weak)}. Overlapping windows of the "
                       "same firm may overstate them; the holdout and its placebo decide.")
    else:
        out.append("- No slow ripple cleared the strict exploration bar, so the holdout cannot confirm one.")
    h1 = [r for r in config.ROLES if res["H1"][r]["pass_exploration"]]
    out.append("- Same-day candidates: " + (", ".join(h1) if h1 else "none") + ".")
    under = [r for r in config.ROLES if power["roles"][r]["underpowered"]]
    border = [r for r in config.ROLES if power["roles"][r].get("borderline")]
    out.append("- Power: the smallest 20-day ripple the data reliably detects is "
               + ", ".join(f"{_mde(power, r)} ({r})" for r in config.ROLES)
               + (f"; underpowered (> {config.MDE_LIMIT * 100:.1f}%): {', '.join(under)}" if under
                  else "; every role passes the power rule")
               + (f"; power borderline (see the power table): {', '.join(border)}." if border else "."))
    flagged = [r for r in config.ROLES if res["H3"][r]["applies"] and res["H3"][r]["status_exploration"] == "flag"]
    untest = [r for r in config.ROLES if res["H3"][r]["applies"] and res["H3"][r]["status_exploration"] == "untestable"]
    if flagged:
        out.append(f"- Pre-trend flag (B moved before A's shock): {', '.join(flagged)} — possible shared "
                   "information rather than transmission.")
    if untest:
        out.append(f"- Pre-trend could not be tested for: {', '.join(untest)}.")
    out.append("- None of this is proof yet. Exploration only nominates candidates; the verdict comes from the "
               "locked 2022+ holdout.")
    return out


def _h2_rows_exploration(res: dict) -> list[list]:
    rows = []
    for role in config.ROLES:
        for w in config.SLOW_WINDOWS:
            h = res["H2"][f"{role}|{w}"]
            rows.append([role, WLABEL[w], _pct(h["mean"]), _t(h["t"]), _p(h["p_two"]), _yn(h["holm"]),
                         _yn(_fin(h["t"]) and abs(h["t"]) > config.T_BAR),
                         "**PASS**" if h["pass_exploration"] else "fail",
                         _t(h.get("t_2w")) + (" ⚠" if h.get("flag_2w") else "")])
    return rows


def _h4_rows(table: dict) -> list[list]:
    rows = []
    for role in config.ROLES:
        for w in config.WINDOWS:
            c = table[role][w]
            ci = c.get("ci_boot") or [math.nan, math.nan]
            rows.append([role, WLABEL[w], _pct(c["mean"]), _t(c["t"]), _t(c.get("t_week")),
                         f"{_pct(ci[0])} to {_pct(ci[1])}", _t(c.get("t_2w")), c["n"], c["g"]])
    return rows


def _variant_rows(variants: dict, period: str) -> list[list]:
    rows = []
    for variant, rule, label in config.ROBUSTNESS_RUNS:
        res = variants.get(label)
        for role in config.ROLES:
            if res is None:
                rows.append([label, role, "not run", "not run", "not run"])
                continue
            cells = []
            for w in config.SLOW_WINDOWS:
                h = res["H2"][f"{role}|{w}"]
                if period == "exploration":
                    cells.append(f"{_mt(h)}{' ✓' if h.get('pass_exploration') else ''}")
                else:
                    cells.append(f"{_pct(h.get('direction_mean'))} in dir. (p {_p(h.get('p_one'))})"
                                 f"{' ✓' if h.get('pass') else ''}")
            rows.append([label, role, *cells])
    return rows


def _subset_rows(rob: dict) -> list[list]:
    rows = []
    for key, label in SUBSETS:
        for role in config.ROLES:
            t = rob[key][role]
            rows.append([label, role, *[_mt(t[w]) for w in config.SLOW_WINDOWS], t["w20"]["n"]])
    return rows


def _power_rows(power: dict) -> list[list]:
    rows = []
    for r in config.ROLES:
        pr = power["roles"][r]
        w = pr["w20"]
        conc = w.get("concentration", {})
        rows.append([r, pr["n_events"], pr["n_dates"], _pct(w["se"], signed=False),
                     *[_mde(power, r, x) for x in config.SLOW_WINDOWS],
                     _pct(w.get("mde_ucb80"), signed=False), _pct(w.get("mde_twoway"), signed=False),
                     f"{conc.get('g_eff_events', math.nan):.0f} / {conc.get('g_eff_variance', math.nan):.0f}",
                     _yn(pr["underpowered"]), "; ".join(pr.get("borderline_reasons", [])) or "—"])
    return rows


def _projection_rows(power: dict) -> list[list]:
    rows = []
    for r in config.ROLES:
        proj = power["roles"][r]["w20"].get("holdout_projection")
        if not proj:
            continue
        rows.append([r, proj["t_explore_days"], proj["t_holdout_days"], _pct(proj["mde_holdout_floor"], False),
                     _pct(proj["mde_joint"], False) if _fin(proj["mde_joint"]) else "> 3.00%",
                     _pct(proj["joint_power_at_limit"], False)])
    return rows


def render_exploration(res: dict, power: dict, variants: dict, figure: str, data_notes: list[str],
                       integrity: list[str]) -> str:
    L = [f"# Domino study — exploration readout ({config.EXPLORATION[0][:4]}–{config.EXPLORATION[1][:4]})", "",
         f"Generated {dt.date.today().isoformat()} from code commit `{_commit()}` under the frozen "
         "pre-registration (`docs/superpowers/specs/2026-10-18-domino-study-design.md`, incl. its §13 log). "
         f"The holdout ({config.HOLDOUT_START[:4]}+) stays **LOCKED** until this readout is committed; the gate then "
         "checks the integrity table at the end against the research store.", "",
         "Question: when one company has a large price shock of its own, do its graph-linked suppliers, customers "
         "and competitors move more than matched unlinked look-alikes, on the same day or slowly afterwards? "
         "Ripple = s·[CAR(B) − mean CAR(look-alikes)], so a positive ripple means B followed A's direction.", "",
         "## Plain-language summary", "", *_summary_exploration(res, power), "",
         "## Verdict (provisional — exploration only)", "",
         _table(["Role", "Power: 20-day MDE", "Same-day (H1)", "Slow-ripple candidates (H2)", "Pre-trend (H3)",
                 "Status"], _verdict_rows_exploration(res, power)), "",
         "## Data", "", *data_notes]
    emb = res["counts"].get("embargoed")
    if emb:
        L += ["- Embargo: exploration windows ending after 2021-12-31 are not measured (events per window): "
              + "; ".join(f"{r} " + ", ".join(f"{WLABEL[w]} {emb[r][w]}" for w in config.SLOW_WINDOWS)
                          for r in config.ROLES) + "."]
    L += ["", "## Power check (spec §5, run before any hypothesis test)", "",
          "Smallest synthetic 20-day ripple detected in 80% of date-bootstrap replicates under the full exploration "
          f"rule; above {config.MDE_LIMIT * 100:.1f}% a role is underpowered / inconclusive, never 'no effect'. The "
          "80%-upper-bound and two-way MDEs, the effective number of dates (by events / by variance) and the "
          "borderline notes are descriptive caveats, fixed before any outcome was measured.", "",
          _table(["Role", "Events", "Dates", "SE [+1,+20]", "MDE [+1,+5]", "MDE [+1,+20]", "MDE [+1,+60]",
                  "MDE upper bound", "MDE two-way", "Effective dates", "Underpowered", "Borderline because"],
                 _power_rows(power)), ""]
    proj = _projection_rows(power)
    if proj:
        L += ["Projected holdout power ([+1,+20], calendar arithmetic only — no holdout data): the ripple the "
              "holdout leg can confirm (one-sided p < 0.05 and the 50 bp floor), the ripple the full exploration × "
              "holdout chain detects 80% of the time, and the chain's power at 1.5%.", "",
              _table(["Role", "Exploration days", "Holdout days", "Holdout-leg MDE", "Full-chain MDE",
                      "Full-chain power at 1.5%"], proj), ""]
    L += ["## H1 — same-day ripple", "",
          f"Pass in exploration: mean Ripple[0,0] > 0 with t > {config.T_BAR:.0f} (date-clustered).", "",
          _table(["Role", "Mean [0,0]", "t", "Exploration"],
                 [[r, _pct(res["H1"][r]["mean"]), _t(res["H1"][r]["t"]),
                   "candidate" if res["H1"][r]["pass_exploration"] else "fail"] for r in config.ROLES]), "",
          "## H2 — slow ripple (primary family: 3 roles × 3 windows)", "",
          f"Pass in exploration: Holm at family α = {config.FAMILY_ALPHA} on two-sided date-clustered p, and "
          f"|t| > {config.T_BAR:.0f}. Passing tests go to the holdout in their exploration direction. The last "
          "column is an exploratory diagnostic (t clustered by date AND linked firm; ⚠ = a pass below |t| = 3).", "",
          _table(["Role", "Window", "Mean", "t (date)", "p two-sided", "Holm", f"|t| > {config.T_BAR:.0f}",
                  "Exploration", "t two-way"], _h2_rows_exploration(res)), "",
          "## H3 — pre-trend", "",
          f"Ripple[-5,-1] must have |t| < {config.PRETREND_T:.0f} for any role that passes H1 or H2; otherwise the "
          "role is flagged as possible shared information rather than transmission. 'Could not be tested' is never "
          "read as ok.", "",
          _table(["Role", "Mean [-5,-1]", "t", "Status", "Required"],
                 [[r, _pct(res["table"][r]["pre"]["mean"]), _t(res["H3"][r]["t_pre"]),
                   _h3_text(res["H3"][r]["status_exploration"], res["H3"][r]["t_pre"]),
                   _yn(res["H3"][r]["applies"])] for r in config.ROLES]), "",
          "## H4 — direction by role (descriptive)", "",
          "Every role × window, equal-weighted across events. t (date) is the primary clustering; t (week) and "
          "the date-block bootstrap 95% CI are robustness; t two-way is an exploratory diagnostic.", "",
          _table(["Role", "Window", "Mean", "t (date)", "t (week)", "95% CI (bootstrap)", "t two-way", "Events",
                  "Dates"], _h4_rows(res["table"])), "",
          "## H5 — first-domino drift (descriptive)", "",
          "s·CAR(A) after the shock day: positive = continuation, negative = reversal.", "",
          _table(["Window", "Mean s·CAR(A)", "t (date)", "Shocks"],
                 [[WLABEL[w], _pct(c["mean"]), _t(c["t"]), c["n"]] for w, c in res["H5"].items()]), "",
          "## H6 — what makes ripples bigger (hypothesis-generating, exploration only)", "",
          "A pattern here can only be claimed if it is registered in spec §13 before the holdout opens and then "
          f"confirmed in the holdout. {res['H6']['_notes']['b_size_rule']}; events in thinner years: "
          f"{res['H6']['_notes']['b_size_excluded_events']}.", ""]
    for split, groups in res["H6"].items():
        if split.startswith("_"):
            continue
        rows = []
        for g, tab in groups.items():
            for r in config.ROLES:
                rows.append([g, r, *[_mt(tab[r][w]) for w in config.SLOW_WINDOWS], tab[r]["w20"]["n"]])
        L += [f"### {split}", "", _table(["Group", "Role", "[+1,+5]", "[+1,+20]", "[+1,+60]", "Events"], rows), ""]
    rob = res["robustness"]
    r10 = rob["r10_ex_b_coshock"]
    L += ["## Transmission coefficient γ", "",
          "OLS of [CAR(B) − mean CAR(look-alikes)] on AR(A, 0), clustered by date: the share of A's shock that "
          "shows up in B beyond what look-alikes show.", "",
          _table(["Role", "Window", "γ", "t", "Events"],
                 [[r, WLABEL[w], _g(g["slope"]), _t(g["t"]), g["n"]]
                  for r in config.ROLES for w, g in res["gamma"][r].items()]), "",
          "## Robustness (spec §8 — reported, never changes the verdict)", "",
          "### R1–R3: model and shock-rule variants (H2 family re-run; ✓ = passes the exploration rule in that "
          "variant)", "",
          _table(["Check", "Role", "[+1,+5]", "[+1,+20]", "[+1,+60]"], _variant_rows(variants, "exploration")), "",
          "### R4, R5, R7, R8: subsets and K", "",
          f"R8 counts a link carrying both LLM and manual provenance in both subsets ({rob['r8_overlap_events']} "
          "events).", "",
          _table(["Check", "Role", "[+1,+5]", "[+1,+20]", "[+1,+60]", "Events [+1,+20]"], _subset_rows(rob)), "",
          "### R9: raw linked and control moves on the ripple's own sample (how much is industry-wide)", "",
          _table(["Role", "Window", "Linked B s·CAR", "Look-alikes s·CAR", "Ripple"],
                 [[r, WLABEL[w], _mt(rob["r9_raw_moves"]["linked_b"][r][w]),
                   _mt(rob["r9_raw_moves"]["controls"][r][w]), _mt(res["table"][r][w])]
                  for r in config.ROLES for w in ("d0",) + config.SLOW_WINDOWS]), "",
          "### R10 (exploratory, not pre-registered): without events where B had its own shock in [e−10, e]", "",
          "Share of events affected: " + ", ".join(f"{r} {_pct(r10['share_coshock'][r], False)}"
                                                   for r in config.ROLES) + ".", "",
          _table(["Role", "[+1,+5]", "[+1,+20]", "[+1,+60]", "Events [+1,+20]"],
                 [[r, *[_mt(r10["table"][r][w]) for w in config.SLOW_WINDOWS], r10["table"][r]["w20"]["n"]]
                  for r in config.ROLES]), "",
          "## Figures", "", "Average cumulative abnormal return paths, day −5 to +60, on events with a complete "
          "60-day ripple (exploration).", "", f"![CAR paths]({figure})", "",
          "## Integrity", "",
          "sha256 of the exploration results and power files this readout was written from. The holdout gate "
          "compares these against the research store before any holdout event is built.", "", *integrity, ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------------------------- holdout
def uncommitted_leads(expl: dict, hold: dict) -> list[str]:
    """Holdout slow-window moves that look significant but were NOT nominated by exploration: reported as leads
    for a future pre-registered test, never as findings."""
    out = []
    for role in config.ROLES:
        for w in config.SLOW_WINDOWS:
            if expl["H2"][f"{role}|{w}"]["pass_exploration"]:
                continue
            c = hold["table"][role][w]
            p = c.get("p_two")
            sig = (p < 0.05) if _fin(p) else (_fin(c.get("t")) and abs(c["t"]) > 1.96)
            if sig:
                floor = " and is below the 0.5% floor" if _fin(c["mean"]) and abs(c["mean"]) < config.FLOOR else ""
                out.append(f"- Descriptively, {role} {WLABEL[w]} moved {_pct(c['mean'])} (t {_t(c['t'])}) in the "
                           f"holdout, but it was not nominated by exploration{floor}, so it cannot be claimed — it "
                           "is a lead for a new pre-registered test on fresh data.")
    return out


def same_day_caveat(v: dict) -> str:
    if not any(s in ("same_day_only", "same_day_slow_inconclusive") for s in v["by_role"].values()):
        return ""
    return ("- What 'same day' does and does not show: a same-day ripple cannot separate A→B transmission from "
            "shared same-day news or finer sub-industry co-movement, because look-alikes are matched only on GICS "
            "sector. It is measured close to close, so it says nothing about speed within the day.")


def _summary_holdout(expl: dict, hold: dict, power: dict) -> list[str]:
    title = OVERALL[hold["verdict"]["overall"]][0]
    out = [f"- **{title}.** {overall_meaning(hold['verdict'])}"]
    cands = [k for k, v in expl["H2"].items() if v["pass_exploration"]]
    for k in cands:
        h = hold["H2"][k]
        role, w = k.split("|")
        if h["pass"]:
            out.append(f"- {role} {WLABEL[w]}: confirmed out of sample — {_pct(h['direction_mean'])} in the "
                       f"exploration direction (one-sided p {_p(h['p_one'])}), above the {config.FLOOR * 100:.1f}% "
                       "floor, and beyond what random same-sector links produce."
                       + (" Caution: the exploratory two-way test does not confirm it." if h.get("flag_2w") else ""))
        else:
            pl = h.get("placebo", {})
            reasons = []
            if not _fin(h.get("p_one")):
                reasons.append("could not be evaluated (holdout t/p missing)")
            else:
                if not h["replicates"]:
                    reasons.append("did not replicate")
                if not h["floor_ok"]:
                    reasons.append(f"below the {config.FLOOR * 100:.1f}% floor")
            if pl.get("run") and not pl.get("evaluable", True):
                reasons.append(pl.get("reason") or "placebo not evaluable")
            elif h["beats_placebo"] is False and pl.get("run"):
                reasons.append("did not beat the placebo")
            out.append(f"- {role} {WLABEL[w]}: not confirmed ({'; '.join(reasons) or 'see the H2 table'}); holdout "
                       f"mean {_pct(h['direction_mean'])} in the exploration direction.")
    if not cands:
        out.append("- No slow-ripple candidate came out of exploration, so the holdout had nothing to confirm.")
    for r in config.ROLES:
        if hold["H1"][r]["pass_exploration"]:
            out.append(f"- Same-day {r}: {'confirmed' if hold['H1'][r]['pass'] else 'not confirmed'} "
                       f"(holdout mean {_pct(hold['H1'][r]['mean'])}).")
    for r in config.ROLES:
        d = hold["verdict"]["details"][r]
        if d["borderline"] and hold["verdict"]["by_role"][r] in ("no_ripple", "same_day_only"):
            out.append(f"- {r}: '{ROLE_STATUS[hold['verdict']['by_role'][r]]}' with power borderline — "
                       + "; ".join(d["borderline_reasons"]) + ".")
    flagged = [r for r in config.ROLES if hold["H3"][r]["applies"] and hold["H3"][r]["status"] == "flag"]
    untest = [r for r in config.ROLES if hold["H3"][r]["applies"] and hold["H3"][r]["status"] == "untestable"]
    if flagged:
        out.append(f"- Pre-trend flag in at least one period: {', '.join(flagged)} — any ripple there may be "
                   "shared information rather than transmission.")
    if untest:
        out.append(f"- Pre-trend could not be tested for: {', '.join(untest)}.")
    caveat = same_day_caveat(hold["verdict"])
    if caveat:
        out.append(caveat)
    out += uncommitted_leads(expl, hold)
    out.append("- Research only: close-to-close returns, no trading costs beyond the 50 bp floor, no news latency.")
    return out


def render_holdout(expl: dict, hold: dict, power: dict, variants: dict, figure: str, data_notes: list[str]) -> str:
    v = hold["verdict"]
    title, meaning = OVERALL[v["overall"]][0], overall_meaning(v)
    chain = []
    for role in config.ROLES:
        for w in config.SLOW_WINDOWS:
            h = hold["H2"][f"{role}|{w}"]
            pl = h["placebo"]
            if not h["pass_exploration"]:                 # nothing was tested: show the raw holdout move only
                c = hold["table"][role][w]
                chain.append([role, WLABEL[w], "no", "—", f"{_pct(c['mean'])} (raw)", f"{_t(c['t'])} (raw)", "—",
                              "—", "—", "not run", "—", "not a candidate", "—"])
                continue
            if pl.get("run"):
                plc = (f"{_pct(pl.get('p95'))} (n {pl.get('n_finite')}/{pl.get('n')}"
                       + ("" if pl.get("evaluable") else ", not evaluable") + ")")
            else:
                plc = "not run"
            chain.append([role, WLABEL[w], _yn(h["pass_exploration"]), "+" if h["direction"] > 0 else "−",
                          _pct(h["direction_mean"]), _t(h["direction"] * h["t"] if _fin(h["t"]) else math.nan),
                          _p(h["p_one"]), _yn(h["replicates"]),
                          _yn(h["floor_ok"]), plc, _yn(h["beats_placebo"]),
                          "**PASS**" if h["pass"] else ("inconclusive" if h.get("unevaluable") else "fail"),
                          _p(h.get("p_one_2w")) + (" ⚠" if h.get("flag_2w") else "")])
    diag = []
    for key, h in hold["H2"].items():
        pl = h["placebo"]
        if pl.get("run"):
            diag.append([key.replace("|", " "), pl.get("n_finite"), f"{pl.get('median_coverage', 0):.0f} / "
                         f"{pl.get('real_count')}", f"{pl.get('pool_median', math.nan):.0f} (min {pl.get('pool_min')})",
                         _pct(pl.get("dropped_link_share"), False), _pct(pl.get("p50")), _pct(pl.get("p95")),
                         _t(pl.get("sd_ratio")) + (" ⚠" if pl.get("sd_ratio_flag") else ""),
                         pl.get("reason") or "ok"])
    proj = {r: power["roles"][r]["w20"].get("holdout_projection") or {} for r in config.ROLES}
    L = [f"# Domino study — holdout readout ({config.HOLDOUT_START[:4]}+)", "",
         f"Generated {dt.date.today().isoformat()} from code commit `{_commit()}`. The exploration readout "
         "(`readout-exploration.md`) was committed before any holdout event was built, and the gate verified its "
         "integrity table against the research store.", "",
         "## Plain-language summary", "", *_summary_holdout(expl, hold, power), "",
         "## Verdict (spec §7)", "", f"**Overall: {title}.** {meaning}", "",
         _table(["Role", "Verdict", "Power: 20-day MDE", "Full-chain power at 1.5% (projected)", "Pre-trend"],
                [[r, ROLE_STATUS[v["by_role"][r]], _mde(power, r) + _power_note(power, r),
                  _pct(proj[r].get("joint_power_at_limit"), False),
                  h3_holdout_text(hold["H3"][r], expl["H3"][r]["t_pre"])]
                 for r in config.ROLES]), "",
         "## Data", "", *data_notes, "",
         "## H2 — slow ripple, holdout chain", "",
         "A test passes only if all hold: it passed exploration; the holdout has the same sign with one-sided "
         f"p < {config.HOLDOUT_ALPHA}; the holdout mean is at least {config.FLOOR * 100:.1f}% in that direction; "
         "and it beats the 95th percentile of the placebo (each linked firm relabelled to one random same-sector "
         "firm that is not linked to the shocked firm). Candidates' means and t are shown in the exploration "
         "direction; rows that were not exploration candidates were not tested and show the raw holdout move only. "
         "The last column is the exploratory two-way (date × linked firm) one-sided p (⚠ = a pass it does not "
         "confirm).", "",
         _table(["Role", "Window", "Exploration pass", "Direction", "Holdout mean (in direction)", "t (in direction)",
                 "p one-sided", "Replicates", f"≥ {config.FLOOR * 100:.1f}%", "Placebo 95th pct (in direction)",
                 "Beats placebo", "Result", "p two-way"], chain), ""]
    if diag:
        L += ["Placebo diagnostics (evaluable only with ≥ 90% finite permutations and median coverage ≥ 50% of the "
              "real events; SD ratio = placebo SD / real date-clustered SE, ⚠ below 0.8):", "",
              _table(["Test", "Finite", "Coverage", "Pool size", "Links dropped", "p50", "p95", "SD ratio",
                      "Evaluable"], diag), ""]
    def _h1_row(r: str) -> list:
        h = hold["H1"][r]
        tx = expl["table"][r]["d0"].get("t_2w")
        p2 = hold["table"][r]["d0"].get("p_one_pos_2w")
        warn_x = " ⚠" if h["pass_exploration"] and not (_fin(tx) and abs(tx) > config.T_BAR) else ""
        warn_h = " ⚠" if h["pass"] and not (_fin(p2) and p2 < config.HOLDOUT_ALPHA) else ""
        return [r, "candidate" if h["pass_exploration"] else "fail", _pct(h["mean"]), _t(h["t"]), _p(h["p_one"]),
                "**PASS**" if h["pass"] else "fail", _t(tx) + warn_x, _p(p2) + warn_h]

    L += ["## H1 — same-day ripple, holdout", "",
          "The last two columns are the exploratory two-way (date × linked firm) diagnostics: the exploration t "
          "(⚠ = a candidate below |t| = 3) and the holdout one-sided p (⚠ = a pass it does not confirm).", "",
          _table(["Role", "Exploration", "Holdout mean [0,0]", "t", "p one-sided", "Result",
                  "Exploration t two-way", "Holdout p two-way"], [_h1_row(r) for r in config.ROLES]), "",
          "## H3 — pre-trend in both periods", "",
          _table(["Role", "t exploration", "t holdout", "Status", "Required"],
                 [[r, _t(expl["H3"][r]["t_pre"]), _t(hold["H3"][r]["t_pre"]),
                   h3_holdout_text(hold["H3"][r], expl["H3"][r]["t_pre"]), _yn(hold["H3"][r]["applies"])]
                  for r in config.ROLES]), "",
          "## H4 — direction by role, holdout (descriptive)", "",
          _table(["Role", "Window", "Mean", "t (date)", "t (week)", "95% CI (bootstrap)", "t two-way", "Events",
                  "Dates"], _h4_rows(hold["table"])), "",
          "## H5 — first-domino drift, holdout (descriptive)", "",
          _table(["Window", "Mean s·CAR(A)", "t (date)", "Shocks"],
                 [[WLABEL[w], _pct(c["mean"]), _t(c["t"]), c["n"]] for w, c in hold.get("H5", {}).items()]), "",
          "## H6 — registered follow-ups (spec §13; registered before the holdout opened, never verdict inputs)", "",
          _table(["Id", "Hypothesis", "Holdout mean", "t", "p one-sided", "Events", "Rest of the role", "Result"],
                 [[k, f["text"], _pct(f["mean"]), _t(f["t"]), _p(f["p_one"]), f["n"],
                   f"{_pct(f['complement_mean'])} (t {_t(f['complement_t'])})",
                   "**CONFIRMED**" if f["pass"] else "not confirmed"]
                  for k, f in hold.get("H6_followups", {}).items()]), "",
          "## Robustness (spec §8 — reported, never changes the verdict)", "",
          f"### R6: holdout restricted to {config.HOLDOUT_LATE_START[:4]} onward", "",
          _table(["Role", "[+1,+5]", "[+1,+20]", "[+1,+60]", "Events [+1,+20]"],
                 [[r, *[_mt(hold["robustness"]["r6_holdout_2024_on"][r][w]) for w in config.SLOW_WINDOWS],
                   hold["robustness"]["r6_holdout_2024_on"][r]["w20"]["n"]] for r in config.ROLES]), "",
          "### R1–R3: model and shock-rule variants in the holdout (✓ = the variant passed its own exploration "
          "rule, replicates one-sided p < 0.05 and clears the 50 bp floor; no placebo)", "",
          _table(["Check", "Role", "[+1,+5]", "[+1,+20]", "[+1,+60]"], _variant_rows(variants, "holdout")), ""]
    r10 = hold["robustness"].get("r10_ex_b_coshock")
    if r10:
        L += ["### R10 (exploratory): without events where B had its own shock in [e−10, e]", "",
              "Share of events affected: " + ", ".join(f"{r} {_pct(r10['share_coshock'][r], False)}"
                                                       for r in config.ROLES) + ".", "",
              _table(["Role", "[+1,+5]", "[+1,+20]", "[+1,+60]", "Events [+1,+20]"],
                     [[r, *[_mt(r10["table"][r][w]) for w in config.SLOW_WINDOWS], r10["table"][r]["w20"]["n"]]
                      for r in config.ROLES]), ""]
    L += ["## Figures", "", "Average cumulative abnormal return paths, day −5 to +60, on events with a complete "
          "60-day ripple (holdout).", "", f"![CAR paths]({figure})", ""]
    return "\n".join(L)


# -------------------------------------------------------------------------------------------------- stage
def run(period: str = "exploration", out_dir: str | Path | None = None) -> dict:
    if period not in ("exploration", "holdout"):
        raise ValueError(f"unknown period {period!r}")
    if period == "holdout":
        io.require_holdout_unlocked()
    out = Path(out_dir) if out_dir else DOC_DIR
    store = io.store_dir()
    expl = _load(store / "results_exploration.json")
    if expl is None:
        raise FileNotFoundError("results_exploration.json missing: run `measure --period exploration` first")
    power = _load(store / "power.json")
    if power is None:
        raise FileNotFoundError("power.json missing: the readout cannot state power (spec §5)")
    variants = {label: _load(store / f"results_{period}{config.variant_suffix(v, r)}.json")
                for v, r, label in config.ROBUSTNESS_RUNS}
    variants = {k: x for k, x in variants.items() if x is not None}
    paths = io.read_frame(f"paths_{period}", measure.PATH_SCHEMA)
    fig_rel = f"figures/paths-{period}.png"
    plot_paths(paths, out / fig_rel, f"Domino study — average CAR paths ({period})")
    if period == "exploration":
        text = render_exploration(expl, power, variants, fig_rel, _data_notes(store, expl["counts"]),
                                  integrity_lines(store))
    else:
        hold = _load(store / "results_holdout.json")
        if hold is None:
            raise FileNotFoundError("results_holdout.json missing: run `measure --period holdout` first")
        text = render_holdout(expl, hold, power, variants, fig_rel, _data_notes(store, hold["counts"]))
    doc = out / f"readout-{period}.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(text, encoding="utf-8")
    return {"readout": str(doc), "figure": str(out / fig_rel), "variants": sorted(variants)}
