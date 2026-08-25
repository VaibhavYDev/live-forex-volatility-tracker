import { useState, type ReactNode } from "react";
import { useForexStream } from "../../hooks/useForexStream";
import { StoreCtx } from "./hooks";
import { MarketStore } from "./store";

/**
 * One store per app, created once and never replaced.
 *
 * The lazy `useState` initialiser matters: `new MarketStore()` in the render
 * body would allocate a store on every render and throw away every subscription
 * with it. It also survives StrictMode's double mount, which is the point —
 * the socket may reconnect, the accumulated market state should not vanish.
 */
export function StreamProvider({
  url,
  symbols,
  children,
}: {
  url: string;
  symbols: string[];
  children: ReactNode;
}) {
  const [store] = useState(() => new MarketStore());
  useForexStream(store, url, symbols);
  return <StoreCtx.Provider value={store}>{children}</StoreCtx.Provider>;
}
