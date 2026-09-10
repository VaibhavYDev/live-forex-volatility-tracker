/**
 * Serverless demo mode.
 *
 * The published dashboard has to answer a link that anyone can click, at any
 * hour, with nothing running anywhere. GitHub Pages serves static files and
 * runs no Python, so there is no ingestor, no Redis and no API behind it.
 *
 * What there IS: a dataset baked by `scripts/build_demo_dataset.py`, which runs
 * the real `fx_core` modules — Welford, EWMA, the four range estimators and the
 * RegimeDetector — over a deterministic session and writes out what they
 * produced. This module feeds that into the SAME MarketStore the WebSocket
 * feeds, so every component below it is unchanged and unaware.
 *
 * The alternative was porting the numerical core to TypeScript so the browser
 * could compute it live. That would put a second implementation of the
 * statistics in the repo, and the one on the public URL would be the one nobody
 * tests. Baking the real engine's output is the more honest of the two.
 */

import raw from "./dataset.json";
import type { Bar, Transition, Vol, ZPoint } from "../lib/stream/types";

interface SymbolData {
  series: Record<string, Bar[]>;
  estimators: Record<string, number>;
  bars_in_window: number;
  zhist: ZPoint[];
  alerts: Transition[];
  enter_z: number;
  exit_z: number;
  regime: string;
  sigma_ann: number;
  ticks: { i: number; bid: number; ask: number; mid: number }[];
}

interface Dataset {
  anchor: number;
  tick_hz: number;
  symbols: string[];
  by_symbol: Record<string, SymbolData>;
}

const data = raw as unknown as Dataset;

/** Built with VITE_DEMO=1. A normal build is untouched by any of this. */
export const isDemo = (): boolean => import.meta.env.VITE_DEMO === "1";

export const demoSymbols = (): string[] => data.symbols;
export const tickHz = (): number => data.tick_hz;

/**
 * Seconds to add to every baked timestamp so the session lands on the viewer's
 * clock.
 *
 * The dataset is anchored to a FIXED instant rather than generated at build
 * time: anchoring to "now" would rewrite every timestamp in a 1.6 MB file on
 * each CI run, making the diff useless and the file uncacheable. Rebasing here
 * costs one addition per bar and means a visitor in any month sees a chart
 * ending at their own current minute rather than at the day it was built.
 *
 * Computed once at module load, not per call — recomputing would drift the
 * series against the tick loop over a long-lived tab.
 */
const SHIFT = Math.floor(Date.now() / 1000 / 60) * 60 - data.anchor;

const shiftBar = (b: Bar): Bar => ({ ...b, t: b.t + SHIFT });

/**
 * Memoised, and that is load-bearing rather than an optimisation.
 *
 * `useChartBars` calls this during render. Returning a freshly mapped array
 * each time would hand the chart a new `bars` reference on every render, and
 * PriceChart's snapshot effect is keyed on `[bars]` — so it would tear down and
 * rebuild the whole series continuously, pinning a core at 100% on a page
 * nobody is maintaining. Same reference in, no redraw.
 */
const seriesCache = new Map<string, readonly Bar[]>();

export function demoSeries(symbol: string, tf: string): readonly Bar[] {
  const key = `${symbol}:${tf}`;
  let hit = seriesCache.get(key);
  if (!hit) {
    hit = (data.by_symbol[symbol]?.series[tf] ?? []).map(shiftBar);
    seriesCache.set(key, hit);
  }
  return hit;
}

export function demoZHist(symbol: string): readonly ZPoint[] {
  return (data.by_symbol[symbol]?.zhist ?? []).map((p) => ({ ...p, t: p.t + SHIFT }));
}

export function demoAlerts(symbol: string): readonly Transition[] {
  return (data.by_symbol[symbol]?.alerts ?? []).map((a) => ({
    ...a,
    ts: new Date(Date.parse(a.ts) + SHIFT * 1000).toISOString(),
  }));
}

/** Mirrors the API's /compare response so VolatilityPanel needs no demo branch
 *  beyond choosing its source. */
export function demoCompare(symbol: string): {
  bars_used: number;
  window_s: number;
  annualization_basis: string;
  estimators: Record<string, number>;
} | null {
  const d = data.by_symbol[symbol];
  if (!d) return null;
  return {
    bars_used: d.bars_in_window,
    window_s: d.bars_in_window * 60,
    annualization_basis: "252 trading days x 24h = 362,880 one-minute bars/year",
    estimators: d.estimators,
  };
}

export function demoVol(symbol: string): Vol | null {
  const d = data.by_symbol[symbol];
  if (!d) return null;
  const last = d.zhist.at(-1);
  return {
    symbol,
    // Per-bar sigma, back out of the annualised figure the estimators produced.
    sigma: d.sigma_ann / Math.sqrt((252 * 24 * 3600) / 60),
    sigmaAnn: d.sigma_ann,
    z: last?.z ?? null,
    regime: (last?.r ?? "normal") as Vol["regime"],
    warm: true,
    ts: new Date().toISOString(),
    enterZ: d.enter_z,
    exitZ: d.exit_z,
  };
}

export function demoTicks(symbol: string): { bid: number; ask: number; mid: number }[] {
  return data.by_symbol[symbol]?.ticks ?? [];
}
