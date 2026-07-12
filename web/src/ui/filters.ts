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
  // Asia-Pacific: SE Asia + Australia / New Zealand (Pacific-rim grouping)
  SEA:   ["SG", "MY", "TH", "ID", "AU", "NZ"],
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
  /** Hide all edges in both 2D and 3D — a pure-visual declutter toggle. */
  hideEdges: boolean;
  /** Impact-magnitude band [magMin, magMax] in [0,1]; dots whose combined-impact
   *  magnitude falls outside are hidden. Default [0, 1] filters nothing. */
  magMin: number;
  magMax: number;
}

export type FilterListener = (state: FilterState) => void;

const ALL_MARKET_KEYS = Object.keys(MARKET_COUNTRIES);

export function wireFilters(onChange: FilterListener): FilterState {
  const chips = Array.from(
    document.querySelectorAll<HTMLInputElement>('.chip input[data-edge-type]'),
  );
  const provisional = document.getElementById("toggle-provisional") as HTMLInputElement | null;
  const inferred    = document.getElementById("toggle-inferred")    as HTMLInputElement | null;
  const hideEdges   = document.getElementById("toggle-hide-edges")  as HTMLInputElement | null;
  const magMinEl    = document.getElementById("mag-min")            as HTMLInputElement | null;
  const magMaxEl    = document.getElementById("mag-max")            as HTMLInputElement | null;
  const magMinVal   = document.getElementById("mag-min-val");
  const magMaxVal   = document.getElementById("mag-max-val");
  const magReadout  = document.getElementById("mag-readout");
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

  // All edge types in display order — used as fallback when no filter chips
  // are present in the DOM (they were removed from the sidebar in the UI
  // cleanup; we always show all edge types unless a chip opts one out).
  const ALL_EDGE_TYPES: EdgeType[] = ["supplies", "customer_of", "competes_with", "regulated_by"];

  function readState(): FilterState {
    const types: EdgeType[] = [];
    for (const c of chips) {
      if (c.checked) types.push(c.dataset.edgeType as EdgeType);
    }
    // No chips in DOM → treat every type as checked.
    const effectiveTypes = chips.length === 0 ? ALL_EDGE_TYPES : types;
    // Determine market filter: null if all known markets are selected,
    // otherwise list of selected keys. Compare against ALL_MARKET_KEYS by
    // value (not just length) to be robust against stray DOM checkboxes.
    const selectedMarketSet = new Set(
      marketCbs.filter((c) => c.checked).map((c) => c.dataset.market as string),
    );
    const allSelected = ALL_MARKET_KEYS.every((k) => selectedMarketSet.has(k));
    const markets = allSelected ? null : Array.from(selectedMarketSet);

    return {
      types: effectiveTypes,
      includeProvisional: provisional?.checked ?? false,
      includeInferred:    inferred?.checked    ?? false,
      markets,
      hideEdges: hideEdges?.checked ?? false,
      magMin: magMinEl ? parseFloat(magMinEl.value) : 0,
      magMax: magMaxEl ? parseFloat(magMaxEl.value) : 1,
    };
  }

  const fire = () => onChange(readState());
  chips.forEach((c) => c.addEventListener("change", fire));
  provisional?.addEventListener("change", fire);
  inferred?.addEventListener("change", fire);
  hideEdges?.addEventListener("change", fire);

  // ---- Impact-magnitude band sliders ----
  // Keep min <= max, update the readout live on every drag tick, but DEBOUNCE the
  // actual re-render (the globe rebuild is heavy) so dragging stays smooth.
  function refreshMagLabels() {
    const lo = magMinEl ? parseFloat(magMinEl.value) : 0;
    const hi = magMaxEl ? parseFloat(magMaxEl.value) : 1;
    if (magMinVal) magMinVal.textContent = lo.toFixed(2);
    if (magMaxVal) magMaxVal.textContent = hi.toFixed(2);
    if (magReadout) magReadout.textContent =
      lo <= 0 && hi >= 1 ? "showing all dots" : `showing ${lo.toFixed(2)} – ${hi.toFixed(2)}`;
  }
  let magTimer: ReturnType<typeof setTimeout> | undefined;
  function afterMagChange() {
    refreshMagLabels();
    clearTimeout(magTimer);
    magTimer = setTimeout(() => onChange(readState()), 90);   // debounce the re-render
  }
  magMinEl?.addEventListener("input", () => {
    // dragging min past max pushes max up to meet it (thumbs never cross)
    if (magMaxEl && parseFloat(magMinEl.value) > parseFloat(magMaxEl.value)) magMaxEl.value = magMinEl.value;
    afterMagChange();
  });
  magMaxEl?.addEventListener("input", () => {
    if (magMinEl && parseFloat(magMaxEl.value) < parseFloat(magMinEl.value)) magMinEl.value = magMaxEl.value;
    afterMagChange();
  });
  refreshMagLabels();

  return readState();
}
