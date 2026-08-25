import { vi } from "vitest";

/**
 * A stand-in for Lightweight Charts.
 *
 * The library is canvas-only and jsdom has no canvas, so every test that renders
 * a chart either mocks this or skips the component entirely — which is how
 * `PriceChart` reached 0% coverage while containing the live-bar merge logic,
 * the marker ordering rule and the regime shading. Recording the calls lets the
 * *logic* be tested here and leaves the *pixels* to the browser suite.
 */

export interface SeriesSpy {
  setData: ReturnType<typeof vi.fn>;
  update: ReturnType<typeof vi.fn>;
  setMarkers: ReturnType<typeof vi.fn>;
  applyOptions: ReturnType<typeof vi.fn>;
  priceScale: () => { applyOptions: ReturnType<typeof vi.fn> };
}

export interface ChartSpy {
  candles: SeriesSpy;
  histograms: SeriesSpy[];
  removed: boolean;
}

const series = (): SeriesSpy => ({
  setData: vi.fn(),
  update: vi.fn(),
  setMarkers: vi.fn(),
  applyOptions: vi.fn(),
  priceScale: () => ({ applyOptions: vi.fn() }),
});

/** The most recent chart created. Charts are per-mount, so this is the one. */
export let chart: ChartSpy | null = null;

export function createChart(): unknown {
  const spy: ChartSpy = { candles: series(), histograms: [], removed: false };
  chart = spy;
  return {
    addCandlestickSeries: () => spy.candles,
    addHistogramSeries: () => {
      const h = series();
      spy.histograms.push(h);
      return h;
    },
    applyOptions: vi.fn(),
    timeScale: () => ({ fitContent: vi.fn() }),
    remove: () => {
      spy.removed = true;
    },
  };
}

export function reset(): void {
  chart = null;
}
