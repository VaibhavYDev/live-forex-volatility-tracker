import { act, render, screen } from "@testing-library/react";
import { axe } from "jest-axe";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { StoreCtx } from "../../lib/stream/hooks";
import { MarketStore } from "../../lib/stream/store";
import type { Bar, Vol, ZPoint } from "../../lib/stream/types";
import { ThemeProvider } from "../../lib/theme";
import { stubCanvas, stubWidth, type Recorder } from "../../test/canvas";
import { ZScorePane } from "../charts/ZScorePane";

const T0 = 1_787_000_000; // an arbitrary minute boundary
const W = 400;

let store: MarketStore;
let canvas: Recorder;

beforeEach(() => {
  store = new MarketStore();
  canvas = stubCanvas();
  stubWidth(W);
});

const z = (i: number, value: number, r: "normal" | "stressed" = "normal"): ZPoint => ({
  t: T0 + i * 60,
  z: value,
  r,
});

const vol = (enterZ?: number, exitZ?: number): Vol => ({
  symbol: "EURUSD",
  sigma: 0.0004,
  sigmaAnn: 0.08,
  z: 1.1,
  regime: "normal",
  warm: true,
  ts: "2026-08-22T12:00:00Z",
  enterZ,
  exitZ,
});

const mount = (ui: ReactNode) =>
  render(
    <ThemeProvider>
      <StoreCtx.Provider value={store}>{ui}</StoreCtx.Provider>
    </ThemeProvider>,
  );

const band = () => document.querySelector<HTMLElement>(".zband");
/** A sealed OHLC bar. The pane only reads `t`, but the store wants the shape. */
const bar = (t: number): Bar => ({ t, o: 1.1, h: 1.1, l: 1.1, c: 1.1, n: 1, src: "replay" });

const seed = (points: ZPoint[], v?: Vol, bars?: Bar[]) =>
  act(() => {
    store.setZHist("EURUSD", points);
    if (v) store.setVol(v);
    if (bars) store.setBars({ EURUSD: bars });
  });

describe("the hysteresis band", () => {
  it("is not drawn until the thresholds are known", () => {
    // Better an axis with no band than a band at values the running detector
    // does not actually use.
    seed([z(0, 1), z(1, 2)]);
    mount(<ZScorePane symbol="EURUSD" />);
    expect(band()).toBeNull();
  });

  it("spans exit_z to enter_z, not a single line at the threshold", () => {
    // The dead zone IS the mechanism. A single line would show the escalation
    // level and hide the reason an alert does not immediately un-fire.
    seed([z(0, 0), z(1, 1)], vol(3, 1.5));
    mount(<ZScorePane symbol="EURUSD" />);

    // Domain quantises to [-2, 4]: floor(min(-1, 0)) - 1 .. ceil(max(3, 1)) + 1.
    // enter 3 sits at (4-3)/6 = 16.67% from the top, exit 1.5 at (4-1.5)/6 = 41.67%.
    const el = band()!;
    expect(parseFloat(el.style.top)).toBeCloseTo(16.667, 2);
    expect(parseFloat(el.style.height)).toBeCloseTo(25, 2);
  });

  it("holds still when a new point does not widen the domain", () => {
    // The band is positioned as a percentage of the domain, so an exactly-fitted
    // domain would slide it a pixel or two every single minute. A threshold line
    // that drifts is a threshold line nobody believes.
    //
    // 3.6 then 3.9 is the discriminating pair: the raw maximum moves, so an
    // unquantised scale WOULD shift the band, while both land inside the same
    // rounded ceiling. A gentler second point passes either way and proves
    // nothing.
    seed([z(0, 3.6), z(1, 3.2)], vol(3, 1.5));
    mount(<ZScorePane symbol="EURUSD" />);
    const before = band()!.style.top;
    expect(parseFloat(before)).toBeCloseTo(28.571, 2); // domain [-2, 5]

    act(() => store.pushZ("EURUSD", z(2, 3.9)));
    expect(band()!.style.top).toBe(before);
  });

  it("rescales when a reading goes outside the domain", () => {
    seed([z(0, 0), z(1, 1)], vol(3, 1.5));
    mount(<ZScorePane symbol="EURUSD" />);
    const before = parseFloat(band()!.style.top);

    act(() => store.pushZ("EURUSD", z(2, 9)));
    expect(parseFloat(band()!.style.top)).toBeGreaterThan(before);
  });
});

describe("the series", () => {
  it("breaks the line where a minute could not be measured", () => {
    // Joining across the hole would draw a straight confident line through the
    // exact window in which we were blind.
    seed([z(0, 1), z(1, 1.2), z(5, 1.4), z(6, 1.5)], vol(3, 1.5));
    mount(<ZScorePane symbol="EURUSD" />);

    // One path for the zero line, then one per contiguous run.
    const runs = canvas.paths().filter((p) => p.points.length > 1);
    expect(runs.length).toBeGreaterThanOrEqual(3);
  });

  it("changes colour at a regime boundary rather than drawing one flat line", () => {
    seed(
      [z(0, 1, "normal"), z(1, 1.2, "normal"), z(2, 3.4, "stressed"), z(3, 3.6, "stressed")],
      vol(3, 1.5),
    );
    mount(<ZScorePane symbol="EURUSD" />);

    const strokes = new Set(canvas.paths().map((p) => p.stroke));
    expect(strokes.size).toBeGreaterThanOrEqual(3); // zero line + two regimes
  });

  it("still plots a lone sample stranded between two holes", () => {
    // A one-point run is a zero-length path, which strokes nothing. Losing the
    // only reading either side of an outage is the worst time to lose one.
    seed([z(0, 1), z(10, 2.2), z(20, 1)], vol(3, 1.5));
    mount(<ZScorePane symbol="EURUSD" />);

    const drawn = canvas.ops.filter((o) => o.op === "stroke");
    expect(drawn.length).toBeGreaterThanOrEqual(4); // zero line + three singles
  });

  it("renders an axis and says so when there is nothing to plot", () => {
    mount(<ZScorePane symbol="EURUSD" />);
    expect(screen.getByText(/no sealed bars yet/i)).toBeInTheDocument();
  });

  it("does not claim to be waiting for a bar while bars are on screen", () => {
    // The old copy said "Waiting for the first sealed bar" regardless, which is
    // visibly false with candles rendered directly above it. A z-score is
    // missing here because the BASELINE is not estimated yet, not because no
    // bar has sealed, and the panel has to say the true thing.
    seed([], vol(3, 1.5), [bar(0), bar(60), bar(120)]);
    mount(<ZScorePane symbol="EURUSD" />);

    expect(screen.queryByText(/waiting for the first sealed bar/i)).not.toBeInTheDocument();
    expect(screen.getByText(/estimating the baseline/i)).toBeInTheDocument();
  });

  it("counts down the bars left in the warm-up", () => {
    // 33 bars to a baseline; 3 seen means 30 to go. A progressing number is the
    // difference between "warming up" and "stuck".
    seed([], vol(3, 1.5), [bar(0), bar(60), bar(120)]);
    mount(<ZScorePane symbol="EURUSD" />);
    expect(screen.getByText(/30 more bars/i)).toBeInTheDocument();
  });
});

describe("accessibility", () => {
  it("describes the canvas rather than leaving it an unlabelled image", () => {
    seed([z(0, 0.4), z(1, 2.9)], vol(3, 1.5));
    mount(<ZScorePane symbol="EURUSD" />);

    expect(screen.getByRole("img")).toHaveAccessibleName(
      /EURUSD z-score 2\.90.*rising.*regime normal.*threshold 3/i,
    );
  });

  it("states both thresholds in text, not only as a shaded rectangle", () => {
    seed([z(0, 1)], vol(3, 1.5));
    mount(<ZScorePane symbol="EURUSD" />);
    expect(screen.getByText(/escalate ≥ 3.*clear ≤ 1\.5/)).toBeInTheDocument();
  });

  it("passes axe", async () => {
    seed([z(0, 1), z(1, 2)], vol(3, 1.5));
    const { container } = mount(<ZScorePane symbol="EURUSD" />);
    expect(await axe(container)).toHaveNoViolations();
  });
});
