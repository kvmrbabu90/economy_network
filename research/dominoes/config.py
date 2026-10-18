"""Frozen constants for the Stage-1 domino study.

Every value comes from the pre-registration (docs/superpowers/specs/2026-10-18-domino-study-design.md,
§3-§6 plus the §13 operational definitions). This is the single source of truth; nothing here may be
tuned after outcome data has been seen.
"""
from __future__ import annotations

# --- data and periods (§3) ---
PRICE_START = "2013-09-01"
EXPLORATION = ("2015-01-01", "2021-12-31")
HOLDOUT_START = "2022-01-01"
HOLDOUT_LATE_START = "2024-01-01"          # robustness 6
COVID_EXCLUDE = ("2020-02-15", "2020-06-30")  # robustness 4

MARKET_TICKER = "SPY"
SECTOR_TO_ETF = {
    "Communication Services": "XLC", "Consumer Discretionary": "XLY", "Consumer Staples": "XLP",
    "Energy": "XLE", "Financials": "XLF", "Health Care": "XLV", "Industrials": "XLI",
    "Information Technology": "XLK", "Materials": "XLB", "Real Estate": "XLRE", "Utilities": "XLU",
}
SECTOR_ETFS = tuple(sorted(set(SECTOR_TO_ETF.values())))

EXCLUDED_EXTRACTORS = frozenset({"wikidata", "inference:co-mention"})   # P1830 'owner of'; co-mention
STUDY_EDGE_TYPES = ("supplies", "competes_with")
ROLES = ("supplier", "customer", "competitor")

# --- definitions (§4, §13) ---
EST_WINDOW = (-270, -21)       # market-model estimation window, trading days relative to day 0
MIN_EST_DAYS = 200
SHOCK_SIGMA = 3.0
DEDUP_DAYS = 10                # §13.2: drop a shock if ANY earlier shock of the firm is within 10 days
DV_WINDOW = (-80, -21)         # dollar volume / abnormal-volume baseline
MIN_DV_DAYS = 20               # §13.5
DATA_ERROR_ABS = 0.50          # §13.3: |r| > 50% ...
DATA_ERROR_REVERT = 0.05       # ... and next day back within 5% of the pre-move level -> bad print

WINDOWS = {"pre": (-5, -1), "d0": (0, 0), "w5": (1, 5), "w20": (1, 20), "w60": (1, 60)}
SLOW_WINDOWS = ("w5", "w20", "w60")
PATH_DAYS = (-5, 60)           # CAR-path figures

K_PRIMARY = 5
K_VARIANTS = (3, 5, 10)        # §13.6, robustness 7
K_MAX = 10
MIN_CONTROLS = 2

# --- statistics and pass rules (§5, §6) ---
T_BAR = 3.0
FAMILY_ALPHA = 0.05
HOLDOUT_ALPHA = 0.05           # one-sided, in the exploration direction (§13.1)
PRETREND_T = 2.0               # H3
FLOOR = 0.005                  # 50 bp, holdout mean in the exploration direction (§13.1)
MDE_LIMIT = 0.015              # 1.5% over 20 days -> above this a role is "underpowered / inconclusive"
POWER_DELTAS = tuple(round(i * 0.0005, 4) for i in range(0, 61))   # 0 .. 3% in 5 bp steps
POWER_BOOT_N = 1000
POWER_TARGET = 0.80
PLACEBO_N = 500
BOOT_N = 5000
SEED = 20261018

HOLDOUT_MARKER = "holdout_unlocked.txt"

_SECTOR_ALIASES = {"Healthcare": "Health Care"}


def normalize_sector(sector: str | None) -> str | None:
    """Canonical GICS sector name ('Healthcare' -> 'Health Care')."""
    if sector is None:
        return None
    s = sector.strip()
    return _SECTOR_ALIASES.get(s, s)


MODEL_VARIANTS = ("market", "sector_etf", "ff6")       # primary, robustness 1, robustness 2
PRIMARY_RUN = ("market", "sigma3")
ROBUSTNESS_RUNS = (                                     # spec §8 checks 1-3: each re-runs shocks -> measure
    ("sector_etf", "sigma3", "R1 sector-ETF excess return"),
    ("ff6", "sigma3", "R2 Fama-French 5 factors + momentum"),
    ("market", "sigma4", "R3a 4σ shocks"),
    ("market", "top1pct", "R3b top 1% |AR| per firm"),
)
READOUT_PATH = "docs/research/domino-study/readout-exploration.md"   # what `unlock` requires in the commit


def variant_suffix(variant: str = "market", rule: str = "sigma3") -> str:
    """'' for the primary analysis (market model, 3-sigma shocks); a stable suffix for robustness runs."""
    return "" if (variant, rule) == ("market", "sigma3") else f"__{variant}_{rule}"


def period_of(date_iso: str) -> str | None:
    """'exploration' | 'holdout' | None for an ISO date string (YYYY-MM-DD)."""
    d = str(date_iso)[:10]
    if EXPLORATION[0] <= d <= EXPLORATION[1]:
        return "exploration"
    if d >= HOLDOUT_START:
        return "holdout"
    return None
