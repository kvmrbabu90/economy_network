"""Rebuild the shared research context — the returns panel and the abnormal-return model for a variant —
from the research store, so every stage derives AR/sigma/CARs the same way."""
from __future__ import annotations

from research.dominoes import config, fetch_factors, fetch_prices, io, snapshot_links
from research.dominoes import returns as rt


def load(variant: str = "market") -> tuple[rt.Panel, rt.AbnormalModel]:
    if variant not in config.MODEL_VARIANTS:
        raise ValueError(f"unknown model variant {variant!r}")
    if not io.frame_path("prices_raw").exists():
        raise FileNotFoundError("prices_raw snapshot missing: dollar volume needs the unadjusted close "
                                "(run the `prices` stage)")
    panel = rt.build_panel(fetch_prices.load_prices(), raw=fetch_prices.load_prices_raw())
    if variant == "market":
        return panel, rt.AbnormalModel(panel, "market")
    if variant == "sector_etf":
        firms = io.read_frame("firms", snapshot_links.FIRM_SCHEMA)
        etf_of = {r.symbol: config.SECTOR_TO_ETF[r.sector] for r in firms.itertuples()
                  if r.sector in config.SECTOR_TO_ETF}
        return panel, rt.AbnormalModel(panel, "sector_etf", etf_of=etf_of)
    factors = io.read_frame("factors", fetch_factors.FACTOR_SCHEMA)
    return panel, rt.AbnormalModel(panel, "ff6", factors=factors)
