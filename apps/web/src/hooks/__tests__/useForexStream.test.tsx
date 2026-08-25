import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAlerts, useConn, useFeed, useQuote, useRegime, useVol, useZHist } from "../../lib/stream/hooks";
import { StreamProvider } from "../../lib/stream/provider";
import { flushFrames } from "../../test/setup";

/**
 * The wire → store adapter: the only place in the app that knows what a server
 * frame looks like. Every field name here is a protocol contract with
 * `fx_api/ws/protocol.py`, and a rename on either side is invisible to the type
 * checker because the payload arrives as JSON.
 */

class FakeSocket {
  static readonly OPEN = 1;
  static last: FakeSocket | null = null;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 1;
  sent: string[] = [];
  closed = false;

  constructor(public url: string) {
    FakeSocket.last = this;
  }
  send(p: string): void {
    this.sent.push(p);
  }
  close(): void {
    this.closed = true;
    this.readyState = 3;
    this.onclose?.();
  }
}

beforeEach(() => {
  FakeSocket.last = null;
  vi.stubGlobal("WebSocket", FakeSocket);
});

function Probe() {
  const q = useQuote("EURUSD");
  const vol = useVol("EURUSD");
  const regime = useRegime("EURUSD");
  const alerts = useAlerts("EURUSD");
  const z = useZHist("EURUSD");
  const feed = useFeed();
  const conn = useConn();
  return (
    <dl>
      <dd data-testid="mid">{q ? q.mid : "—"}</dd>
      <dd data-testid="z">{vol?.z ?? "—"}</dd>
      <dd data-testid="band">{vol?.enterZ ?? "—"}</dd>
      <dd data-testid="regime">{regime}</dd>
      <dd data-testid="alerts">{alerts.length}</dd>
      <dd data-testid="zhist">{z.length}</dd>
      <dd data-testid="feed">{feed.state}</dd>
      <dd data-testid="conn">{conn.state}</dd>
    </dl>
  );
}

const mount = () =>
  render(
    <StreamProvider url="ws://test/ws/stream" symbols={["EURUSD"]}>
      <Probe />
    </StreamProvider>,
  );

const frame = (payload: unknown) =>
  act(() => {
    FakeSocket.last!.onmessage?.({ data: JSON.stringify(payload) });
  });

const at = (min: number) => new Date(Date.UTC(2026, 7, 25, 12, min)).toISOString();
const read = (id: string) => screen.getByTestId(id).textContent;

describe("frame handling", () => {
  it("routes a tick through the conflating buffer, not straight to React", () => {
    mount();
    frame({ type: "tick", s: "EURUSD", b: 1.084, a: 1.0841, m: 1.08405, t: at(0) });

    // Still buffered: a tick must not schedule a render on its own.
    expect(read("mid")).toBe("—");
    act(() => void flushFrames());
    expect(read("mid")).toBe("1.08405");
  });

  it("carries the hysteresis band with the reading", () => {
    // Thresholds ride on the vol frame precisely so the pane cannot draw a band
    // that disagrees with the z it is plotting.
    mount();
    frame({
      type: "vol",
      s: "EURUSD",
      ts: at(1),
      sigma: 0.0004,
      sigma_annualized: 0.08,
      z: 2.4,
      warmed_up: true,
      regime: "normal",
      enter_z: 3,
      exit_z: 1.5,
    });
    expect(read("z")).toBe("2.4");
    expect(read("band")).toBe("3");
    expect(read("zhist")).toBe("1");
  });

  it("does not append a z point for a bar it could not evaluate", () => {
    // A gated bar has no z. A null appended to keep the series contiguous would
    // draw the blind minutes as calm.
    mount();
    frame({
      type: "vol",
      s: "EURUSD",
      ts: at(2),
      sigma: 0,
      sigma_annualized: 0,
      z: null,
      warmed_up: false,
      regime: "unknown",
      enter_z: 3,
      exit_z: 1.5,
    });
    expect(read("zhist")).toBe("0");
  });

  it("applies a transition immediately rather than waiting for a frame", () => {
    mount();
    frame({
      type: "alert",
      s: "EURUSD",
      seq: 1,
      ts: at(3),
      old_regime: "normal",
      new_regime: "stressed",
      trigger_value: 3.4,
      threshold_value: 3,
      sigma: 0.0004,
      cause: "threshold",
      reason: "held",
    });
    expect(read("alerts")).toBe("1");
    expect(read("regime")).toBe("stressed");
  });

  it("seeds from a snapshot without clobbering a live transition", () => {
    // The snapshot is assembled server-side and can arrive after an alert that
    // superseded it. Last-write-wins would walk the UI backwards.
    mount();
    frame({
      type: "alert",
      s: "EURUSD",
      seq: 1,
      ts: at(4),
      old_regime: "stressed",
      new_regime: "normal",
      trigger_value: 0.4,
      threshold_value: 1.5,
      sigma: 0.0004,
      cause: "threshold",
      reason: "cleared",
    });
    frame({
      type: "snapshot",
      data: {
        EURUSD: {
          quote: { bid: 1.08, ask: 1.081, mid: 1.0805, ts: at(4) },
          bars: [{ t: 1_787_000_000, o: 1.08, h: 1.08, l: 1.08, c: 1.08, n: 5, src: "stream" }],
          vol: {},
          zhist: [{ t: 1_787_000_000, z: 1.1, r: "normal" }],
          regime: { regime: "stressed", seq: 0 },
        },
      },
    });

    expect(read("regime")).toBe("normal");
    expect(read("zhist")).toBe("1");
  });

  it("takes feed health from hello and from status", () => {
    mount();
    frame({ type: "hello", protocol: 1, server_time: at(0), max_symbols: 25, feed: { state: "healthy" } });
    expect(read("feed")).toBe("healthy");

    frame({ type: "status", state: "degraded", detail: "upstream reconnecting", ts: at(5) });
    expect(read("feed")).toBe("degraded");
  });

  it("ignores frames it does not understand", () => {
    // Forward compatibility: a newer server adding a frame type must not break
    // an older tab.
    mount();
    expect(() => frame({ type: "quantum_flux", s: "EURUSD" })).not.toThrow();
    expect(() => frame({ type: "pong", ts: at(0) })).not.toThrow();
  });

  it("survives an unparseable payload", () => {
    mount();
    act(() => {
      FakeSocket.last!.onmessage?.({ data: "not json at all" });
    });
    expect(read("conn")).toBeTruthy();
  });
});

describe("connection lifecycle", () => {
  it("reports open and subscribes with the requested symbols", () => {
    mount();
    act(() => void FakeSocket.last!.onopen?.());

    expect(read("conn")).toBe("open");
    expect(JSON.parse(FakeSocket.last!.sent[0]!)).toMatchObject({
      op: "subscribe",
      symbols: ["EURUSD"],
    });
  });

  it("closes the socket on unmount", () => {
    const view = mount();
    const ws = FakeSocket.last!;
    view.unmount();
    expect(ws.closed).toBe(true);
  });

  it("drops frames from a socket that is being torn down", () => {
    /**
     * StrictMode mounts twice in development, and a closing socket can still
     * deliver. Without the `live` guard the second client's store takes writes
     * from the first one's dying connection - rare, silent, and maddening.
     */
    const view = mount();
    const stale = FakeSocket.last!;
    view.unmount();

    expect(() =>
      stale.onmessage?.({
        data: JSON.stringify({ type: "tick", s: "EURUSD", b: 9, a: 9, m: 9, t: at(9) }),
      }),
    ).not.toThrow();
  });

  it("unsubscribes when the tab is hidden and resubscribes when it returns", () => {
    // Stop paying for data nobody is looking at.
    mount();
    act(() => void FakeSocket.last!.onopen?.());
    const ws = FakeSocket.last!;
    ws.sent.length = 0;

    Object.defineProperty(document, "hidden", { value: true, configurable: true });
    act(() => void document.dispatchEvent(new Event("visibilitychange")));
    expect(JSON.parse(ws.sent.at(-1)!).op).toBe("unsubscribe");

    Object.defineProperty(document, "hidden", { value: false, configurable: true });
    act(() => void document.dispatchEvent(new Event("visibilitychange")));
    expect(JSON.parse(ws.sent.at(-1)!).op).toBe("subscribe");
  });
});
