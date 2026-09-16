/**
 * The wire → store adapter. This is the only place in the app that knows what a
 * server frame looks like.
 *
 * At 50 messages/second, one `setState` per message means 50 reconciliations per
 * second, a permanently busy main thread and a chart that stutters — the single
 * mistake that makes most live dashboards feel broken, and one that is invisible
 * until a real firehose is pointed at them. So nothing here touches React state.
 * Frames land in `MarketStore`, which decides what is a 60Hz pixel stream (ticks,
 * conflated into one rAF-scheduled flush) and what is structural change worth a
 * render (a regime transition, a feed going degraded, a socket dropping).
 *
 * rAF gives us browser-native backpressure for free: a backgrounded tab stops
 * getting frames, so the buffer simply coalesces — which pairs exactly with the
 * server-side conflation in `fx_api/ws/conflator.py`.
 */

import { useEffect, useState } from "react";
import { ForexStreamClient, type ServerFrame } from "../lib/stream/client";
import type { MarketStore } from "../lib/stream/store";
import type { Bar } from "../lib/stream/types";

/** The banner calls the feed stale past 60s, so recovery has to start before a
 *  visitor has finished reading the word. */
const STALE_AFTER_S = 75;
const STALE_CHECK_MS = 15_000;

/** Backoff for automatic retries, in checks. Rebuilding the socket every 15s
 *  against an ingestor that is simply switched off is the same reconnect storm
 *  the ingestor's own ladder exists to prevent — see MIN_HEALTHY_SESSION_S in
 *  fx_ingestor/main.py. A dead feed is usually dead for hours, so the interval
 *  has to grow: 15s, 30s, 60s ... capped at five minutes. */
const MAX_BACKOFF_CHECKS = 20; // 20 x 15s = 5 minutes

export function useForexStream(store: MarketStore, url: string, symbols: string[]): void {
  const key = symbols.join(",");
  // Bumping this tears the socket down and builds a new one. That is the whole
  // reconnect mechanism: the effect below already owns the full lifecycle, so
  // re-running it is a cleaner recovery than reaching into a live client.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    // A socket can stay OPEN while the feed behind it has stopped — the machine
    // slept, the ingestor died, the container was stopped. Nothing in the
    // WebSocket layer notices, so the page sits on "Stale" forever with a
    // perfectly healthy connection. Watch the heartbeat and rebuild when it
    // stops, which is the only signal that actually tracks the feed.
    let ticks = 0;
    let waitFor = 1; // checks to skip before the next automatic retry

    const watchdog = window.setInterval(() => {
      const ts = store.feed().ts;
      if (!ts) return;

      const age = (Date.now() - Date.parse(ts)) / 1000;
      if (age <= STALE_AFTER_S) {
        // Recovered. Reset so the next outage retries promptly rather than
        // inheriting a five-minute wait from the last one.
        ticks = 0;
        waitFor = 1;
        return;
      }

      // A backgrounded tab is not a broken feed. Browsers throttle timers in
      // hidden tabs, so a laptop that slept wakes with a huge apparent age and
      // would reconnect on the spot — before the user has even looked at it.
      if (document.hidden) return;

      if (++ticks < waitFor) return;
      ticks = 0;
      waitFor = Math.min(waitFor * 2, MAX_BACKOFF_CHECKS);
      setAttempt((n) => n + 1);
    }, STALE_CHECK_MS);
    return () => window.clearInterval(watchdog);
  }, [store]);

  // Exposed so the banner's refresh control can force the same path a user
  // would otherwise get by reloading the page.
  // The manual control bypasses the backoff above. Someone who clicked Refresh
  // is present and asking now; making them wait out an exponential delay they
  // cannot see is the worst possible answer.
  useEffect(() => store.onRefreshRequest(() => setAttempt((n) => n + 1)), [store]);

  useEffect(() => {
    // StrictMode mounts twice in dev, and a socket that is closing can still
    // deliver a frame. Without this the second client's store gets writes from
    // the first one's dying connection — rare, silent, and maddening to debug.
    let live = true;

    const client = new ForexStreamClient({
      url,
      symbols,
      onStateChange: (state, attempt) => {
        if (live) store.setConn(state, attempt);
      },
      onFrame: (frame: ServerFrame) => {
        if (!live) return;
        switch (frame.type) {
          case "tick":
            store.offer({ symbol: frame.s, bid: frame.b, ask: frame.a, mid: frame.m, ts: frame.t });
            break;

          case "vol": {
            store.setVol({
              symbol: frame.s,
              sigma: frame.sigma,
              sigmaAnn: frame.sigma_annualized,
              z: frame.z,
              regime: frame.regime,
              warm: frame.warmed_up,
              ts: frame.ts,
              enterZ: frame.enter_z,
              exitZ: frame.exit_z,
            });
            // A gated bar has no z. Appending null so the series stayed
            // contiguous would draw the blind minutes as calm, which is the one
            // thing this pane must never do — the hole is the honest answer.
            if (frame.z !== null && frame.regime !== "unknown") {
              store.pushZ(frame.s, {
                t: Math.floor(new Date(frame.ts).getTime() / 60_000) * 60,
                z: frame.z,
                r: frame.regime,
              });
            }
            break;
          }

          case "alert": {
            const { type: _, ...transition } = frame;
            store.pushAlert(transition);
            break;
          }

          case "snapshot": {
            // Snapshot-then-delta: apply the consistent picture of "now", then
            // let the buffered deltas land on top of it.
            const bars: Record<string, readonly Bar[]> = {};
            for (const [symbol, snap] of Object.entries(frame.data)) {
              bars[symbol] = snap.bars ?? [];
              store.setZHist(symbol, snap.zhist ?? []);
              store.seedRegime(symbol, snap.regime?.regime ?? "unknown");
              if (snap.quote?.mid !== undefined) {
                store.offer({
                  symbol,
                  bid: snap.quote.bid ?? snap.quote.mid,
                  ask: snap.quote.ask ?? snap.quote.mid,
                  mid: snap.quote.mid,
                  ts: snap.quote.ts ?? new Date().toISOString(),
                });
              }
            }
            store.setBars(bars);
            break;
          }

          case "status":
            store.setFeed({ state: frame.state, detail: frame.detail, ts: frame.ts });
            break;

          case "hello":
            store.setFeed(frame.feed);
            break;
        }
      },
    });

    client.connect();

    // Stop paying for data nobody is looking at. Saves the user's bandwidth and
    // our server's CPU, and costs four lines.
    const onVisibility = () => {
      if (document.hidden) client.unsubscribe(symbols);
      else client.subscribe(symbols);
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      live = false;
      document.removeEventListener("visibilitychange", onVisibility);
      client.close();
    };
    // Keyed on the joined string, not the array: `symbols` is a fresh reference
    // on every render at most call sites, and depending on it would tear down
    // and rebuild the socket on each one.
  }, [store, url, key, attempt]);
}
