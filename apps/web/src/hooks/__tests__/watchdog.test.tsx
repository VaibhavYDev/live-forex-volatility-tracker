import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MarketStore } from "../../lib/stream/store";
import { useForexStream } from "../useForexStream";

/**
 * The failure this exists for: a laptop sleeps, the ingestor stops, and the
 * WebSocket stays OPEN because nothing in that layer noticed. The page then
 * reported "Stale - last update 66237s ago" indefinitely with a connection it
 * considered perfectly healthy, and only a manual reload fixed it.
 *
 * The fix has to recover WITHOUT becoming the thing it is recovering from: a
 * socket rebuilt every 15s against a switched-off ingestor is the same
 * reconnect storm the ingestor's own backoff ladder exists to prevent.
 */

const OPEN_SPY = vi.fn();

vi.mock("../../lib/stream/client", () => ({
  ForexStreamClient: class {
    constructor(opts: { onStateChange: (s: string, n: number) => void }) {
      OPEN_SPY();
      opts.onStateChange("open", 0);
    }
    connect() {}
    close() {}
  },
}));

const ago = (s: number) => new Date(Date.now() - s * 1000).toISOString();

function Harness({ store }: { store: MarketStore }) {
  useForexStream(store, "ws://x/ws/stream", ["EURUSD"]);
  return null;
}

let store: MarketStore;

beforeEach(() => {
  vi.useFakeTimers();
  OPEN_SPY.mockClear();
  store = new MarketStore();
});

afterEach(() => {
  vi.useRealTimers();
});

/** Advance N watchdog checks (15s each). */
const checks = (n: number) => act(() => void vi.advanceTimersByTime(n * 15_000));

describe("stale-feed recovery", () => {
  it("does nothing while the heartbeat keeps arriving", () => {
    // Re-stamped on every check, which is what a living ingestor does — its
    // heartbeat is 5s. Advancing fake timers also advances Date.now(), so a
    // single "fresh" stamp would age into staleness and the watchdog would be
    // right to fire.
    render(<Harness store={store} />);
    const before = OPEN_SPY.mock.calls.length;

    for (let i = 0; i < 10; i++) {
      act(() => store.setFeed({ state: "healthy", ts: new Date().toISOString() }));
      checks(1);
    }

    expect(OPEN_SPY.mock.calls.length).toBe(before);
  });

  it("rebuilds the socket once the feed stops", () => {
    render(<Harness store={store} />);
    act(() => store.setFeed({ state: "healthy", ts: ago(600) }));
    const before = OPEN_SPY.mock.calls.length;

    checks(2);

    expect(OPEN_SPY.mock.calls.length).toBeGreaterThan(before);
  });

  it("backs off instead of retrying every fifteen seconds", () => {
    // A feed that is off is usually off for hours. Retrying at a fixed 15s
    // would be four reconnects a minute, indefinitely, against a server that
    // has nothing to give.
    render(<Harness store={store} />);
    act(() => store.setFeed({ state: "healthy", ts: ago(600) }));
    const before = OPEN_SPY.mock.calls.length;

    checks(40); // ten minutes

    const retries = OPEN_SPY.mock.calls.length - before;
    expect(retries).toBeGreaterThan(0);
    expect(retries).toBeLessThan(8); // vs. 40 without backoff
  });

  it("does not reconnect from a hidden tab", () => {
    // Browsers throttle timers in background tabs, so a machine returning from
    // sleep surfaces a huge apparent age all at once. Reconnecting then races
    // ahead of the user actually looking at the page.
    const spy = vi.spyOn(document, "hidden", "get").mockReturnValue(true);
    try {
      render(<Harness store={store} />);
      act(() => store.setFeed({ state: "healthy", ts: ago(600) }));
      const before = OPEN_SPY.mock.calls.length;

      checks(20);

      expect(OPEN_SPY.mock.calls.length).toBe(before);
    } finally {
      spy.mockRestore();
    }
  });

  it("retries promptly again after a recovery", () => {
    // The backoff must not persist across outages: a feed that dropped once an
    // hour ago should not make the next drop wait five minutes.
    render(<Harness store={store} />);

    act(() => store.setFeed({ state: "healthy", ts: ago(600) }));
    checks(30); // climb the backoff
    act(() => store.setFeed({ state: "healthy", ts: ago(1) })); // recovered
    checks(2);

    const before = OPEN_SPY.mock.calls.length;
    act(() => store.setFeed({ state: "healthy", ts: ago(600) })); // dies again
    checks(2);

    expect(OPEN_SPY.mock.calls.length).toBeGreaterThan(before);
  });

  it("reconnects immediately when a person asks", () => {
    // Someone who clicked Refresh is present and asking now. Making them wait
    // out an exponential delay they cannot see is the worst possible answer.
    render(<Harness store={store} />);
    act(() => store.setFeed({ state: "healthy", ts: ago(600) }));
    checks(30); // deep into the backoff
    const before = OPEN_SPY.mock.calls.length;

    act(() => store.requestRefresh());

    expect(OPEN_SPY.mock.calls.length).toBeGreaterThan(before);
  });

  it("stays quiet before any status has arrived", () => {
    // No heartbeat yet is not a heartbeat that stopped.
    render(<Harness store={store} />);
    const before = OPEN_SPY.mock.calls.length;

    checks(20);

    expect(OPEN_SPY.mock.calls.length).toBe(before);
  });
});
