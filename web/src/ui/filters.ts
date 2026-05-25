// Edge-type filter chips + provisional toggle + market filter.
// Emits a settings object that main.ts uses to assemble API query params.

import type { EdgeType } from "../api";

// Which ISO-3166-1 alpha-2 country codes belong to each market group key.
// "OTHER" is handled as a catch-all for codes not listed in any group.
export const MARKET_COUNTRIES: Record<string, string[]> = {
  US:    ["US"],
  EU:    ["DE", "FR", "NL", "IT", "ES", "SE", "DK", "FI", "AT", "PT",
          "IE", "BE", "PL", "NO", "CH"],
  UK:    ["GB"],
  JP:    ["JP"],
  CN:    ["CN"],
  IN:    ["IN"],
  KR:    ["KR"],
  TW:    ["TW"],
  SEA:   ["SG", "MY", "TH", "ID"],
  LATAM: ["BR", "MX", "CO", "PE", "CL", "AR", "VE", "EC", "BO", "PY", "UY"],
  MEA:   ["SA", "AE", "EG", "ZA", "NG", "MA"],
  OTHER: [], // catch-all — any country code not in the groups above
};

// All known country codes that are explicitly assigned to a group
const KNOWN_COUNTRY_CODES = new Set(
  Object.entries(MARKET_COUNTRIES)
    .filter(([k]) => k !== "OTHER")
    .flatMap(([, v]) => v),
);

/** Returns true if the given ISO country code belongs to the selected market key. */
export function countryInMarket(country: string, market: string): boolean {
  if (market === "OTHER") return !KNOWN_COUNTRY_CODES.has(country);
  return (MARKET_COUNTRIES[market] ?? []).includes(country);
}

/** Returns true if the ISO country code belongs to ANY of the selected market keys. */
export function countryInMarkets(country: string, markets: string[]): boolean {
  return markets.some((m) => countryInMarket(country, m));
}

export interface FilterState {
  types: EdgeType[];
  includeProvisional: boolean;
  includeInferred: boolean;
  /** null = all markets; string[] = only these market keys */
  markets: string[] | null;
}

export type FilterListener = (state: FilterState) => void;

const ALL_MARKET_KEYS = Object.keys(MARKET_COUNTRIES);

export function wireFilters(onChange: FilterListener): FilterState {
  const chips = Array.from(
    document.querySelectorAll<HTMLInputElement>('.chip input[data-edge-type]'),
  );
  const provisional = document.getElementById("toggle-provisional") as HTMLInputElement | null;
  const inferred    = document.getElementById("toggle-inferred")    as HTMLInputElement | null;
  const marketAll   = document.getElementById("market-all")         as HTMLInputElement | null;
  const marketCbs   = Array.from(
    document.querySelectorAll<HTMLInputElement>(".market-cb"),
  );

  // ---- "All markets" toggle logic ----
  // When "All" is checked, check every individual box and disable them.
  // When any individual box changes, auto-update "All" if all are checked.
  function syncAllCheckbox() {
    if (!marketAll) return;
    const allChecked = marketCbs.every((c) => c.checked);
    marketAll.checked = allChecked;
    marketAll.indeterminate = !allChecked && marketCbs.some((c) => c.checked);
  }

  marketAll?.addEventListener("change", () => {
    const on = marketAll.checked;
    marketCbs.forEach((c) => { c.checked = on; });
    marketAll.indeterminate = false;
    onChange(readState());
  });

  marketCbs.forEach((c) => {
    c.addEventListener("change", () => {
      syncAllCheckbox();
      onChange(readState());
    });
  });

  function readState(): FilterState {
    const types: EdgeType[] = [];
    for (const c of chips) {
      if (c.checked) types.push(c.dataset.edgeType as EdgeType);
    }
    // Determine market filter: null if all selected, otherwise list of keys
    const selectedMarkets = marketCbs
      .filter((c) => c.checked)
      .map((c) => c.dataset.market as string);
    const markets =
      selectedMarkets.length === ALL_MARKET_KEYS.length ? null : selectedMarkets;

    return {
      types,
      includeProvisional: provisional?.checked ?? false,
      includeInferred:    inferred?.checked    ?? false,
      markets,
    };
  }

  const fire = () => onChange(readState());
  chips.forEach((c) => c.addEventListener("change", fire));
  provisional?.addEventListener("change", fire);
  inferred?.addEventListener("change", fire);

  return readState();
}
