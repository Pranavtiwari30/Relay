import { describe, expect, it } from "vitest";
import {
  DASH,
  formatCount,
  formatDuration,
  formatPercent,
  formatRelative,
  formatSpan,
  humanize,
  parseJson,
  shortId,
} from "./format";

describe("formatPercent", () => {
  it("shows a dash, not 0% or 100%, when there are no outcomes", () => {
    expect(formatPercent(null)).toBe(DASH);
    expect(formatPercent(undefined)).toBe(DASH);
  });
  it("formats ratios", () => {
    expect(formatPercent(0)).toBe("0%");
    expect(formatPercent(1)).toBe("100%");
    expect(formatPercent(0.7)).toBe("70.0%");
    expect(formatPercent(0.666)).toBe("66.6%");
  });
  it("never rounds an imperfect rate up to 100%", () => {
    expect(formatPercent(0.9996)).toBe("99.9%");
    expect(formatPercent(0.99999)).toBe("99.9%");
  });
});

describe("durations and spans", () => {
  it("distinguishes missing from zero", () => {
    expect(formatDuration(null)).toBe(DASH);
    expect(formatDuration(0)).toBe("0 ms");
    expect(formatSpan(null)).toBe(DASH);
    expect(formatSpan(0)).toBe("0s");
  });
  it("picks a sensible unit", () => {
    expect(formatDuration(42)).toBe("42 ms");
    expect(formatDuration(1500)).toBe("1.50 s");
    expect(formatDuration(12_340)).toBe("12.3 s");
    expect(formatSpan(59)).toBe("59s");
    expect(formatSpan(60)).toBe("1m");
    expect(formatSpan(3725)).toBe("1h 2m");
    expect(formatSpan(7200)).toBe("2h");
    expect(formatSpan(90_000)).toBe("1d 1h");
  });
  it("formats relative times in both directions", () => {
    const now = new Date("2026-03-01T12:00:00Z");
    expect(formatRelative("2026-03-01T11:59:58Z", now)).toBe("just now");
    expect(formatRelative("2026-03-01T11:58:00Z", now)).toBe("2m ago");
    expect(formatRelative("2026-03-01T12:00:30Z", now)).toBe("in 30s");
    expect(formatRelative(null, now)).toBe(DASH);
    expect(formatRelative("not a date", now)).toBe(DASH);
  });
});

describe("labels", () => {
  it("humanizes codes and keeps acronyms upper-case", () => {
    expect(humanize("http_5xx")).toBe("HTTP 5xx");
    expect(humanize("DEAD_LETTERED")).toBe("Dead lettered");
    expect(humanize("destination_unavailable")).toBe("Destination unavailable");
    expect(humanize("integration.pause")).toBe("Integration pause");
    expect(humanize(null)).toBe(DASH);
  });
  it("compacts large counts only", () => {
    expect(formatCount(0)).toBe("0");
    expect(formatCount(null)).toBe(DASH);
    expect(formatCount(1284)).toBe((1284).toLocaleString());
    expect(formatCount(12_900)).toMatch(/12\.9\s?K/i);
  });
  it("shortens long ids and parses JSON safely", () => {
    expect(shortId("6dc34399-5b2a-4445-9b83-6f3e5e66df1a")).toBe("6dc34399…df1a");
    expect(shortId("short")).toBe("short");
    expect(parseJson('{"a":1}')).toEqual({ ok: true, value: { a: 1 } });
    expect(parseJson("{nope").ok).toBe(false);
  });
});
