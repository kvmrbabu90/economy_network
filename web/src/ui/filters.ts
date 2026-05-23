// Edge-type filter chips + provisional toggle. Emits a settings object that
// main.ts uses to assemble API query params.

import type { EdgeType } from "../api";

export interface FilterState {
  types: EdgeType[];
  includeProvisional: boolean;
}

export type FilterListener = (state: FilterState) => void;

export function wireFilters(onChange: FilterListener): FilterState {
  const chips = Array.from(
    document.querySelectorAll<HTMLInputElement>('.chip input[data-edge-type]'),
  );
  const provisional = document.getElementById("toggle-provisional") as HTMLInputElement | null;

  function readState(): FilterState {
    const types: EdgeType[] = [];
    for (const c of chips) {
      if (c.checked) types.push(c.dataset.edgeType as EdgeType);
    }
    return {
      types,
      includeProvisional: provisional?.checked ?? false,
    };
  }

  const fire = () => onChange(readState());
  chips.forEach((c) => c.addEventListener("change", fire));
  provisional?.addEventListener("change", fire);

  return readState();
}
