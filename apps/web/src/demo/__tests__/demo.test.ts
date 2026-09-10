import { describe, expect, it } from "vitest";
import {
  demoAlerts,
  demoCompare,
  demoSeries,
  demoSymbols,
  demoTicks,
  demoVol,
  demoZHist,
  isDemo,
  tickHz,
} from "../index";

/**
 * This module backs the PUBLIC link — the one a reviewer clicks with nothing
 * running. It has no server to fall back on, so a defect here is not a degraded
 * dashboard, it is the only dashboard, broken, with nobody watching.
 */

const SYM = "EURUSD";

describe("the dataset", () => {
  it("covers every pair the app lists", () => {
    expect(demoSymbols()).toEqual(["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"]);
  });

  it("carries every timeframe the selector offers", () => {
    // A button with no data behind it renders an empty chart and no error.
    for (const tf of ["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"]) {
      expect(demoSeries(SYM, tf).length, tf).toBeGreaterThan(100);
    }
  });

  it("streams ticks at a stated rate", () => {
    expect(tickHz()).toBeGreaterThan(0);
    expect(demoTicks(SYM).length).toBeGreaterThan(100);
  });

  it("is off unless the build asked for it", () => {
    // A normal build must not divert to baked data.
    expect(isDemo()).toBe(false);
  });

  it("returns empty rather than throwing for an unknown pair", () => {
    expect(demoSeries("XXXYYY", "1h")).toEqual([]);
    expect(demoVol("XXXYYY")).toBeNull();
    expect(demoCompare("XXXYYY")).toBeNull();
    expect(demoTicks("XXXYYY")).toEqual([]);
    expect(demoZHist("XXXYYY")).toEqual([]);
  });
});

describe("rebasing onto the viewer's clock", () => {
  it("lands the series at roughly now, not at the day it was built", () => {
    // The file is anchored to a fixed instant so rebuilds produce a readable
    // diff. Without the shift a visitor would open a chart that stops months in
    // the past, which reads as an abandoned project.
    const last = demoSeries(SYM, "1m").at(-1)!;
    const ageMinutes = (Date.now() / 1000 - last.t) / 60;
    expect(ageMinutes).toBeGreaterThan(-1);
    expect(ageMinutes).toBeLessThan(5);
  });

  it("shifts the z-history by the same amount as the bars", () => {
    // Drift between the two would draw the z-line against the wrong minutes.
    const bar = demoSeries(SYM, "1m").at(-1)!;
    const z = demoZHist(SYM).at(-1)!;
    expect(Math.abs(bar.t - z.t)).toBeLessThan(24 * 3600);
  });

  it("shifts alert timestamps too", () => {
    const alerts = demoAlerts(SYM);
    expect(alerts.length).toBeGreaterThan(0);
    for (const a of alerts) {
      const age = Date.now() - Date.parse(a.ts);
      expect(age).toBeGreaterThan(0); // in the past
      expect(age).toBeLessThan(30 * 24 * 3600 * 1000);
    }
  });
});

describe("series identity", () => {
  it("returns the same array reference every call", () => {
    /**
     * Load-bearing, not an optimisation. `useChartBars` calls this during
     * render, and PriceChart's snapshot effect is keyed on `[bars]` — a fresh
     * array each time would rebuild the entire series on every render forever,
     * pinning a core on a page nobody is maintaining.
     */
    expect(demoSeries(SYM, "1h")).toBe(demoSeries(SYM, "1h"));
  });

  it("keeps timeframes and symbols separate", () => {
    expect(demoSeries(SYM, "1h")).not.toBe(demoSeries(SYM, "4h"));
    expect(demoSeries(SYM, "1h")).not.toBe(demoSeries("GBPUSD", "1h"));
  });
});

describe("what the panel reads", () => {
  it("mirrors the API's compare response", () => {
    // Shape parity is why VolatilityPanel needs no demo-specific rendering.
    const c = demoCompare(SYM)!;
    expect(Object.keys(c.estimators).sort()).toEqual([
      "close_to_close",
      "garman_klass",
      "parkinson",
      "rogers_satchell",
      "yang_zhang",
    ]);
    expect(c.bars_used).toBeGreaterThan(0);
    expect(c.window_s).toBe(c.bars_used * 60);
    expect(c.annualization_basis).toMatch(/252/);
  });

  it("reports volatilities in a plausible range", () => {
    // Computed by the real estimators at build time. A units error — per-bar
    // served as annualised — would show up here as a number near zero.
    const c = demoCompare(SYM)!;
    for (const [name, v] of Object.entries(c.estimators)) {
      expect(v, name).toBeGreaterThan(0.01); // > 1% annualised
      expect(v, name).toBeLessThan(1.0); // < 100%
    }
  });

  it("publishes the thresholds the detector actually used", () => {
    // The band the pane draws must be the band the z-scores were judged
    // against, or the picture contradicts itself.
    const v = demoVol(SYM)!;
    expect(v.enterZ).toBeGreaterThan(v.exitZ!);
    expect(v.warm).toBe(true);
    expect(v.sigmaAnn).toBeGreaterThan(0);
    expect(v.sigma).toBeLessThan(v.sigmaAnn); // per-bar below annualised
  });
});

describe("the session is worth showing", () => {
  it("contains a regime transition", () => {
    // A demo where the detector never fires hides the one feature the project
    // is named after. The dataset builder injects volatility clustering for
    // exactly this reason.
    expect(demoAlerts(SYM).length).toBeGreaterThan(0);
  });

  it("crosses the escalation threshold", () => {
    const v = demoVol(SYM)!;
    const peak = Math.max(...demoZHist(SYM).map((p) => p.z));
    expect(peak).toBeGreaterThan(v.enterZ!);
  });

  it("shows both regimes in the z-history", () => {
    expect(new Set(demoZHist(SYM).map((p) => p.r))).toEqual(new Set(["normal", "stressed"]));
  });

  it("ends calm, so the chart does not open mid-crisis", () => {
    expect(demoZHist(SYM).at(-1)!.r).toBe("normal");
  });
});
