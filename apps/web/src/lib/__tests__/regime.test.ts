import { describe, expect, it } from "vitest";
import { humanDuration, spans, stressed } from "../regime";
import type { Transition } from "../stream/types";

const T0 = Date.parse("2026-08-22T12:00:00Z") / 1000;
const at = (min: number) => new Date((T0 + min * 60) * 1000).toISOString();

const t = (
  min: number,
  to: "normal" | "stressed",
  seq = min,
): Transition => ({
  s: "EURUSD",
  seq,
  ts: at(min),
  old_regime: to === "stressed" ? "normal" : "stressed",
  new_regime: to,
  trigger_value: 3.4,
  threshold_value: to === "stressed" ? 3 : 1.5,
  sigma: 0.0004,
  cause: "threshold",
  reason: "held",
});

/** The store keeps alerts newest first; every call here mirrors that. */
const newestFirst = (...xs: Transition[]) => [...xs].reverse();

describe("spans", () => {
  it("covers the whole window with no gaps or overlaps", () => {
    const out = spans(newestFirst(t(20, "stressed"), t(50, "normal")), T0, T0 + 90 * 60, "normal");

    expect(out[0]!.from).toBe(T0);
    expect(out[out.length - 1]!.to).toBe(T0 + 90 * 60);
    for (let i = 1; i < out.length; i++) {
      expect(out[i]!.from).toBe(out[i - 1]!.to);
    }
  });

  it("reads the span before a transition from its own old_regime", () => {
    // Not from the previous transition's new_regime. If an event were ever lost
    // in transit the two disagree, and the server's account of what it believed
    // at the time is the more trustworthy of the two.
    const out = spans(newestFirst(t(30, "normal")), T0, T0 + 60 * 60, "normal");
    expect(out[0]!.regime).toBe("stressed");
    expect(out[1]!.regime).toBe("normal");
  });

  it("treats a window with no transitions as one span of the current regime", () => {
    const out = spans([], T0, T0 + 60 * 60, "stressed");
    expect(out).toEqual([{ from: T0, to: T0 + 60 * 60, regime: "stressed" }]);
  });

  it("does not paint unknown as calm", () => {
    // A pair we have never evaluated must not render as a reassuring green bar.
    const out = spans([], T0, T0 + 60 * 60, "unknown");
    expect(out[0]!.regime).toBe("unknown");
    expect(stressed([], T0, T0 + 60 * 60, "unknown")).toEqual([]);
  });

  it("ignores transitions outside the window", () => {
    const out = spans(newestFirst(t(-40, "stressed"), t(500, "normal")), T0, T0 + 60 * 60, "stressed");
    expect(out).toHaveLength(1);
    expect(out[0]!.regime).toBe("stressed");
  });

  it("clamps a transition on the window edge rather than emitting a zero-width span", () => {
    // A span of zero seconds becomes a flex-basis of 0% and a marker the user
    // can never hover. Excluding the boundary is cheaper than filtering later.
    const out = spans(newestFirst(t(0, "stressed")), T0, T0 + 60 * 60, "stressed");
    expect(out.every((s) => s.to > s.from)).toBe(true);
  });

  it("returns nothing for an inverted or empty window", () => {
    expect(spans([], T0 + 60, T0, "normal")).toEqual([]);
    expect(spans([], T0, T0, "normal")).toEqual([]);
  });

  it("orders spans by time even though the store holds alerts newest first", () => {
    const out = spans(
      newestFirst(t(10, "stressed"), t(20, "normal"), t(30, "stressed")),
      T0,
      T0 + 60 * 60,
      "stressed",
    );
    expect(out.map((s) => s.regime)).toEqual(["normal", "stressed", "normal", "stressed"]);
  });
});

describe("stressed", () => {
  it("returns only the stressed intervals", () => {
    const out = stressed(
      newestFirst(t(10, "stressed"), t(25, "normal")),
      T0,
      T0 + 60 * 60,
      "normal",
    );
    expect(out).toHaveLength(1);
    expect(out[0]!.from).toBe(T0 + 10 * 60);
    expect(out[0]!.to).toBe(T0 + 25 * 60);
  });

  it("leaves an in-progress event open to the end of the window", () => {
    // The event has not cleared. Closing the span at the last transition would
    // stop shading the chart at the exact moment the user cares most.
    const out = stressed(newestFirst(t(10, "stressed")), T0, T0 + 60 * 60, "stressed");
    expect(out[0]!.to).toBe(T0 + 60 * 60);
  });
});

describe("humanDuration", () => {
  it("rounds to the resolution the data actually has", () => {
    // Bars are one minute wide, so second-level precision would be invented.
    expect(humanDuration(30)).toBe("under a minute");
    expect(humanDuration(60)).toBe("1 minute");
    expect(humanDuration(25 * 60)).toBe("25 minutes");
    expect(humanDuration(60 * 60)).toBe("1 hour");
    expect(humanDuration(150 * 60)).toBe("3 hours");
  });
});
