import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TF_SECONDS, foldMinutes } from "../candles";
import { StoreCtx } from "../stream/hooks";
import { MarketStore } from "../stream/store";
import type { Bar } from "../stream/types";
import { useChartBars } from "../useChartBars";

/**
 * Where the chart's candles come from. Two properties matter most:
 *
 *   * switching timeframe asks the network for nothing - every intraday
 *     timeframe is folded from minutes already held, so the switch is instant
 *     and the new timeframe's last candle is as live as the old one's;
 *   * a request that lands at the wrong moment - for a pair the user has left,
 *     or aborted on purpose - changes nothing and reports no failure.
 */

let store: MarketStore;
const T = 1_792_598_400; // Wednesday 2026-10-21 16:00 UTC
const DAY = 1_792_540_800; // its UTC midnight

const minute = (t: number, src = "stream", c = 1.1): Bar => ({
  t,
  o: 1.1,
  h: 1.1 + ((t / 60) % 7) * 1e-4,
  l: 1.1 - ((t / 60) % 5) * 1e-4,
  c,
  n: src === "stream" ? 5 : 0,
  src,
});
const run = (from: number, count: number, src = "stream") =>
  Array.from({ length: count }, (_, i) => minute(from + i * 60, src));

function Probe({ symbol = "EURUSD", tf }: { symbol?: string; tf: string }) {
  const { bars, loading, error, coverage } = useChartBars(symbol, tf as never);
  return (
    <div>
      <span data-testid="count">{bars.length}</span>
      <span data-testid="first">{bars[0]?.t ?? "-"}</span>
      <span data-testid="last">{bars.at(-1)?.c ?? "-"}</span>
      <span data-testid="src">{bars.at(-1)?.src ?? "-"}</span>
      <span data-testid="state">{error ? "error" : loading ? "loading" : "idle"}</span>
      <span data-testid="coverage">
        {coverage ? `${coverage.observed}/${coverage.expected}` : "-"}
      </span>
    </div>
  );
}

const ui = (tf: string, symbol = "EURUSD") => (
  <StoreCtx.Provider value={store}>
    <Probe tf={tf} symbol={symbol} />
  </StoreCtx.Provider>
);
const read = (id: string) => screen.getByTestId(id).textContent;

const ok = (bars: Bar[]) => ({ ok: true, json: async () => ({ bars }) }) as unknown as Response;
const never = () => new Promise<Response>(() => {});
const urls = () => vi.mocked(fetch).mock.calls.map((c) => String(c[0]));

/** History already loaded, so a test can be about something else. */
function loaded(symbol: string, bars: Bar[]) {
  store.mergeMinutes(symbol, bars, "history");
  store.historyLoaded(symbol, store.minuteGen(symbol));
}

beforeEach(() => {
  store = new MarketStore();
  vi.stubGlobal("fetch", vi.fn(never));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("intraday timeframes", () => {
  it.each(["1m", "3m", "5m", "10m", "15m", "30m"] as const)(
    "%s is folded from the minutes held, with no request of its own",
    (tf) => {
      const minutes = run(T, 120);
      act(() => loaded("EURUSD", minutes));
      render(ui(tf));
      expect(read("count")).toBe(String(foldMinutes(minutes, TF_SECONDS[tf]).length));
      expect(fetch).not.toHaveBeenCalled();
    },
  );

  it("switches timeframe instantly, without a request", () => {
    act(() => loaded("EURUSD", run(T, 90)));
    const view = render(ui("1m"));
    expect(read("count")).toBe("90");
    view.rerender(ui("15m"));
    expect(read("count")).toBe("6");
    view.rerender(ui("30m"));
    expect(read("count")).toBe("3");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("follows each sealed minute", () => {
    act(() => loaded("EURUSD", run(T, 3)));
    render(ui("3m"));
    expect(read("count")).toBe("1");
    act(() => store.sealMinute("EURUSD", minute(T + 180)));
    expect(read("count")).toBe("2");
  });

  it("asks for a day of minutes once per pair, from this origin", async () => {
    vi.mocked(fetch).mockResolvedValue(ok(run(T - 3_600, 60, "backfill")));
    const view = render(ui("5m"));
    await waitFor(() => expect(read("count")).toBe("12"));
    expect(urls()).toEqual(["/api/bars/EURUSD?interval=1m&limit=1440"]);

    view.rerender(ui("15m"));
    view.rerender(ui("1m"));
    expect(fetch).toHaveBeenCalledTimes(1);

    view.rerender(ui("1m", "GBPUSD"));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(urls()[1]).toBe("/api/bars/GBPUSD?interval=1m&limit=1440");
  });

  it("draws the snapshot's minutes while the request is in flight", () => {
    act(() => store.mergeMinutes("EURUSD", run(T, 30)));
    render(ui("5m"));
    expect(read("count")).toBe("6");
    expect(read("state")).toBe("idle");
  });

  it("asks again only after a reconnect whose snapshot left a hole", async () => {
    act(() => loaded("EURUSD", run(T, 30)));
    render(ui("1m"));
    act(() => store.mergeMinutes("EURUSD", run(T + 20 * 60, 30)));
    expect(fetch).not.toHaveBeenCalled();

    act(() => store.mergeMinutes("EURUSD", run(T + 300 * 60, 30)));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  });

  it("says so when the request fails, and keeps what it has", async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: false, status: 500 } as Response);
    act(() => store.mergeMinutes("EURUSD", run(T, 10)));
    render(ui("1m"));
    await waitFor(() => expect(read("state")).toBe("error"));
    expect(read("count")).toBe("10");
  });

  it("tries again a minute after a failure", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    vi.mocked(fetch).mockRejectedValueOnce(new TypeError("Failed to fetch"));
    render(ui("1m"));
    // Timers are fake here, so settle the rejected request by hand.
    await act(async () => {
      for (let i = 0; i < 5; i++) await Promise.resolve();
    });
    expect(read("state")).toBe("error");
    await act(async () => void vi.advanceTimersByTime(60_000));
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it("reports loading only when there is nothing to draw yet", () => {
    render(ui("1m"));
    expect(read("state")).toBe("loading");
  });
});

describe("a request that lands at the wrong moment", () => {
  it("does not land on the pair the user has moved to", async () => {
    let resolveSlow!: (r: Response) => void;
    vi.mocked(fetch)
      .mockReturnValueOnce(new Promise<Response>((r) => (resolveSlow = r)))
      .mockImplementation(never);
    const view = render(ui("1m", "EURUSD"));
    view.rerender(ui("1m", "GBPUSD"));

    await act(async () => resolveSlow(ok(run(T, 50))));

    expect(read("count")).toBe("0");
    expect(store.minutes("GBPUSD")).toEqual([]);
  });

  it("does not report its own abort as a failure", async () => {
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
      .mockImplementation(never);

    const view = render(ui("1m", "EURUSD"));
    view.rerender(ui("1m", "GBPUSD"));

    await waitFor(() => expect(aborted).toBe(1));
    expect(read("state")).toBe("loading");
  });

  it("never draws one pair's candles under another's name", () => {
    act(() => {
      loaded("EURUSD", run(T, 10));
      loaded("USDJPY", []);
    });
    const view = render(ui("1m", "EURUSD"));
    expect(read("count")).toBe("10");
    view.rerender(ui("1m", "USDJPY"));
    expect(read("count")).toBe("0");
  });
});

describe("1D", () => {
  const days: Bar[] = [
    { t: DAY - 2 * 86_400, o: 1.09, h: 1.1, l: 1.08, c: 1.095, n: 0, src: "backfill" },
    { t: DAY - 86_400, o: 1.095, h: 1.11, l: 1.09, c: 1.1, n: 0, src: "backfill" },
    { t: DAY, o: 1.1, h: 1.12, l: 1.05, c: 1.1, n: 0, src: "backfill" },
  ];
  const byInterval = (daily: Bar[], minutes: Bar[]) => (url: string) =>
    Promise.resolve(ok(String(url).includes("interval=1d") ? daily : minutes));

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime((DAY + 600 * 60) * 1000); // 10:00 UTC
  });

  it("joins the provider's days with today's minutes and says how complete today is", async () => {
    vi.mocked(fetch).mockImplementation(byInterval(days, run(DAY, 600)) as never);
    render(ui("1d"));
    await waitFor(() => expect(read("coverage")).toBe("600/600"));
    expect(read("count")).toBe("3");
    expect(read("src")).toBe("mixed");
    expect(urls()).toContain("/api/bars/EURUSD?interval=1d&limit=300");
  });

  it("calls today partial when minutes of it are missing", async () => {
    const gappy = run(DAY, 600).filter((_, i) => i % 100 !== 0);
    vi.mocked(fetch).mockImplementation(byInterval(days, gappy) as never);
    render(ui("1d"));
    await waitFor(() => expect(read("coverage")).toBe("594/600"));
  });

  it("keeps the daily series for a revisit", async () => {
    vi.mocked(fetch).mockImplementation(byInterval(days, run(DAY, 600)) as never);
    const view = render(ui("1d"));
    await waitFor(() => expect(read("count")).toBe("3"));
    view.rerender(ui("5m"));
    view.rerender(ui("1d"));
    expect(read("count")).toBe("3");
    expect(urls().filter((u) => u.includes("interval=1d"))).toHaveLength(1);
  });

  it("says so when the daily request fails", async () => {
    vi.mocked(fetch).mockImplementation(((url: string) =>
      String(url).includes("interval=1d")
        ? Promise.reject(new TypeError("Failed to fetch"))
        : Promise.resolve(ok([]))) as never);
    render(ui("1d"));
    await waitFor(() => expect(read("state")).toBe("error"));
  });
});
