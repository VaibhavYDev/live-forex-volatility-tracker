import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StoreCtx } from "../../lib/stream/hooks";
import { MarketStore } from "../../lib/stream/store";
import { flushFrames } from "../../test/setup";
import { SymbolTab } from "../SymbolTab";

/**
 * A closed market sends no ticks. The last real quote stays on screen, but it
 * must say when it was quoted rather than pass for the current price.
 */

const mountWithQuote = (ts: string) => {
  const store = new MarketStore();
  act(() => {
    store.offer({ symbol: "EURUSD", bid: 1.0842, ask: 1.0843, mid: 1.08425, ts });
    flushFrames();
  });
  render(
    <StoreCtx.Provider value={store}>
      <SymbolTab symbol="EURUSD" active onSelect={() => {}} />
    </StoreCtx.Provider>,
  );
};

describe("SymbolTab", () => {
  it("dates a quote the market has stopped updating", () => {
    const ts = new Date(Date.now() - 3 * 3_600_000).toISOString();
    mountWithQuote(ts);

    expect(screen.getByText("1.08425")).toHaveAttribute("data-stale");
    expect(screen.getByText(`as of ${ts.slice(11, 16)} UTC`)).toBeInTheDocument();
  });

  it("shows a fresh quote undated", () => {
    mountWithQuote(new Date().toISOString());

    expect(screen.getByText("1.08425")).not.toHaveAttribute("data-stale");
    expect(screen.queryByText(/as of/)).toBeNull();
  });
});
