// /health-backed status pill. Green = ok + invariant intact. Yellow = ok but
// counts smell off. Red = unreachable or invariant violated.

import { getHealth } from "../api";

export function startStatusPolling(): void {
  poll();
  setInterval(poll, 15_000); // mild background poll; nothing aggressive
}

async function poll(): Promise<void> {
  const pill = document.getElementById("status-pill");
  const text = document.getElementById("status-text");
  if (!pill || !text) return;
  try {
    const h = await getHealth();
    if (h.status !== "ok" || h.customer_of_rows_in_db !== 0) {
      pill.classList.remove("ok");
      pill.classList.add("error");
      text.textContent = `INVARIANT VIOLATED (${h.customer_of_rows_in_db} rows)`;
      return;
    }
    pill.classList.remove("warn", "error");
    pill.classList.add("ok");
    text.textContent = `${h.node_count} nodes · ${h.core_edge_count} core + ${h.audit_edge_count} audit`;
  } catch (err) {
    pill.classList.remove("ok", "warn");
    pill.classList.add("error");
    text.textContent = "API unreachable";
  }
}
