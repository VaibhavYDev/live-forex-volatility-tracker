/**
 * Where the chart's bars come from, per timeframe.
 *
 * 1m is the live view: the store already holds the WebSocket snapshot and the
 * in-progress candle, so it re-renders on every tick with no request at all.
 *
 * Everything coarser is folded server-side and fetched. That split is the whole
 * reason the wire stays small — a 1-week chart is folded from roughly four
 * years of minutes, and shipping those to the browser to aggregate there would
 * cost megabytes to draw 200 candles.
 *
 * Coarser views refresh on a timer rather than per tick. A 4-hour candle that
 * repainted 25 times a second would be measuring noise far below its own
 * resolution, and the request it costs is not free.
 */

import { useEffect, useState } from "react";
import { demoSeries, isDemo } from "../demo";
import { apiBase } from "./endpoints";
import { useBars } from "./stream/hooks";
import type { Bar } from "./stream/types";
import { LIVE_TF, type Timeframe } from "../components/TimeframeBar";

const REFRESH_MS = 60_000;
const LIMIT = 300;

export interface ChartBars {
  readonly bars: readonly Bar[];
  readonly loading: boolean;
  /** Set when a fetch failed. The chart says so rather than showing an empty
   *  pane, which is indistinguishable from "this market has no history". */
  readonly error: boolean;
}

export function useChartBars(symbol: string, tf: Timeframe): ChartBars {
  const live = useBars(symbol);
  // Demo builds carry every timeframe pre-folded by the same `fold()` the API
  // uses, so there is nothing to fetch and nothing to fail.
  const demo = isDemo();
  const [fetched, setFetched] = useState<readonly Bar[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (demo) return;
    // Drop the previous timeframe's bars on EVERY change, not just on the way
    // back to 1m. Mutation testing caught this: switching 1d -> 4h left the
    // daily series in state until the new response landed, so the chart drew
    // daily candles under a 4h label for the width of one round trip. On a slow
    // connection that is a second of confidently wrong data.
    setFetched([]);
    setError(false);
    if (tf === LIVE_TF) return;

    // Guards against a slow response for a timeframe the user has already
    // clicked away from overwriting the one they are now looking at.
    let live = true;
    const controller = new AbortController();

    const load = async () => {
      setLoading(true);
      try {
        const res = await fetch(
          `${apiBase()}/api/bars/${symbol}?interval=${tf}&limit=${LIMIT}`,
          { signal: controller.signal },
        );
        if (!res.ok) throw new Error(String(res.status));
        const body = (await res.json()) as { bars: Bar[] };
        if (!live) return;
        setFetched(body.bars ?? []);
        setError(false);
      } catch {
        // AbortError lands here too; `live` is already false in that case, so
        // an aborted request cannot flip the error flag on the new timeframe.
        if (live) setError(true);
      } finally {
        if (live) setLoading(false);
      }
    };

    void load();
    const timer = setInterval(load, REFRESH_MS);
    return () => {
      live = false;
      controller.abort();
      clearInterval(timer);
    };
  }, [symbol, tf, demo]);

  if (demo) {
    // 1m still comes from the store so the live tick keeps building its candle.
    return tf === LIVE_TF
      ? { bars: live, loading: false, error: false }
      : { bars: demoSeries(symbol, tf), loading: false, error: false };
  }
  return tf === LIVE_TF
    ? { bars: live, loading: false, error: false }
    : { bars: fetched, loading, error };
}
