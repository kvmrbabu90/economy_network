"""Stage: the readout (markdown tables) and the average CAR-path figure (spec §10 Output).

Reads results_<period>*.json, power.json and paths_<period>.parquet from the research store and writes
docs/research/domino-study/readout-<period>.md plus figures/paths-<period>.png. It formats what measure
computed and decides nothing: every pass/fail shown here was set by measure under the frozen rules.
"""
from __future__ import annotations

import datetime as dt
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
OVERALL = {
    "slow_ripple": ("Slow ripple confirmed",
                    "Slow ripples are real and large enough to study. Next: Stage 2 (predict the ripple) and "
                    "Stage 3 (measure how fast prices absorb it, with intraday data)."),
    "same_day_only": ("Same-day ripple only",
                      "The market prices the link on the same day. Any value sits in a latency race where faster "
                      "data alone is unlikely to be enough; Stage 2 is not justified for trading."),
    "no_ripple": ("No ripple",
                  "The map's links don't carry measurable price ripples. Close the price-ripple thesis for these "
                  "link types."),
    "no_ripple_powered_roles": ("No ripple where the data had power; other roles inconclusive",
                                "No ripple among the adequately powered roles. The underpowered roles are "
                                "inconclusive: strengthen the map (10-K supplier/customer links with revenue "
                                "weights) and re-run under this same frozen protocol."),
}
ROLE_STATUS = {"slow_ripple": "slow ripple (confirmed)", "same_day_only": "same-day only",
               "inconclusive_underpowered": "underpowered / inconclusive", "no_ripple": "no ripple"}


def _fin(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _pct(x, signed: bool = True) -> str:
    if not _fin(x):
        return "n/a"
    return f"{x * 100:+.2f}%" if signed else f"{x * 100:.2f}%"


def _t(x) -> str:
    return f"{x:.2f}" if _fin(x) else "n/a"


def _p(x) -> str:
    if not _fin(x):
        return "n/a"
    return "<0.0001" if x < 1e-4 else f"{x:.4f}"


def _yn(b) -> str:
    return "n/a" if b is None else ("yes" if b else "no")


def _mt(c: dict) -> str:
    return f"{_pct(c.get('mean'))} (t {_t(c.get('t'))})"


def _table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return "\n".join(out)


def _commit() -> str:
    r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=io.REPO_ROOT, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "unknown"


def _load(path: Path) -> dict | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _mde(power: dict | None, role: str, w: str = "w20") -> str:
    if not power:
        return "n/a"
    m = power["roles"][role][w]["mde_bootstrap"]
    return _pct(m, signed=False) if _fin(m) else f"> {config.POWER_DELTAS[-1] * 100:.2f}%"


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
        lines.append(f"- Prices: yfinance (split/dividend adjusted), {man['start']} to {man['end']}; "
                     f"{len(man['fetched'])} symbols fetched, {len(man['failed'])} unavailable.")
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


# ------------------------------------------------------------------------------------------ exploration
def _verdict_rows_exploration(res: dict, power: dict | None) -> list[list]:
    rows = []
    for role in config.ROLES:
        cands = [w for w in config.SLOW_WINDOWS if res["H2"][f"{role}|{w}"]["pass_exploration"]]
        h1 = res["H1"][role]["pass_exploration"]
        under = bool(power and power["roles"][role]["underpowered"])
        if cands:
            status = "slow-ripple candidate → holdout"
        elif h1:
            status = "same-day candidate → holdout"
        elif under:
            status = "underpowered / inconclusive"
        else:
            status = "no candidate"
        pre = res["H3"][role]
        rows.append([role, _mde(power, role) + (" (underpowered)" if under else ""),
                     "candidate" if h1 else "—", ", ".join(WLABEL[w] for w in cands) or "—",
                     "ok" if pre["ok_exploration"] else f"FLAG (t {_t(pre['t_pre'])})", status])
    return rows


def _summary_exploration(res: dict, power: dict | None) -> list[str]:
    c = res["counts"]
    total = sum(c["events"].values())
    out = [f"- Exploration years {config.EXPLORATION[0][:4]}–{config.EXPLORATION[1][:4]}: {c.get('shocks', 'n/a')} "
           f"first-domino price shocks gave {total} shock → linked-company events on {c['event_dates']} days."]
    cands = [(k, v) for k, v in res["H2"].items() if v["pass_exploration"]]
    if cands:
        out.append("- Slow-ripple candidates that cleared the strict exploration bar (Holm + |t| > 3): "
                   + "; ".join(f"{k.split('|')[0]} {WLABEL[k.split('|')[1]]} {_pct(v['mean'])} (t {_t(v['t'])})"
                               for k, v in cands) + ".")
    else:
        out.append("- No slow ripple cleared the strict exploration bar, so the holdout cannot confirm one.")
    h1 = [r for r in config.ROLES if res["H1"][r]["pass_exploration"]]
    out.append("- Same-day candidates: " + (", ".join(h1) if h1 else "none") + ".")
    if power:
        under = [r for r in config.ROLES if power["roles"][r]["underpowered"]]
        out.append("- Power: the smallest 20-day ripple the data reliably detects is "
                   + ", ".join(f"{_mde(power, r)} ({r})" for r in config.ROLES)
                   + (f"; underpowered (> {config.MDE_LIMIT * 100:.1f}%): {', '.join(under)}." if under
                      else "; every role is adequately powered."))
    flagged = [r for r in config.ROLES if not res["H3"][r]["ok_exploration"]]
    if flagged:
        out.append(f"- Pre-trend flag (B moved before A's shock): {', '.join(flagged)} — possible shared "
                   "information rather than transmission.")
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
                         "**PASS**" if h["pass_exploration"] else "fail"])
    return rows


def _h4_rows(table: dict) -> list[list]:
    rows = []
    for role in config.ROLES:
        for w in config.WINDOWS:
            c = table[role][w]
            ci = c.get("ci_boot") or [math.nan, math.nan]
            rows.append([role, WLABEL[w], _pct(c["mean"]), _t(c["t"]), _t(c.get("t_week")),
                         f"{_pct(ci[0])} to {_pct(ci[1])}", c["n"], c["g"]])
    return rows


def _variant_rows(variants: dict, key: str) -> list[list]:
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
                ok = h.get(key)
                cells.append(f"{_mt(h)}{' ✓' if ok else ''}")
            rows.append([label, role, *cells])
    return rows


def _subset_rows(rob: dict) -> list[list]:
    rows = []
    for key, label in SUBSETS:
        for role in config.ROLES:
            t = rob[key][role]
            rows.append([label, role, *[_mt(t[w]) for w in config.SLOW_WINDOWS], t["w20"]["n"]])
    return rows


def render_exploration(res: dict, power: dict | None, variants: dict, figure: str, data_notes: list[str]) -> str:
    L = [f"# Domino study — exploration readout ({config.EXPLORATION[0][:4]}–{config.EXPLORATION[1][:4]})", "",
         f"Generated {dt.date.today().isoformat()} from code commit `{_commit()}` under the frozen "
         "pre-registration (`docs/superpowers/specs/2026-10-18-domino-study-design.md`). "
         f"The holdout ({config.HOLDOUT_START[:4]}+) stays **LOCKED** until this readout is committed; its commit "
         "hash is then written to the holdout marker.", "",
         "Question: when one company has a large price shock of its own, do its graph-linked suppliers, customers "
         "and competitors move more than matched unlinked look-alikes, on the same day or slowly afterwards? "
         "Ripple = s·[CAR(B) − mean CAR(look-alikes)], so a positive ripple means B followed A's direction.", "",
         "## Plain-language summary", "", *_summary_exploration(res, power), "",
         "## Verdict (provisional — exploration only)", "",
         _table(["Role", "Power: 20-day MDE", "Same-day (H1)", "Slow-ripple candidates (H2)", "Pre-trend (H3)",
                 "Status"], _verdict_rows_exploration(res, power)), "",
         "## Data", "", *data_notes, "",
         "## Power check (spec §5, run before any hypothesis test)", "",
         "Smallest synthetic 20-day ripple detected in 80% of date-bootstrap replicates under the full exploration "
         f"rule; above {config.MDE_LIMIT * 100:.1f}% a role is underpowered / inconclusive, never 'no effect'.", ""]
    if power:
        rows = [[r, power["roles"][r]["n_events"], power["roles"][r]["n_dates"],
                 _pct(power["roles"][r]["w20"]["se"], signed=False),
                 *[_mde(power, r, w) for w in config.SLOW_WINDOWS],
                 _yn(power["roles"][r]["underpowered"])] for r in config.ROLES]
        L += [_table(["Role", "Events", "Dates", "SE [+1,+20]", "MDE [+1,+5]", "MDE [+1,+20]", "MDE [+1,+60]",
                      "Underpowered"], rows)]
    else:
        L += ["Power check not found in the store."]
    L += ["", "## H1 — same-day ripple", "",
          f"Pass in exploration: mean Ripple[0,0] > 0 with t > {config.T_BAR:.0f} (date-clustered).", "",
          _table(["Role", "Mean [0,0]", "t", "Exploration"],
                 [[r, _pct(res["H1"][r]["mean"]), _t(res["H1"][r]["t"]),
                   "candidate" if res["H1"][r]["pass_exploration"] else "fail"] for r in config.ROLES]), "",
          "## H2 — slow ripple (primary family: 3 roles × 3 windows)", "",
          f"Pass in exploration: Holm at family α = {config.FAMILY_ALPHA} on two-sided date-clustered p, and "
          f"|t| > {config.T_BAR:.0f}. Passing tests go to the holdout in their exploration direction.", "",
          _table(["Role", "Window", "Mean", "t (date)", "p two-sided", "Holm", f"|t| > {config.T_BAR:.0f}",
                  "Exploration"], _h2_rows_exploration(res)), "",
          "## H3 — pre-trend", "",
          f"Ripple[-5,-1] must have |t| < {config.PRETREND_T:.0f}; otherwise the role is flagged as possible "
          "shared information rather than transmission.", "",
          _table(["Role", "Mean [-5,-1]", "t", "OK"],
                 [[r, _pct(res["table"][r]["pre"]["mean"]), _t(res["H3"][r]["t_pre"]),
                   _yn(res["H3"][r]["ok_exploration"])] for r in config.ROLES]), "",
          "## H4 — direction by role (descriptive)", "",
          "Every role × window, equal-weighted across events. t (date) is the primary clustering; t (week) and "
          "the date-block bootstrap 95% CI are robustness.", "",
          _table(["Role", "Window", "Mean", "t (date)", "t (week)", "95% CI (bootstrap)", "Events", "Dates"],
                 _h4_rows(res["table"])), "",
          "## H5 — first-domino drift (descriptive)", "",
          "s·CAR(A) after the shock day: positive = continuation, negative = reversal.", "",
          _table(["Window", "Mean s·CAR(A)", "t (date)", "Shocks"],
                 [[WLABEL[w], _pct(c["mean"]), _t(c["t"]), c["n"]] for w, c in res["H5"].items()]), "",
          "## H6 — what makes ripples bigger (hypothesis-generating, exploration only)", "",
          "A pattern here can only be claimed if it is registered in spec §13 before the holdout opens and then "
          "confirmed in the holdout.", ""]
    for split, groups in res["H6"].items():
        rows = []
        for g, tab in groups.items():
            for r in config.ROLES:
                rows.append([g, r, *[_mt(tab[r][w]) for w in config.SLOW_WINDOWS], tab[r]["w20"]["n"]])
        L += [f"### {split}", "", _table(["Group", "Role", "[+1,+5]", "[+1,+20]", "[+1,+60]", "Events"], rows), ""]
    L += ["## Transmission coefficient γ", "",
          "OLS of [CAR(B) − mean CAR(look-alikes)] on AR(A, 0), clustered by date: the share of A's shock that "
          "shows up in B beyond what look-alikes show.", "",
          _table(["Role", "Window", "γ", "t", "Events"],
                 [[r, WLABEL[w], _t(g["slope"]), _t(g["t"]), g["n"]]
                  for r in config.ROLES for w, g in res["gamma"][r].items()]), "",
          "## Robustness (spec §8 — reported, never changes the verdict)", "",
          "### R1–R3: model and shock-rule variants (H2 family re-run; ✓ = passes the exploration rule)", "",
          _table(["Check", "Role", "[+1,+5]", "[+1,+20]", "[+1,+60]"],
                 _variant_rows(variants, "pass_exploration")), "",
          "### R4, R5, R7, R8: subsets and K", "",
          _table(["Check", "Role", "[+1,+5]", "[+1,+20]", "[+1,+60]", "Events [+1,+20]"],
                 _subset_rows(res["robustness"])), "",
          "### R9: raw linked and control moves (how much is industry-wide)", "",
          _table(["Role", "Window", "Linked B s·CAR", "Look-alikes s·CAR", "Ripple"],
                 [[r, WLABEL[w], _mt(res["robustness"]["r9_raw_moves"]["linked_b"][r][w]),
                   _mt(res["robustness"]["r9_raw_moves"]["controls"][r][w]), _mt(res["table"][r][w])]
                  for r in config.ROLES for w in ("d0",) + config.SLOW_WINDOWS]), "",
          "## Figures", "", "Average cumulative abnormal return paths, day −5 to +60 (exploration events).", "",
          f"![CAR paths]({figure})", ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------------------------- holdout
def _summary_holdout(expl: dict, hold: dict) -> list[str]:
    title, meaning = OVERALL[hold["verdict"]["overall"]]
    out = [f"- **{title}.** {meaning}"]
    cands = [k for k, v in expl["H2"].items() if v["pass_exploration"]]
    for k in cands:
        h = hold["H2"][k]
        role, w = k.split("|")
        if h["pass"]:
            out.append(f"- {role} {WLABEL[w]}: confirmed out of sample — {_pct(h['mean'])} in the exploration "
                       f"direction (one-sided p {_p(h['p_one'])}), above the {config.FLOOR * 100:.1f}% floor, and "
                       "beyond what random same-sector links produce.")
        else:
            why = [n for n, ok in (("did not replicate", h["replicates"]),
                                   (f"below the {config.FLOOR * 100:.1f}% floor", h["floor_ok"]),
                                   ("did not beat the placebo", h["beats_placebo"] is not False)) if not ok]
            out.append(f"- {role} {WLABEL[w]}: not confirmed ({'; '.join(why)}); holdout mean {_pct(h['mean'])}.")
    if not cands:
        out.append("- No slow-ripple candidate came out of exploration, so the holdout had nothing to confirm.")
    for r in config.ROLES:
        if hold["H1"][r]["pass_exploration"]:
            out.append(f"- Same-day {r}: {'confirmed' if hold['H1'][r]['pass'] else 'not confirmed'} "
                       f"(holdout mean {_pct(hold['H1'][r]['mean'])}).")
    flagged = [r for r in config.ROLES if not hold["H3"][r]["ok"]]
    if flagged:
        out.append(f"- Pre-trend flag in at least one period: {', '.join(flagged)} — any ripple there may be "
                   "shared information rather than transmission.")
    out.append("- Research only: close-to-close returns, no trading costs beyond the 50 bp floor, no news latency.")
    return out


def render_holdout(expl: dict, hold: dict, power: dict | None, variants: dict, figure: str,
                   data_notes: list[str]) -> str:
    v = hold["verdict"]
    title, meaning = OVERALL[v["overall"]]
    chain = []
    for role in config.ROLES:
        for w in config.SLOW_WINDOWS:
            h = hold["H2"][f"{role}|{w}"]
            pl = h["placebo"]
            chain.append([role, WLABEL[w], _yn(h["pass_exploration"]), "+" if h["direction"] > 0 else "−",
                          _pct(h["mean"]), _t(h["t"]), _p(h["p_one"]), _yn(h["replicates"]), _yn(h["floor_ok"]),
                          _pct(pl.get("p95")) if pl.get("run") else "not run", _yn(h["beats_placebo"]),
                          "**PASS**" if h["pass"] else "fail"])
    L = [f"# Domino study — holdout readout ({config.HOLDOUT_START[:4]}+)", "",
         f"Generated {dt.date.today().isoformat()} from code commit `{_commit()}`. The exploration readout "
         "(`readout-exploration.md`) was committed before any holdout event was built.", "",
         "## Plain-language summary", "", *_summary_holdout(expl, hold), "",
         "## Verdict (spec §7)", "", f"**Overall: {title}.** {meaning}", "",
         _table(["Role", "Verdict", "Power: 20-day MDE"],
                [[r, ROLE_STATUS[v["by_role"][r]], _mde(power, r)] for r in config.ROLES]), "",
         "## Data", "", *data_notes, "",
         "## H2 — slow ripple, holdout chain", "",
         "A test passes only if all hold: it passed exploration; the holdout has the same sign with one-sided "
         f"p < {config.HOLDOUT_ALPHA}; the holdout mean is at least {config.FLOOR * 100:.1f}% in that direction; "
         "and it beats the 95th percentile of the placebo (random same-sector B, never linked to A).", "",
         _table(["Role", "Window", "Exploration pass", "Direction", "Holdout mean", "t", "p one-sided",
                 "Replicates", f"≥ {config.FLOOR * 100:.1f}%", "Placebo 95th pct", "Beats placebo", "Result"],
                chain), "",
         "## H1 — same-day ripple, holdout", "",
         _table(["Role", "Exploration", "Holdout mean [0,0]", "t", "p one-sided", "Result"],
                [[r, "candidate" if hold["H1"][r]["pass_exploration"] else "fail", _pct(hold["H1"][r]["mean"]),
                  _t(hold["H1"][r]["t"]), _p(hold["H1"][r]["p_one"]),
                  "**PASS**" if hold["H1"][r]["pass"] else "fail"] for r in config.ROLES]), "",
         "## H3 — pre-trend in both periods", "",
         _table(["Role", "t exploration", "t holdout", "OK"],
                [[r, _t(expl["H3"][r]["t_pre"]), _t(hold["H3"][r]["t_pre"]), _yn(hold["H3"][r]["ok"])]
                 for r in config.ROLES]), "",
         "## H4 — direction by role, holdout (descriptive)", "",
         _table(["Role", "Window", "Mean", "t (date)", "t (week)", "95% CI (bootstrap)", "Events", "Dates"],
                _h4_rows(hold["table"])), "",
         "## Robustness (spec §8 — reported, never changes the verdict)", "",
         f"### R6: holdout restricted to {config.HOLDOUT_LATE_START[:4]} onward", "",
         _table(["Role", "[+1,+5]", "[+1,+20]", "[+1,+60]", "Events [+1,+20]"],
                [[r, *[_mt(hold["robustness"]["r6_holdout_2024_on"][r][w]) for w in config.SLOW_WINDOWS],
                  hold["robustness"]["r6_holdout_2024_on"][r]["w20"]["n"]] for r in config.ROLES]), "",
         "### R1–R3: model and shock-rule variants in the holdout (✓ = replicates and clears the floor; no "
         "placebo for variants)", "",
         _table(["Check", "Role", "[+1,+5]", "[+1,+20]", "[+1,+60]"], _variant_rows(variants, "pass")), "",
         "## Figures", "", "Average cumulative abnormal return paths, day −5 to +60 (holdout events).", "",
         f"![CAR paths]({figure})", ""]
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
    variants = {label: _load(store / f"results_{period}{config.variant_suffix(v, r)}.json")
                for v, r, label in config.ROBUSTNESS_RUNS}
    variants = {k: x for k, x in variants.items() if x is not None}
    paths = io.read_frame(f"paths_{period}", measure.PATH_SCHEMA)
    fig_rel = f"figures/paths-{period}.png"
    plot_paths(paths, out / fig_rel, f"Domino study — average CAR paths ({period})")
    if period == "exploration":
        text = render_exploration(expl, power, variants, fig_rel, _data_notes(store, expl["counts"]))
    else:
        hold = _load(store / "results_holdout.json")
        if hold is None:
            raise FileNotFoundError("results_holdout.json missing: run `measure --period holdout` first")
        text = render_holdout(expl, hold, power, variants, fig_rel, _data_notes(store, hold["counts"]))
    doc = out / f"readout-{period}.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(text, encoding="utf-8")
    return {"readout": str(doc), "figure": str(out / fig_rel), "variants": sorted(variants)}
