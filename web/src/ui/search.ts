// Top-bar search with debounced live dropdown. Selecting a result triggers
// the `onSelect` callback so main.ts can re-center the graph.

import { search, type SearchHit } from "../api";

export function wireSearch(onSelect: (hit: SearchHit) => void): void {
  const input = document.getElementById("search-input") as HTMLInputElement | null;
  const list = document.getElementById("search-results") as HTMLOListElement | null;
  if (!input || !list) return;

  let token = 0;
  let activeIndex = -1;
  let currentHits: SearchHit[] = [];

  const debounceMs = 140;
  let timer: number | undefined;

  function close() {
    list!.hidden = true;
    list!.replaceChildren();
    activeIndex = -1;
    currentHits = [];
  }

  function render(hits: SearchHit[]) {
    list!.replaceChildren();
    currentHits = hits;
    if (!hits.length) {
      list!.hidden = true;
      return;
    }
    hits.forEach((hit, i) => {
      const li = document.createElement("li");
      li.setAttribute("data-id", hit.id);
      const isSlug = hit.id.startsWith("slug:");
      const isReg = hit.id.startsWith("regulator:");
      const typeLabel = isReg ? "regulator" : isSlug ? "provisional" : "company";

      const name = document.createElement("span");
      name.className = "sr-name";
      name.textContent = hit.name;
      const t = document.createElement("span");
      t.className = "sr-type";
      t.textContent = typeLabel;
      const sc = document.createElement("span");
      sc.className = "sr-score";
      sc.textContent = String(hit.score);
      li.append(name, t, sc);

      li.addEventListener("mousedown", (ev) => {
        ev.preventDefault(); // avoid losing input focus before click handler
        onSelect(hit);
        close();
      });
      li.addEventListener("mouseenter", () => highlight(i));
      list!.appendChild(li);
    });
    list!.hidden = false;
  }

  function highlight(i: number) {
    const items = Array.from(list!.children) as HTMLElement[];
    items.forEach((el, j) => el.setAttribute("aria-selected", String(j === i)));
    activeIndex = i;
  }

  async function fire() {
    const q = input!.value.trim();
    if (!q) { close(); return; }
    const my = ++token;
    try {
      const resp = await search(q, 10);
      if (my !== token) return; // out-of-order
      render(resp.results);
      highlight(0);
    } catch {
      // The status pill will already say "API unreachable"; don't clutter the UI.
      close();
    }
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = window.setTimeout(fire, debounceMs);
  });
  input.addEventListener("focus", fire);
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      if (currentHits.length) highlight(Math.min(activeIndex + 1, currentHits.length - 1));
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      if (currentHits.length) highlight(Math.max(activeIndex - 1, 0));
    } else if (ev.key === "Enter") {
      if (activeIndex >= 0 && currentHits[activeIndex]) {
        ev.preventDefault();
        onSelect(currentHits[activeIndex]);
        close();
      }
    } else if (ev.key === "Escape") {
      close();
    }
  });
}
