import { describe, expect, it } from "vitest";
import { reduceToggle, type RelState } from "../ui/state";

const base: RelState = { model: "daily", metric: "net", clean: false };

describe("reduceToggle", () => {
  it("switches the model and leaves metric/clean intact", () => {
    expect(reduceToggle(base, { model: "dynamic" })).toEqual({ model: "dynamic", metric: "net", clean: false });
    expect(reduceToggle(base, { model: "hourly" })).toEqual({ model: "hourly", metric: "net", clean: false });
  });

  it("switches the metric and the clean flag independently", () => {
    expect(reduceToggle(base, { metric: "diracc" }).metric).toBe("diracc");
    expect(reduceToggle(base, { clean: "1" }).clean).toBe(true);
    expect(reduceToggle({ ...base, clean: true }, { clean: "0" }).clean).toBe(false);
  });

  it("preserves a selected metric across a model round-trip (dynamic hides it, daily restores it)", () => {
    // diracc stays in state through dynamic; renderDashboard is what hides/coerces it per-model, so
    // the state machine must not silently drop it — this locks that contract.
    let s = reduceToggle(base, { metric: "diracc" });
    s = reduceToggle(s, { model: "dynamic" });
    expect(s).toEqual({ model: "dynamic", metric: "diracc", clean: false });
    s = reduceToggle(s, { model: "daily" });
    expect(s.metric).toBe("diracc");
  });

  it("returns the state unchanged for a button carrying none of the known data-* keys", () => {
    expect(reduceToggle(base, {})).toEqual(base);
  });

  it("does not mutate the input state object", () => {
    const snapshot = { ...base };
    reduceToggle(base, { model: "hourly" });
    expect(base).toEqual(snapshot);
  });
});
