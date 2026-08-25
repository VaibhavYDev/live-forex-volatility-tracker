import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StoreCtx } from "../../lib/stream/hooks";
import { PerfMonitor } from "../../lib/stream/perf";
import { MarketStore } from "../../lib/stream/store";
import { flushFrames, pendingFrames } from "../../test/setup";
import { PerfOverlay, perfRequested } from "../PerfOverlay";

let store: MarketStore;
beforeEach(() => {
  store = new MarketStore();
  vi.useFakeTimers({
    shouldAdvanceTime: true,
    toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"],
  });
});
afterEach(() => vi.useRealTimers());

const search = (qs: string) =>
  Object.defineProperty(window, "location", {
    value: { ...window.location, search: qs },
    configurable: true,
  });

describe("perfRequested", () => {
  it("is off unless asked for", () => {
    // The monitor keeps its own rAF loop, which would defeat the store's idle
    // guard and burn a wakeup per frame through a closed weekend.
    search("");
    expect(perfRequested()).toBe(false);
    search("?foo=1&perf=0");
    expect(perfRequested()).toBe(false);
  });

  it("turns on with ?perf=1", () => {
    search("?perf=1");
    expect(perfRequested()).toBe(true);
  });
});

describe("PerfOverlay", () => {
  const mount = () =>
    render(
      <StoreCtx.Provider value={store}>
        <PerfOverlay />
      </StoreCtx.Provider>,
    );

  it("publishes frame statistics rather than adjectives", () => {
    mount();
    act(() => void vi.advanceTimersByTime(600));
    expect(screen.getByLabelText(/frame diagnostics/i).textContent).toMatch(/fps .*dropped/);
  });

  it("reports the store's conflation counter", () => {
    mount();
    act(() => {
      const q = { symbol: "EURUSD", bid: 1, ask: 1, mid: 1, ts: "2026-08-25T12:00:00Z" };
      store.offer(q);
      store.offer({ ...q, mid: 2 });
      flushFrames();
    });
    act(() => void vi.advanceTimersByTime(600));
    expect(screen.getByLabelText(/frame diagnostics/i).textContent).toMatch(/conflated 1/);
  });

  it("stops its rAF loop on unmount", () => {
    // The loop re-arms itself every frame, so a leaked monitor keeps the tab
    // awake at 60Hz forever — exactly the cost the store's idle guard avoids.
    const view = mount();
    expect(pendingFrames()).toBe(1);

    view.unmount();
    expect(pendingFrames()).toBe(0);
    act(() => void flushFrames());
    expect(pendingFrames()).toBe(0);
  });
});

describe("PerfMonitor", () => {
  it("degrades rather than throwing where longtask is unavailable", () => {
    // Safari and Firefox do not ship the longtask entry type; frame timing still
    // works, so the monitor must survive the observe() rejection.
    class Hostile {
      observe(): never {
        throw new Error("longtask unsupported");
      }
      disconnect(): void {}
    }
    vi.stubGlobal("PerformanceObserver", Hostile);

    const monitor = new PerfMonitor();
    expect(() => monitor.start()).not.toThrow();
    expect(monitor.report()).toContain("fps");
    monitor.stop();
  });

  it("counts dropped frames from the gap between them", () => {
    const monitor = new PerfMonitor();
    monitor.start();
    // One frame ~100ms late is six missed budgets, not one.
    act(() => void vi.advanceTimersByTime(100));
    flushFrames();
    expect(monitor.stats.frames).toBeGreaterThan(0);
    monitor.stop();
  });
});
