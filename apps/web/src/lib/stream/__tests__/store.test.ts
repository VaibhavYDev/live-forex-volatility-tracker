import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushFrames, pendingFrames } from "../../../test/setup";
import { MarketStore } from "../store";
import type { Quote, Transition } from "../types";

const q = (symbol: string, mid: number): Quote => ({
  symbol,
  bid: mid - 0.00005,
  ask: mid + 0.00005,
  mid,
  ts: "2026-08-22T12:00:00Z",
});

const alert = (s: string, seq: number, to: "normal" | "stressed" = "stressed"): Transition => ({
  s,
  seq,
  ts: "2026-08-22T12:00:00Z",
  old_regime: to === "stressed" ? "normal" : "stressed",
  new_regime: to,
  trigger_value: 3.4,
  threshold_value: 3,
  sigma: 0.0004,
  cause: "threshold",
  reason: "z 3.4 held above 3.0 for 90s",
});

let store: MarketStore;
beforeEach(() => {
  store = new MarketStore();
});

/**
 * The contract React actually enforces. Every one of these assertions is an
 * identity check, not a value check — `toEqual` would pass on a store that
 * re-allocates on every read and renders in an infinite loop.
 */
describe("getSnapshot referential stability", () => {
  it("returns the same reference for an unchanged quote", () => {
    store.offer(q("EURUSD", 1.1));
    flushFrames();
    expect(store.quote("EURUSD")).toBe(store.quote("EURUSD"));
  });

  it("shares one frozen empty array across every symbol with no alerts", () => {
    // Two different symbols, same reference. A per-call `[]` here is invisible
    // in dev and pins the CPU at 100% the moment two panes mount.
    expect(store.alerts("EURUSD")).toBe(store.alerts("GBPUSD"));
    expect(Object.isFrozen(store.alerts("EURUSD"))).toBe(true);
    expect(store.bars("EURUSD")).toBe(store.bars("USDJPY"));
  });

  it("keeps the alert list stable when an unrelated symbol transitions", () => {
    store.pushAlert(alert("EURUSD", 1));
    const before = store.alerts("EURUSD");
    store.pushAlert(alert("GBPUSD", 1));
    expect(store.alerts("EURUSD")).toBe(before);
  });

  it("keeps feed and conn stable between changes", () => {
    store.setFeed({ state: "healthy", ts: "2026-08-22T12:00:00Z" });
    const feed = store.feed();
    store.setConn("open", 0);
    expect(store.feed()).toBe(feed);
  });

  // The flip side, and just as load-bearing: a snapshot that never changes
  // identity is a UI that never updates. Stability is not immutability.
  it("changes identity when the value actually changes", () => {
    store.offer(q("EURUSD", 1.1));
    flushFrames();
    const first = store.quote("EURUSD");
    store.offer(q("EURUSD", 1.2));
    flushFrames();
    expect(store.quote("EURUSD")).not.toBe(first);
  });

  it("never hands back a live array that could mutate under React", () => {
    store.pushAlert(alert("EURUSD", 1));
    const list = store.alerts("EURUSD");
    expect(Object.isFrozen(list)).toBe(true);
    store.pushAlert(alert("EURUSD", 2));
    expect(list).toHaveLength(1); // the old reference is still the old value
    expect(store.alerts("EURUSD")).toHaveLength(2);
  });
});

describe("rAF scheduling", () => {
  it("schedules nothing while idle", () => {
    // The FX market is shut ~48h a week. Re-arming rAF unconditionally burns
    // 60 wakeups/sec through every weekend to do nothing.
    expect(pendingFrames()).toBe(0);
    expect(store.pendingFrame).toBe(false);
  });

  it("arms exactly one frame no matter how many ticks land in it", () => {
    for (let i = 0; i < 200; i++) store.offer(q("EURUSD", 1.1 + i * 1e-5));
    expect(pendingFrames()).toBe(1);
  });

  it("goes back to idle after draining", () => {
    store.offer(q("EURUSD", 1.1));
    flushFrames();
    expect(pendingFrames()).toBe(0);
    expect(store.pendingFrame).toBe(false);
  });

  it("re-arms only if work arrived during the flush", () => {
    let listener: (() => void) | null = null;
    store.subQuote("EURUSD", () => listener?.());
    listener = () => store.offer(q("GBPUSD", 1.27)); // a subscriber that writes back
    store.offer(q("EURUSD", 1.1));
    flushFrames();
    expect(pendingFrames()).toBe(1);
  });
});

describe("conflation", () => {
  it("collapses a burst to the last price per symbol", () => {
    store.offer(q("EURUSD", 1.1));
    store.offer(q("EURUSD", 1.2));
    store.offer(q("EURUSD", 1.3));
    flushFrames();

    expect(store.quote("EURUSD")?.mid).toBe(1.3);
    expect(store.stats.conflated).toBe(2);
    expect(store.stats.applied).toBe(1);
  });

  it("does not conflate across symbols", () => {
    store.offer(q("EURUSD", 1.1));
    store.offer(q("GBPUSD", 1.27));
    flushFrames();

    expect(store.stats.conflated).toBe(0);
    expect(store.stats.applied).toBe(2);
  });

  it("fires the imperative tick listener once per frame, not once per message", () => {
    const paint = vi.fn();
    store.onTick("EURUSD", paint);
    for (let i = 0; i < 50; i++) store.offer(q("EURUSD", 1.1 + i * 1e-5));
    flushFrames();

    // The chart is a 60Hz surface. 50 canvas writes for one frame is 49 wasted.
    expect(paint).toHaveBeenCalledTimes(1);
    expect(paint.mock.calls[0]?.[0].mid).toBeCloseTo(1.1 + 49e-5, 10);
  });

  it("costs an unmounted symbol nothing but a failed lookup", () => {
    const paint = vi.fn();
    const off = store.onTick("EURUSD", paint);
    off();
    store.offer(q("EURUSD", 1.1));
    flushFrames();
    expect(paint).not.toHaveBeenCalled();
  });
});

describe("notification isolation", () => {
  it("does not wake GBPUSD subscribers on a EURUSD tick", () => {
    const eur = vi.fn();
    const gbp = vi.fn();
    store.subQuote("EURUSD", eur);
    store.subQuote("GBPUSD", gbp);

    store.offer(q("EURUSD", 1.1));
    flushFrames();

    expect(eur).toHaveBeenCalledTimes(1);
    expect(gbp).not.toHaveBeenCalled();
  });

  it("notifies a symbol once per flush even if several topics went dirty", () => {
    const vol = vi.fn();
    store.subVol("EURUSD", vol);
    store.setVol({
      symbol: "EURUSD",
      sigma: 0.0004,
      sigmaAnn: 0.08,
      z: 3.4,
      regime: "stressed",
      warm: true,
      ts: "2026-08-22T12:00:00Z",
    });
    // setVol dirties the vol topic and the regime writes through to it too.
    expect(vol).toHaveBeenCalledTimes(1);
  });

  it("drops the topic entry when the last subscriber leaves", () => {
    // Symbol tabs mount and unmount all session. A Map that only grows is a
    // leak that takes hours to become visible.
    const off1 = store.subQuote("EURUSD", vi.fn());
    const off2 = store.subQuote("EURUSD", vi.fn());
    off1();
    off2();
    store.offer(q("EURUSD", 1.1));
    expect(() => flushFrames()).not.toThrow();
    expect(store.quote("EURUSD")?.mid).toBe(1.1);
  });
});

describe("alerts are events, not state", () => {
  it("ignores a redelivered seq", () => {
    // Redis consumer groups are at-least-once. The gateway can and will replay.
    store.pushAlert(alert("EURUSD", 7));
    store.pushAlert(alert("EURUSD", 7));
    expect(store.alerts("EURUSD")).toHaveLength(1);
  });

  it("keeps newest first", () => {
    store.pushAlert(alert("EURUSD", 1));
    store.pushAlert(alert("EURUSD", 2, "normal"));
    expect(store.alerts("EURUSD").map((a) => a.seq)).toEqual([2, 1]);
  });

  it("bypasses the frame buffer entirely", () => {
    // An escalation must not wait on a backgrounded tab's rAF. "Stressed at
    // 14:03" is not superseded by anything, so it never enters the conflator.
    store.pushAlert(alert("EURUSD", 1));
    expect(pendingFrames()).toBe(0);
    expect(store.regime("EURUSD")).toBe("stressed");
  });

  it("caps history without dropping the newest", () => {
    for (let i = 1; i <= 80; i++) store.pushAlert(alert("EURUSD", i));
    const list = store.alerts("EURUSD");
    expect(list).toHaveLength(60);
    expect(list[0]?.seq).toBe(80);
  });
});

describe("the z series", () => {
  const z = (i: number, value: number) => ({ t: 1_787_000_000 + i * 60, z: value, r: "normal" as const });

  it("shares one frozen empty series across symbols with no history", () => {
    expect(store.zhist("EURUSD")).toBe(store.zhist("GBPUSD"));
    expect(Object.isFrozen(store.zhist("EURUSD"))).toBe(true);
  });

  it("sorts the snapshot into time order regardless of how it arrived", () => {
    // The renderer walks the array assuming it ascends; a single out-of-order
    // point draws the line backwards across the pane.
    store.setZHist("EURUSD", [z(2, 3), z(0, 1), z(1, 2)]);
    expect(store.zhist("EURUSD").map((p) => p.z)).toEqual([1, 2, 3]);
  });

  it("replaces rather than duplicates a bar the snapshot already had", () => {
    // A vol frame can restate the last bar the snapshot carried. Appending it
    // would leave two points at the same x, which reads as a vertical spike.
    store.setZHist("EURUSD", [z(0, 1), z(1, 2)]);
    store.pushZ("EURUSD", { ...z(1, 2.5), r: "stressed" });

    const out = store.zhist("EURUSD");
    expect(out).toHaveLength(2);
    expect(out[1]).toMatchObject({ z: 2.5, r: "stressed" });
  });

  it("keeps the series bounded", () => {
    for (let i = 0; i < 1_500; i++) store.pushZ("EURUSD", z(i, i));
    expect(store.zhist("EURUSD")).toHaveLength(1_440);
    expect(store.zhist("EURUSD")[1_439]!.z).toBe(1_499);
  });

  it("wakes only the pane for that symbol", () => {
    const eur = vi.fn();
    const gbp = vi.fn();
    store.subZ("EURUSD", eur);
    store.subZ("GBPUSD", gbp);

    store.pushZ("EURUSD", z(0, 1));
    expect(eur).toHaveBeenCalledTimes(1);
    expect(gbp).not.toHaveBeenCalled();
  });

  it("does not enter the frame buffer", () => {
    // One point per minute is not a 60fps signal, and routing it through the
    // conflator would delay it behind a backgrounded tab's rAF for no reason.
    store.pushZ("EURUSD", z(0, 1));
    expect(pendingFrames()).toBe(0);
  });
});

describe("the transition callback", () => {
  it("fires once per genuinely new transition", () => {
    const seen = vi.fn();
    store.onAlert(seen);

    store.pushAlert(alert("EURUSD", 1));
    store.pushAlert(alert("EURUSD", 1)); // redelivery
    store.pushAlert(alert("EURUSD", 2, "normal"));

    expect(seen).toHaveBeenCalledTimes(2);
  });

  it("fires after the list is updated, not before", () => {
    // A toast that reads the store from inside the callback must not see the
    // state from one transition ago.
    let lengthAtFire = -1;
    store.onAlert(() => void (lengthAtFire = store.alerts("EURUSD").length));
    store.pushAlert(alert("EURUSD", 1));
    expect(lengthAtFire).toBe(1);
  });

  it("stops on unsubscribe", () => {
    const seen = vi.fn();
    store.onAlert(seen)();
    store.pushAlert(alert("EURUSD", 1));
    expect(seen).not.toHaveBeenCalled();
  });
});

describe("regime seeding", () => {
  it("takes the snapshot regime when nothing is known", () => {
    store.seedRegime("EURUSD", "stressed");
    expect(store.regime("EURUSD")).toBe("stressed");
  });

  it("never clobbers a transition that beat the snapshot", () => {
    // The snapshot is assembled server-side and can arrive after a live alert
    // that superseded it. Last-write-wins here would walk the UI backwards.
    store.pushAlert(alert("EURUSD", 1, "normal"));
    store.seedRegime("EURUSD", "stressed");
    expect(store.regime("EURUSD")).toBe("normal");
  });

  it("reports unknown rather than guessing normal", () => {
    // A dashboard that defaults to "normal" is asserting calm it cannot see.
    expect(store.regime("EURUSD")).toBe("unknown");
  });
});
