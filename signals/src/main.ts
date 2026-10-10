import "./styles.css";
import { getReliabilitySummary, type ReliabilitySummary } from "./api";
import { renderDashboard, type RelModel, type RelMetric } from "./ui/scorecard";

let sc: ReliabilitySummary | null = null;
let model: RelModel = "daily";
let metric: RelMetric = "net";   // default to the retail bar
let clean = false;

const root = document.getElementById("app") as HTMLElement;

function render(): void {
  if (sc) renderDashboard(root, sc, { model, metric, clean });
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
  if (b.dataset.model) model = b.dataset.model as RelModel;
  else if (b.dataset.metric) metric = b.dataset.metric as RelMetric;
  else if (b.dataset.clean) clean = b.dataset.clean === "1";
  render();
});

load();
