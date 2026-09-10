import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StoreCtx } from "../stream/hooks";
import { MarketStore } from "../stream/store";
import type { Bar } from "../stream/types";
import { useChartBars } from "../useChartBars";

/**
 * The dangerous part of this hook is not fetching, it is the two ways a fetch
 * can land at the wrong moment:
 *
 *   * a slow response for a timeframe the user has already clicked away from,
 *     which would draw daily candles on a 5-minute axis;
 *   * an aborted request reported as a failure, which would flash "could not
 *     load" every time somebody clicks along the row.
 *
 * Both are invisible in manual testing on a fast connection and obvious to a
 * user on a slow one.
 */

let store: MarketStore;

const bar = (t: number, c = 1.1): Bar => ({ t, o: 1.1, h: 1.2, l: 1.0, c, n: 5, src: "replay" });

function Probe({ symbol = "EURUSD", tf }: { symbol?: string; tf: never | string }) {
  const { bars, loading, error } = useChartBars(symbol, tf as never);
  return (
    <div>
      <span data-testid="count">{bars.length}</span>
      <span data-testid="last">{bars.at(-1)?.c ?? "-"}</span>
      <span data-testid="state">{error ? "error" : loading ? "loading" : "idle"}</span>
    </div>
  );
}

const mount = (tf: string) =>
  render(
    <StoreCtx.Provider value={store}>
      <Probe tf={tf} />
    </StoreCtx.Provider>,
  );

const ok = (bars: Bar[]) =>
  ({ ok: true, json: async () => ({ bars }) }) as unknown as Response;

beforeEach(() => {
  store = new MarketStore();
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("the live timeframe", () => {
  it("reads the store and never hits the network", () => {
    // 1m already arrives over the WebSocket, complete with the in-progress
    // candle. Fetching it too would duplicate a stream we already pay for.
    act(() => store.setBars({ EURUSD: [bar(0), bar(60)] }));
    mount("1m");

    expect(screen.getByTestId("count")).toHaveTextContent("2");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("follows live updates", () => {
    act(() => store.setBars({ EURUSD: [bar(0)] }));
    mount("1m");
    act(() => store.setBars({ EURUSD: [bar(0), bar(60), bar(120)] }));
    expect(screen.getByTestId("count")).toHaveTextContent("3");
  });
});

describe("fetched timeframes", () => {
  it("requests the interval it was asked for", async () => {
    vi.mocked(fetch).mockResolvedValue(ok([bar(0)]));
    mount("4h");

    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(vi.mocked(fetch).mock.calls[0]![0]).toContain("interval=4h");
  });

  it("stays on this origin so the proxy can do its job", async () => {
    vi.mocked(fetch).mockResolvedValue(ok([bar(0)]));
    mount("1d");

    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(String(vi.mocked(fetch).mock.calls[0]![0])).toMatch(/^\/api\/bars\//);
  });

  it("renders what came back", async () => {
    vi.mocked(fetch).mockResolvedValue(ok([bar(0, 1.5), bar(60, 1.7)]));
    mount("1h");
    await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("2"));
    expect(screen.getByTestId("last")).toHaveTextContent("1.7");
  });

  it("says so when the request fails", async () => {
    // An empty pane is indistinguishable from "this market has no history".
    vi.mocked(fetch).mockResolvedValue({ ok: false, status: 500 } as Response);
    mount("1w");
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("error"));
  });

  it("says so when the network is gone", async () => {
    vi.mocked(fetch).mockRejectedValue(new TypeError("Failed to fetch"));
    mount("1w");
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("error"));
  });
});

describe("switching away mid-flight", () => {
  it("does not let a stale response overwrite the current timeframe", async () => {
    // The race: click 1w, it is slow, click 5m, 5m returns first, THEN 1w
    // lands. Without the guard the chart shows weekly candles while the button
    // row says 5m — and the next 60s refresh silently corrects it, so it looks
    // like a transient glitch rather than a bug.
    let resolveSlow!: (r: Response) => void;
    const slow = new Promise<Response>((r) => {
      resolveSlow = r;
    });
    vi.mocked(fetch).mockReturnValueOnce(slow).mockResolvedValue(ok([bar(0, 5.5)]));

    const view = mount("1w");
    view.rerender(
      <StoreCtx.Provider value={store}>
        <Probe tf="5m" />
      </StoreCtx.Provider>,
    );

    await waitFor(() => expect(screen.getByTestId("last")).toHaveTextContent("5.5"));

    await act(async () => {
      resolveSlow(ok([bar(0, 9.9)]));
      await slow;
    });

    expect(screen.getByTestId("last")).toHaveTextContent("5.5");
  });

  it("does not report an abort as a failure", async () => {
    // Aborting is what WE did on purpose. Surfacing it would flash an error
    // banner every time somebody clicks along the row.
    //
    // Two details make this test able to fail. The destination is 4h, not 1m:
    // on the live timeframe the hook reports error:false unconditionally, so
    // nothing about the guard would be observable. And the SECOND request never
    // settles, because a successful response clears the error flag on its way
    // in - which would hide an unguarded abort behind the very next round trip.
    let aborted = 0;
    vi.mocked(fetch)
      .mockImplementationOnce(
        (_u, init) =>
          new Promise((_res, rej) => {
            (init as RequestInit).signal?.addEventListener("abort", () => {
              aborted++;
              rej(new DOMException("aborted", "AbortError"));
            });
          }),
      )
      .mockReturnValue(new Promise<Response>(() => {}));

    const view = mount("1w");
    view.rerender(
      <StoreCtx.Provider value={store}>
        <Probe tf="4h" />
      </StoreCtx.Provider>,
    );

    await waitFor(() => expect(aborted).toBe(1));
    // Still waiting on 4h, so "loading" is correct and "error" is not.
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("loading"));
  });

  it("drops the previous series before the new one arrives", async () => {
    // 1d -> 4h with the second request still in flight. Holding the daily bars
    // meanwhile draws them under a 4h label - confidently wrong data for the
    // width of one round trip.
    vi.mocked(fetch).mockResolvedValueOnce(ok([bar(0, 3.3), bar(86_400, 3.4)]));
    const view = mount("1d");
    await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("2"));

    vi.mocked(fetch).mockReturnValue(new Promise<Response>(() => {})); // never settles
    view.rerender(
      <StoreCtx.Provider value={store}>
        <Probe tf="4h" />
      </StoreCtx.Provider>,
    );

    expect(screen.getByTestId("count")).toHaveTextContent("0");
  });

  it("shows live bars, not stale fetched ones, on the way back to 1m", async () => {
    vi.mocked(fetch).mockResolvedValue(ok([bar(0, 3.3), bar(86_400, 3.4)]));
    const view = mount("1d");
    await waitFor(() => expect(screen.getByTestId("count")).toHaveTextContent("2"));

    act(() => store.setBars({ EURUSD: [bar(0)] }));
    view.rerender(
      <StoreCtx.Provider value={store}>
        <Probe tf="1m" />
      </StoreCtx.Provider>,
    );

    expect(screen.getByTestId("count")).toHaveTextContent("1");
  });
});
