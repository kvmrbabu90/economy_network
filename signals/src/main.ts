import "./styles.css";
import { getReliabilitySummary, type ReliabilitySummary } from "./api";
import { renderDashboard } from "./ui/scorecard";
import { reduceToggle, type RelState } from "./ui/state";

let sc: ReliabilitySummary | null = null;
let state: RelState = { model: "daily", metric: "net", clean: false };   // net = the retail bar

const root = document.getElementById("app") as HTMLElement;

function render(): void {
  if (sc) renderDashboard(root, sc, state);
}

async function load(): Promise<void> {
  try {
    sc = await getReliabilitySummary();
    render();
  } catch (e) {
    root.innerHTML = `<div class="rel-card"><p class="rel-sub">Dashboard unavailable — is the API running on :8101?<br><code>${e}</code></p></div>`;
  }
}

// Delegated toggles (model / metric / clean) re-render from the cached scorecard — no refetch.
root.addEventListener("click", (ev) => {
  const b = (ev.target as HTMLElement).closest<HTMLButtonElement>(".rel-btn");
  if (!b || !sc) return;
  state = reduceToggle(state, b.dataset);
  render();
});

load();
