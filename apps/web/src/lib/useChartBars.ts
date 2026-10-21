/**
 * Where the chart's candles come from, per timeframe.
 *
 * Every intraday timeframe is folded in the browser from one series of
 * one-minute bars the store keeps current: the snapshot, then one history
 * request for the day of minutes the server holds, then each sealed minute as
 * the vol frame carries it. So switching between 1m and 30m asks nothing of the
 * network and shows the same minutes at another resolution immediately.
 *
 * That was not possible when only 1m was live. Coarser views were folded
 * server-side and refetched every 60s, so their last candle stood still for up
 * to a minute and then jumped, and switching timeframe meant a round trip. The
 * cost the old split avoided - shipping minutes to fold them here - is bounded
 * by the server's own hot cache: a day of minutes, once per pair viewed, ~130 KB.
 *
 * 1D is the exception: years of days come from the provider's daily series,
 * and only today is reconciled against the minutes we observed.
 *
 * The candle still forming is not in `bars`. It changes on every tick, so the
 * chart paints it from the store directly rather than re-rendering for it.
 */

import { useEffect, useMemo, useState } from "react";
import { MINUTE_S, TF_SECONDS, dailySeries, foldMinutes, type Coverage } from "./candles";
import { apiBase } from "./endpoints";
import { useDaily, useMinuteGen, useMinutes, useStore } from "./stream/hooks";
import type { MarketStore } from "./stream/store";
import type { Bar } from "./stream/types";
import type { Timeframe } from "../components/TimeframeBar";

/** The server's hot cache: a day of one-minute bars. */
const MINUTES = 1_440;
const DAYS = 300;
/** The provider's daily series is refreshed hourly server-side. */
const DAILY_FRESH_MS = 10 * 60_000;
const RETRY_MS = 60_000;

export interface ChartBars {
  readonly bars: readonly Bar[];
  readonly loading: boolean;
  /** Set when a request failed. The chart says so rather than showing an empty
   *  pane, which is indistinguishable from "this market has no history". */
  readonly error: boolean;
  /** 1D only: how much of today's trading the newest candle is built from. */
  readonly coverage: Coverage | null;
}

interface Fetching {
  readonly loading: boolean;
  readonly error: boolean;
}

const IDLE: Fetching = { loading: false, error: false };

async function getBars(url: string, signal: AbortSignal): Promise<Bar[]> {
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error(String(res.status));
  const body = (await res.json()) as { bars?: Bar[] };
  return body.bars ?? [];
}

/**
 * The day of minutes behind the snapshot's four hours, fetched once per pair
 * and again only when a reconnect left a hole the snapshot could not fill.
 */
function useMinuteHistory(store: MarketStore, symbol: string): Fetching {
  const gen = useMinuteGen(symbol);
  const [state, setState] = useState<Fetching>(IDLE);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    setState(IDLE);
    if (!store.needsHistory(symbol)) return;

    // A response for the pair the user has already left must not land on the
    // one they are now looking at; the abort makes that impossible to miss.
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    setState({ loading: true, error: false });

    getBars(`${apiBase()}/api/bars/${symbol}?interval=1m&limit=${MINUTES}`, controller.signal)
      .then((bars) => {
        if (!live) return;
        store.mergeMinutes(symbol, bars, "history");
        store.historyLoaded(symbol, gen);
        setState(IDLE);
      })
      .catch(() => {
        // AbortError lands here too; `live` is already false in that case, so
        // an aborted request cannot flip the error flag on the new pair.
        if (!live) return;
        setState({ loading: false, error: true });
        timer = setTimeout(() => setRetry((n) => n + 1), RETRY_MS);
      });

    return () => {
      live = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, [store, symbol, gen, retry]);

  return state;
}

/** The provider's daily series, while the 1D view is open, kept for revisits. */
function useDailyHistory(store: MarketStore, symbol: string, active: boolean): Fetching {
  const [state, setState] = useState<Fetching>(IDLE);

  useEffect(() => {
    setState(IDLE);
    if (!active) return;

    let live = true;
    let inFlight: AbortController | null = null;
    const load = () => {
      if (inFlight || Date.now() - store.dailyAt(symbol) < DAILY_FRESH_MS) return;
      const controller = (inFlight = new AbortController());
      setState((s) => ({ ...s, loading: true }));
      getBars(`${apiBase()}/api/bars/${symbol}?interval=1d&limit=${DAYS}`, controller.signal)
        .then((bars) => {
          if (!live) return;
          store.setDaily(symbol, bars);
          setState(IDLE);
        })
        .catch(() => {
          if (live) setState({ loading: false, error: true });
        })
        .finally(() => {
          if (inFlight === controller) inFlight = null;
        });
    };

    load();
    // Checked every minute, fetched when the copy is ten minutes old: today's
    // candle is live from the minutes, so only past days wait on this.
    const timer = setInterval(load, RETRY_MS);
    return () => {
      live = false;
      inFlight?.abort();
      clearInterval(timer);
    };
  }, [store, symbol, active]);

  return state;
}

const minuteNow = () => Math.floor(Date.now() / 60_000) * MINUTE_S;

/** Wall-clock minute, ticking only while something needs it. */
function useMinuteClock(active: boolean): number {
  const [now, setNow] = useState(minuteNow);
  useEffect(() => {
    if (!active) return;
    setNow(minuteNow());
    const timer = setInterval(() => setNow(minuteNow()), 15_000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

export function useChartBars(symbol: string, tf: Timeframe): ChartBars {
  const store = useStore();
  const minutes = useMinutes(symbol);
  const daily = useDaily(symbol);
  const history = useMinuteHistory(store, symbol);
  const days = useDailyHistory(store, symbol, tf === "1d");
  const now = useMinuteClock(tf === "1d");

  const { bars, coverage } = useMemo(
    () =>
      tf === "1d"
        ? dailySeries(daily, minutes, now, store.forming(symbol))
        : { bars: foldMinutes(minutes, TF_SECONDS[tf]), coverage: null },
    [store, symbol, tf, minutes, daily, now],
  );

  const fetching = tf === "1d" ? days : history;
  return {
    bars,
    loading: bars.length === 0 && (fetching.loading || history.loading),
    error: fetching.error,
    coverage,
  };
}
