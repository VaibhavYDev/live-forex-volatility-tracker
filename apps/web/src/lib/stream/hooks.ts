import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useRef,
  useSyncExternalStore,
} from "react";
import type { MarketStore } from "./store";
import type { Bar, Conn, FeedStatus, Quote, Regime, Transition, Vol, ZPoint } from "./types";

export const StoreCtx = createContext<MarketStore | null>(null);

export function useStore(): MarketStore {
  const s = useContext(StoreCtx);
  if (!s) throw new Error("useStore outside <StoreProvider>");
  return s;
}

/**
 * Every hook below binds to one topic. Both callbacks are memoised on
 * [store, symbol] — an inline arrow would hand `useSyncExternalStore` a new
 * `subscribe` on every render, which tears down and re-adds the listener each
 * time and defeats the point of the granularity.
 */

export function useQuote(symbol: string): Quote | undefined {
  const s = useStore();
  return useSyncExternalStore(
    useCallback((cb) => s.subQuote(symbol, cb), [s, symbol]),
    useCallback(() => s.quote(symbol), [s, symbol]),
  );
}

export function useVol(symbol: string): Vol | undefined {
  const s = useStore();
  return useSyncExternalStore(
    useCallback((cb) => s.subVol(symbol, cb), [s, symbol]),
    useCallback(() => s.vol(symbol), [s, symbol]),
  );
}

export function useRegime(symbol: string): Regime {
  const s = useStore();
  return useSyncExternalStore(
    useCallback((cb) => s.subVol(symbol, cb), [s, symbol]),
    useCallback(() => s.regime(symbol), [s, symbol]),
  );
}

export function useAlerts(symbol: string): readonly Transition[] {
  const s = useStore();
  return useSyncExternalStore(
    useCallback((cb) => s.subAlerts(symbol, cb), [s, symbol]),
    useCallback(() => s.alerts(symbol), [s, symbol]),
  );
}

export function useBars(symbol: string): readonly Bar[] {
  const s = useStore();
  return useSyncExternalStore(
    useCallback((cb) => s.subBars(symbol, cb), [s, symbol]),
    useCallback(() => s.bars(symbol), [s, symbol]),
  );
}

export function useZHist(symbol: string): readonly ZPoint[] {
  const s = useStore();
  return useSyncExternalStore(
    useCallback((cb) => s.subZ(symbol, cb), [s, symbol]),
    useCallback(() => s.zhist(symbol), [s, symbol]),
  );
}

export function useFeed(): FeedStatus {
  const s = useStore();
  return useSyncExternalStore(s.subFeed, s.feed);
}

export function useConn(): Conn {
  const s = useStore();
  return useSyncExternalStore(s.subConn, s.conn);
}

/**
 * Imperative tick stream for canvas. Deliberately NOT a render subscription:
 * there is no snapshot to compare, only pixels to paint, and dressing that up as
 * `useSyncExternalStore` with a constant snapshot would be a lie about what the
 * hook does.
 *
 * The callback goes through a ref so an inline arrow at the call site does not
 * tear down and re-add the listener on every render. Making the caller memoise
 * is a footgun that only fails under load, which is the worst time to find it.
 */
export function useTicks(symbol: string, fn: (q: Quote) => void): void {
  const s = useStore();
  const latest = useRef(fn);
  useLayoutEffect(() => {
    latest.current = fn;
  });
  useEffect(() => s.onTick(symbol, (q) => latest.current(q)), [s, symbol]);
}

/** Every symbol's transitions as they commit. Same ref treatment as useTicks. */
export function useAlertFeed(fn: (t: Transition) => void): void {
  const s = useStore();
  const latest = useRef(fn);
  useLayoutEffect(() => {
    latest.current = fn;
  });
  useEffect(() => s.onAlert((t) => latest.current(t)), [s]);
}
