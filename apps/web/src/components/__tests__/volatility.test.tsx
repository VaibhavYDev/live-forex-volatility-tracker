import { act, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { StoreCtx } from "../../lib/stream/hooks";
import { MarketStore } from "../../lib/stream/store";
import { flushFrames } from "../../test/setup";
import { VolatilityPanel } from "../VolatilityPanel";

let store: MarketStore;

beforeEach(() => {
  store = new MarketStore();
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 409 }));
});

const panel = (symbol: string) => (
  <StoreCtx.Provider value={store}>
    <VolatilityPanel symbol={symbol} />
  </StoreCtx.Provider>
);

const offer = (symbol: string, bid: number, ask: number) =>
  act(() => {
    store.offer({ symbol, bid, ask, mid: (bid + ask) / 2, ts: new Date().toISOString() });
    flushFrames();
  });

const compare = (parkinson: number) => ({
  ok: true,
  status: 200,
  json: async () => ({
    bars_used: 120,
    window_s: 7200,
    annualization_basis: "252d",
    estimators: { parkinson },
  }),
});

describe("spread", () => {
  it("counts JPY pips in the second decimal", () => {
    render(panel("USDJPY"));
    offer("USDJPY", 147.236, 147.244);
    expect(screen.getByText("0.8 pips")).toBeInTheDocument();
  });

  it("counts other pips in the fourth decimal", () => {
    render(panel("EURUSD"));
    offer("EURUSD", 1.0842, 1.08426);
    expect(screen.getByText("0.6 pips")).toBeInTheDocument();
  });
});

describe("estimator comparison across a symbol switch", () => {
  it("drops the previous pair's figures when the new request fails", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(compare(0.0612));
    fetch.mockResolvedValue({ ok: false, status: 500 });
    vi.stubGlobal("fetch", fetch);
    const view = render(panel("EURUSD"));
    expect(await screen.findByText("6.12%")).toBeInTheDocument();

    view.rerender(panel("USDJPY"));

    expect(await screen.findByText(/Comparison unavailable/)).toBeInTheDocument();
    expect(screen.queryByText("6.12%")).not.toBeInTheDocument();
  });

  it("ignores a late response for the pair it left", async () => {
    let lateEurusd!: (r: unknown) => void;
    const fetch = vi
      .fn()
      .mockReturnValueOnce(new Promise((resolve) => (lateEurusd = resolve)))
      .mockResolvedValue(compare(0.0951));
    vi.stubGlobal("fetch", fetch);
    const view = render(panel("EURUSD"));

    view.rerender(panel("USDJPY"));
    expect(await screen.findByText("9.51%")).toBeInTheDocument();
    await act(async () => lateEurusd(compare(0.0612)));

    await waitFor(() => expect(screen.getByText("9.51%")).toBeInTheDocument());
    expect(screen.queryByText("6.12%")).not.toBeInTheDocument();
    expect(fetch.mock.calls[0]![1].signal.aborted).toBe(true);
  });
});
