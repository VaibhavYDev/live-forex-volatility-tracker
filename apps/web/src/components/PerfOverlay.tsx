import { useEffect, useState } from "react";
import { useStore } from "../lib/stream/hooks";
import { PerfMonitor } from "../lib/stream/perf";

/**
 * Frame health, on screen, behind `?perf=1`.
 *
 * `PerfMonitor` sat unreferenced for a milestone — 93 lines of instrumentation
 * measuring nothing while the README claimed a 50 msg/sec feed renders smoothly.
 * The store benchmark measures the store, not the render path, so that claim had
 * no evidence behind it. By this project's own standard that is an adjective
 * without a number.
 *
 * Off by default: the monitor keeps its own rAF loop alive, which would defeat
 * the store's idle guard and burn a wakeup per frame through a closed weekend.
 */

const REFRESH_MS = 500;

export function PerfOverlay() {
  const store = useStore();
  const [line, setLine] = useState("measuring…");
  const [conflated, setConflated] = useState(0);

  useEffect(() => {
    const monitor = new PerfMonitor();
    monitor.start();
    const timer = window.setInterval(() => {
      setLine(monitor.report());
      setConflated(store.stats.conflated);
    }, REFRESH_MS);

    return () => {
      window.clearInterval(timer);
      monitor.stop();
    };
  }, [store]);

  return (
    <aside className="perf" aria-label="Frame diagnostics">
      <span>{line}</span>
      <span>
        conflated {conflated} · worst flush {store.stats.maxFlushMs.toFixed(2)}ms
      </span>
    </aside>
  );
}

/** `?perf=1`. A query parameter rather than an env var so it can be turned on
 *  against a deployed build without a rebuild. */
export function perfRequested(): boolean {
  try {
    return new URLSearchParams(window.location.search).get("perf") === "1";
  } catch {
    return false;
  }
}
