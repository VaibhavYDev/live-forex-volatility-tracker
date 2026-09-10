/**
 * Drives the store from the baked dataset instead of a WebSocket.
 *
 * Deliberately implements the same contract `useForexStream` does — seed the
 * snapshot, then stream quotes through `store.offer()` — so every component
 * below is identical to the live build. The demo is not a second dashboard; it
 * is the dashboard with a different source.
 *
 * THE FEED NEVER GOES STALE. `ts` is re-stamped on the same 5s cadence the
 * ingestor's heartbeat uses, because the banner's staleness rule is real and
 * would otherwise fire on a page that is working perfectly. A public link that
 * says "Stale" is worse than no link.
 */

import { useEffect, useState, type ReactNode } from "react";
import { StoreCtx } from "../lib/stream/hooks";
import { MarketStore } from "../lib/stream/store";
import type { Bar } from "../lib/stream/types";
import {
  demoAlerts,
  demoSeries,
  demoSymbols,
  demoTicks,
  demoVol,
  demoZHist,
  tickHz,
} from "./index";

const HEARTBEAT_MS = 5_000;

export function DemoProvider({ children }: { children: ReactNode }) {
  const [store] = useState(() => new MarketStore());

  useEffect(() => {
    const symbols = demoSymbols();

    // Snapshot first, exactly as the socket's `snapshot` frame does.
    const bars: Record<string, readonly Bar[]> = {};
    for (const s of symbols) {
      bars[s] = demoSeries(s, "1m");
      store.setZHist(s, demoZHist(s));
      const vol = demoVol(s);
      if (vol) {
        store.seedRegime(s, vol.regime);
        store.setVol(vol);
      }
      // Seeded, not pushed: these are historical, and toasting them would open
      // the page with a stack of pop-ups about events nobody was here for.
      for (const a of demoAlerts(s)) store.seedAlert(a);
    }
    store.setBars(bars);
    store.setConn("open", 0);

    const beat = () => store.setFeed({ state: "healthy", ts: new Date().toISOString() });
    beat();
    const heart = window.setInterval(beat, HEARTBEAT_MS);

    // One cursor per symbol into its own loop, so the five pairs do not move in
    // lockstep — synchronised prices are the tell that a page is a recording.
    const streams = symbols.map((s, i) => ({
      symbol: s,
      ticks: demoTicks(s),
      at: Math.floor((i * 137) % Math.max(demoTicks(s).length, 1)),
    }));

    const pump = window.setInterval(() => {
      const ts = new Date().toISOString();
      for (const st of streams) {
        if (st.ticks.length === 0) continue;
        const q = st.ticks[st.at]!;
        st.at = (st.at + 1) % st.ticks.length;
        store.offer({ symbol: st.symbol, bid: q.bid, ask: q.ask, mid: q.mid, ts });
      }
    }, 1000 / tickHz());

    return () => {
      window.clearInterval(heart);
      window.clearInterval(pump);
    };
  }, [store]);

  return <StoreCtx.Provider value={store}>{children}</StoreCtx.Provider>;
}
