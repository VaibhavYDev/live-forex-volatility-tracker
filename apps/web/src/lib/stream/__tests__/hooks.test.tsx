import { act, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushFrames } from "../../../test/setup";
import { StoreCtx, useAlerts, useQuote, useTicks } from "../hooks";
import { MarketStore } from "../store";
import type { Quote, Transition } from "../types";

let store: MarketStore;
beforeEach(() => {
  store = new MarketStore();
});

const wrap = (ui: ReactNode) => render(<StoreCtx.Provider value={store}>{ui}</StoreCtx.Provider>);

const q = (symbol: string, mid: number): Quote => ({
  symbol,
  bid: mid,
  ask: mid,
  mid,
  ts: "2026-08-22T12:00:00Z",
});

const tick = (symbol: string, mid: number) =>
  act(() => {
    store.offer(q(symbol, mid));
    flushFrames();
  });

/** Counts its own renders so the isolation claim is measured, not asserted. */
function Cell({ symbol, onRender }: { symbol: string; onRender: () => void }) {
  const quote = useQuote(symbol);
  onRender();
  return <span data-testid={symbol}>{quote ? quote.mid.toFixed(4) : "—"}</span>;
}

describe("per-symbol render isolation", () => {
  it("re-renders only the cell whose symbol ticked", () => {
    const eur = vi.fn();
    const gbp = vi.fn();
    wrap(
      <>
        <Cell symbol="EURUSD" onRender={eur} />
        <Cell symbol="GBPUSD" onRender={gbp} />
      </>,
    );
    eur.mockClear();
    gbp.mockClear();

    tick("EURUSD", 1.0912);

    expect(eur).toHaveBeenCalledTimes(1);
    expect(gbp).not.toHaveBeenCalled();
    expect(screen.getByTestId("EURUSD")).toHaveTextContent("1.0912");
  });

  it("does not re-render on a tick that changes nothing it reads", () => {
    // The whole point of the topic split: an alert on another pair is not this
    // cell's business, and neither is a feed status change.
    const eur = vi.fn();
    wrap(<Cell symbol="EURUSD" onRender={eur} />);
    eur.mockClear();

    act(() => store.setFeed({ state: "degraded", detail: "upstream reconnecting" }));

    expect(eur).not.toHaveBeenCalled();
  });

  it("settles after one render instead of looping", () => {
    // If `getSnapshot` allocated, React would re-render forever and this count
    // would be in the thousands (or the test would hang).
    const eur = vi.fn();
    wrap(<Cell symbol="EURUSD" onRender={eur} />);
    expect(eur).toHaveBeenCalledTimes(1);

    tick("EURUSD", 1.09);
    tick("EURUSD", 1.09); // identical value, new object from the wire
    expect(eur).toHaveBeenCalledTimes(3);
  });
});

describe("useAlerts", () => {
  const alert = (s: string, seq: number): Transition => ({
    s,
    seq,
    ts: "2026-08-22T12:00:00Z",
    old_regime: "normal",
    new_regime: "stressed",
    trigger_value: 3.4,
    threshold_value: 3,
    sigma: 0.0004,
    cause: "threshold",
    reason: "sustained",
  });

  function Feed({ symbol, onRender }: { symbol: string; onRender: () => void }) {
    const alerts = useAlerts(symbol);
    onRender();
    return <span data-testid={`a-${symbol}`}>{alerts.length}</span>;
  }

  it("mounts against an empty history without an extra render pass", () => {
    const spy = vi.fn();
    wrap(<Feed symbol="EURUSD" onRender={spy} />);
    expect(spy).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("a-EURUSD")).toHaveTextContent("0");
  });

  it("wakes only the pane for the symbol that transitioned", () => {
    const eur = vi.fn();
    const gbp = vi.fn();
    wrap(
      <>
        <Feed symbol="EURUSD" onRender={eur} />
        <Feed symbol="GBPUSD" onRender={gbp} />
      </>,
    );
    eur.mockClear();
    gbp.mockClear();

    act(() => store.pushAlert(alert("EURUSD", 1)));

    expect(eur).toHaveBeenCalledTimes(1);
    expect(gbp).not.toHaveBeenCalled();
  });
});

describe("useTicks", () => {
  function Canvas({ symbol, paint }: { symbol: string; paint: (q: Quote) => void }) {
    // Deliberately an inline arrow: the hook must tolerate the call site every
    // real component will actually write.
    useTicks(symbol, (quote) => paint(quote));
    return null;
  }

  it("delivers ticks without rendering the component", () => {
    const paint = vi.fn();
    const rendered = vi.fn();
    function Host() {
      rendered();
      return <Canvas symbol="EURUSD" paint={paint} />;
    }
    wrap(<Host />);
    rendered.mockClear();

    tick("EURUSD", 1.09);
    tick("EURUSD", 1.1);

    expect(paint).toHaveBeenCalledTimes(2);
    expect(rendered).not.toHaveBeenCalled();
  });

  it("stops on unmount", () => {
    const paint = vi.fn();
    const { unmount } = wrap(<Canvas symbol="EURUSD" paint={paint} />);
    unmount();
    tick("EURUSD", 1.09);
    expect(paint).not.toHaveBeenCalled();
  });
});

it("fails loudly outside a provider rather than rendering an empty terminal", () => {
  const quiet = vi.spyOn(console, "error").mockImplementation(() => {});
  expect(() => render(<Cell symbol="EURUSD" onRender={() => {}} />)).toThrow(/StoreProvider/);
  quiet.mockRestore();
});
