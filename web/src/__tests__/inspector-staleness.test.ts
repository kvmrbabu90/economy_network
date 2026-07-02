import { describe, expect, it } from "vitest";
import { isStale, combinedDirLabel } from "../ui/inspector";

describe("isStale", () => {
  const now = Date.parse("2026-07-01T12:00:00Z");

  it("fresh data (well under 2h) → not stale", () => {
    expect(isStale("2026-07-01T11:30:00Z", now)).toBe(false);
  });

  it("data older than 2h → stale", () => {
    expect(isStale("2026-07-01T09:30:00Z", now)).toBe(true);
  });

  it("exactly at the 2h boundary → not stale (strict >)", () => {
    expect(isStale("2026-07-01T10:00:00Z", now)).toBe(false);
  });

  it("null / undefined computedAt → stale (pipeline never ran)", () => {
    expect(isStale(null, now)).toBe(true);
    expect(isStale(undefined, now)).toBe(true);
  });

  it("unparseable timestamp → stale (fail safe)", () => {
    expect(isStale("not-a-date", now)).toBe(true);
  });

  it("respects a custom maxAgeMs", () => {
    // 30 min old, 15 min budget → stale
    expect(isStale("2026-07-01T11:30:00Z", now, 15 * 60 * 1000)).toBe(true);
    // 30 min old, 1 h budget → fresh
    expect(isStale("2026-07-01T11:30:00Z", now, 60 * 60 * 1000)).toBe(false);
  });
});

describe("combinedDirLabel", () => {
  it("mixed_signals with magnitude > 0 → MIXED", () => {
    expect(combinedDirLabel({ direction: "positive", magnitude: 0.6, mixed_signals: 1 })).toBe("MIXED");
    expect(combinedDirLabel({ direction: "negative", magnitude: 0.6, mixed_signals: true })).toBe("MIXED");
  });

  it("mixed_signals but magnitude 0 → never MIXED (aligns with the map, which doesn't tint no_effect)", () => {
    expect(combinedDirLabel({ direction: "no_effect", magnitude: 0, mixed_signals: 1 })).toBe("NO EFFECT");
    // Even a directional verdict at magnitude 0 must not show MIXED.
    expect(combinedDirLabel({ direction: "positive", magnitude: 0, mixed_signals: 1 })).toBe("POSITIVE");
  });

  it("no mixed_signals → plain direction label", () => {
    expect(combinedDirLabel({ direction: "positive", magnitude: 0.5, mixed_signals: 0 })).toBe("POSITIVE");
    expect(combinedDirLabel({ direction: "negative", magnitude: 0.5, mixed_signals: 0 })).toBe("NEGATIVE");
    expect(combinedDirLabel({ direction: "no_effect", magnitude: 0, mixed_signals: 0 })).toBe("NO EFFECT");
  });
});
