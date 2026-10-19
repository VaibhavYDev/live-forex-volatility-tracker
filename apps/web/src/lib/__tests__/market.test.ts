import { describe, expect, it } from "vitest";
import parity from "../../../../../tests/fixtures/candle_parity.json";
import { isMarketOpen, openMinutes } from "../market";

/**
 * The browser's copy of the FX week. It decides how many minutes of today
 * could have traded, which decides whether the daily candle is called partial,
 * so it is held to the server's own answers at every boundary.
 */

const utc = (spec: string) => Date.parse(`${spec}Z`) / 1000;

describe("parity with fx_core.calendar", () => {
  const samples = (parity as unknown as { calendar: [number, boolean][] }).calendar;

  it.each(samples)("agrees at %i", (epoch, open) => {
    expect(isMarketOpen(epoch)).toBe(open);
  });
});

describe("the week", () => {
  it("closes Friday 17:00 New York: 21:00 UTC in summer, 22:00 in winter", () => {
    expect(isMarketOpen(utc("2026-10-16T20:59:00"))).toBe(true);
    expect(isMarketOpen(utc("2026-10-16T21:00:00"))).toBe(false);
    expect(isMarketOpen(utc("2026-11-06T21:59:00"))).toBe(true);
    expect(isMarketOpen(utc("2026-11-06T22:00:00"))).toBe(false);
  });

  it("reopens Sunday 17:00 New York, including the Sunday the clocks change", () => {
    expect(isMarketOpen(utc("2026-10-18T20:59:00"))).toBe(false);
    expect(isMarketOpen(utc("2026-10-18T21:00:00"))).toBe(true);
    expect(isMarketOpen(utc("2026-11-01T21:00:00"))).toBe(false);
    expect(isMarketOpen(utc("2026-11-01T22:00:00"))).toBe(true);
  });

  it("is shut all Saturday", () => {
    expect(isMarketOpen(utc("2026-10-17T00:00:00"))).toBe(false);
    expect(isMarketOpen(utc("2026-10-17T23:59:00"))).toBe(false);
  });
});

describe("open minutes", () => {
  const day = (spec: string) => {
    const from = utc(`${spec}T00:00:00`);
    return openMinutes(from, from + 86_400);
  };

  it("counts every minute of a weekday", () => {
    expect(day("2026-10-21")).toBe(1_440);
  });

  it("stops at the Friday close and starts at the Sunday open", () => {
    expect(day("2026-10-16")).toBe(21 * 60);
    expect(day("2026-10-17")).toBe(0);
    expect(day("2026-10-18")).toBe(3 * 60);
    expect(day("2026-11-01")).toBe(2 * 60);
  });

  it("counts minute starts across ranges that do not begin on the hour", () => {
    expect(openMinutes(utc("2026-10-21T10:07:00"), utc("2026-10-21T11:03:00"))).toBe(56);
    expect(openMinutes(utc("2026-10-16T20:30:00"), utc("2026-10-16T21:30:00"))).toBe(30);
    expect(openMinutes(utc("2026-10-21T10:00:00"), utc("2026-10-21T10:00:00"))).toBe(0);
  });
});
