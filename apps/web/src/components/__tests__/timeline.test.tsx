import { act, render, screen } from "@testing-library/react";
import { axe } from "jest-axe";
import { beforeEach, describe, expect, it } from "vitest";
import { StoreCtx } from "../../lib/stream/hooks";
import { MarketStore } from "../../lib/stream/store";
import type { Bar, Transition } from "../../lib/stream/types";
import { RegimeTimeline } from "../RegimeTimeline";

const T0 = 1_787_000_000;

let store: MarketStore;
beforeEach(() => {
  store = new MarketStore();
});

const bars = (n: number): Bar[] =>
  Array.from({ length: n }, (_, i) => ({
    t: T0 + i * 60,
    o: 1.09,
    h: 1.09,
    l: 1.09,
    c: 1.09,
    n: 10,
    src: "stream",
  }));

const t = (min: number, to: "normal" | "stressed"): Transition => ({
  s: "EURUSD",
  seq: min,
  ts: new Date((T0 + min * 60) * 1000).toISOString(),
  old_regime: to === "stressed" ? "normal" : "stressed",
  new_regime: to,
  trigger_value: 3.4,
  threshold_value: 3,
  sigma: 0.0004,
  cause: "threshold",
  reason: "held",
});

const mount = () =>
  render(
    <StoreCtx.Provider value={store}>
      <RegimeTimeline symbol="EURUSD" />
    </StoreCtx.Provider>,
  );

const segments = () => [...document.querySelectorAll<HTMLElement>(".strip__seg")];

describe("RegimeTimeline", () => {
  it("renders nothing before there are bars to anchor to", () => {
    // A strip with no time axis would have to invent one, and "the last four
    // hours" invented locally drifts away from the chart above it after a gap.
    const { container } = mount();
    expect(container).toBeEmptyDOMElement();
  });

  it("spans the same minutes as the chart, not a wall-clock window", () => {
    act(() => {
      store.setBars({ EURUSD: bars(121) });
      store.pushAlert(t(30, "stressed"));
      store.pushAlert(t(90, "normal"));
    });
    mount();

    expect(segments()).toHaveLength(3);
    // 30 of 120 minutes, 60 of 120, 30 of 120.
    const widths = segments().map((s) => parseFloat(s.style.flexBasis));
    expect(widths[0]).toBeCloseTo(25, 1);
    expect(widths[1]).toBeCloseTo(50, 1);
    expect(widths[2]).toBeCloseTo(25, 1);
    expect(widths.reduce((a, b) => a + b, 0)).toBeCloseTo(100, 1);
  });

  it("totals the stressed time rather than making the reader measure a bar", () => {
    act(() => {
      store.setBars({ EURUSD: bars(121) });
      store.pushAlert(t(30, "stressed"));
      store.pushAlert(t(90, "normal"));
    });
    mount();
    // Scoped to the headline stat: the sr-only equivalent below it says the same
    // thing on purpose, and an unscoped query would match both.
    expect(screen.getByText(/1 hour stressed/, { selector: ".strip__stat" })).toBeInTheDocument();
  });

  it("says so plainly when nothing happened", () => {
    act(() => {
      store.setBars({ EURUSD: bars(61) });
      store.seedRegime("EURUSD", "normal");
    });
    mount();
    expect(screen.getByText(/calm throughout/)).toBeInTheDocument();
  });

  it("does not render an unknown regime as calm", () => {
    act(() => store.setBars({ EURUSD: bars(61) }));
    mount();
    expect(segments()[0]).toHaveAttribute("data-regime", "unknown");
  });

  it("keeps an in-progress event open to the right edge", () => {
    act(() => {
      store.setBars({ EURUSD: bars(121) });
      store.pushAlert(t(60, "stressed"));
    });
    mount();

    const last = segments()[segments().length - 1]!;
    expect(last).toHaveAttribute("data-regime", "stressed");
  });

  it("carries a text equivalent instead of announcing twelve segments one by one", () => {
    act(() => {
      store.setBars({ EURUSD: bars(121) });
      store.pushAlert(t(30, "stressed"));
      store.pushAlert(t(90, "normal"));
    });
    const { container } = mount();

    expect(container.querySelector(".strip__track")).toHaveAttribute("aria-hidden", "true");
    expect(container.querySelector(".sr-only")).toHaveTextContent(
      "30 minutes normal, then 1 hour stressed, then 30 minutes normal.",
    );
  });

  it("passes axe", async () => {
    act(() => {
      store.setBars({ EURUSD: bars(121) });
      store.pushAlert(t(30, "stressed"));
    });
    const { container } = mount();
    expect(await axe(container)).toHaveNoViolations();
  });
});
