// Pure toggle state machine for the dashboard controls, split out of main.ts so it is testable
// without a DOM. Each .rel-btn carries exactly one of data-model / data-metric / data-clean; a
// click maps the old state + that button's dataset to the next state. No side effects.
import type { RelModel, RelMetric } from "./scorecard";

export interface RelState { model: RelModel; metric: RelMetric; clean: boolean }

export function reduceToggle(state: RelState, ds: DOMStringMap): RelState {
  if (ds.model) return { ...state, model: ds.model as RelModel };
  if (ds.metric) return { ...state, metric: ds.metric as RelMetric };
  if (ds.clean) return { ...state, clean: ds.clean === "1" };
  return state;
}
