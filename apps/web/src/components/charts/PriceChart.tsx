/**
 * Lightweight Charts wrapper — imperative on purpose.
 *
 * WHY THIS LIBRARY: it is canvas-based and purpose-built for financial series,
 * comfortably handling 100k+ bars. An SVG charting library renders a DOM node per
 * data point and dies well before that.
 *
 * WHY IMPERATIVE: `series.update()` writes straight to the canvas. Routing tick
 * data through React state would put the reconciler on the 60fps path for no
 * benefit — React is excellent at structural change and the wrong tool for a
 * pixel stream. React owns the container and the lifecycle; the chart owns its
 * own pixels.
 */

import {
  createChart,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useCallback, useEffect, useRef, useState } from "react";
import { useLiveSummary } from "../../lib/a11y";
import {
  TF_SECONDS,
  bucketOf,
  bucketStart,
  dayCandle,
  extend,
  sameCandle,
} from "../../lib/candles";
import { BAR_S, stressed } from "../../lib/regime";
import { useAlerts, useRegime, useStore, useTicks } from "../../lib/stream/hooks";
import { useChartBars } from "../../lib/useChartBars";
import { LIVE_TF, TimeframeBar, type Timeframe } from "../TimeframeBar";
import type { Bar } from "../../lib/stream/types";
import { useTheme } from "../../lib/theme";
import type { Palette } from "../../styles/tokens";

/** Below this many bars, stretching them to fill the pane looks like a fault
 *  rather than a young session. Chosen so a ~950px chart still shows candles
 *  narrower than they are tall. */
const FILLS_PANE = 60;
/** Roughly the bar width a full chart settles at, so the transition from
 *  fixed-width to fitted is not a visible jump. */
const COMFORTABLE_BAR_PX = 8;
/** Candles in view when a series first loads; the rest are a scroll away. A
 *  day of minutes fitted into one pane would be under a pixel per candle. */
const SHOWN = 300;
/** Body alpha of the candle still forming: its own colour, visibly unfinished. */
const FORMING_ALPHA = "66";
/** How many of the newest candles are remembered as drawn. */
const PAINTED_KEPT = 4;

/**
 * What the series currently shows, so a change can be drawn as `update()` on
 * the newest candles instead of `setData()` on all of them. Lightweight Charts
 * can only update its newest point, so a change to anything older forces a full
 * redraw — and the live painter never touches a candle older than the newest.
 */
interface View {
  key: string;
  /** `bars` as of the last sync. */
  drawn: readonly Bar[];
  /** Newest time handed to the series, by a sync or by the live painter. */
  lastT: number | null;
  /** The newest few candles as drawn, the forming one included. */
  painted: Map<number, Bar>;
  /** The bucket currently drawn in the forming style. */
  formingAt: number | null;
  fitted: boolean;
  /** Restyles the forming candle when its bucket ends, and when it is due. */
  settle: ReturnType<typeof setTimeout> | null;
  settleAt: number | null;
}

const freshView = (key: string): View => ({
  key,
  drawn: [],
  lastT: null,
  painted: new Map(),
  formingAt: null,
  fitted: false,
  settle: null,
  settleAt: null,
});

function remember(v: View, b: Bar): void {
  v.painted.set(b.t, b);
  if (v.painted.size > PAINTED_KEPT) v.painted.delete(v.painted.keys().next().value!);
  v.lastT = b.t;
}

function point(b: Bar, forming: Palette | null): CandlestickData<UTCTimestamp> {
  const p = { time: b.t as UTCTimestamp, open: b.o, high: b.h, low: b.l, close: b.c };
  if (!forming) return p;
  const solid = b.c >= b.o ? forming.up : forming.down;
  return { ...p, color: `${solid}${FORMING_ALPHA}`, wickColor: solid, borderColor: solid };
}

/**
 * Provenance in words: how much of the window came from where. A candle the
 * stream joined part-way through is neither provider history nor wholly ours,
 * and is counted as what it is.
 */
function provenance(bars: readonly Bar[]): string | null {
  const n = bars.length;
  const recorded = bars.filter((b) => b.src === "backfill").length;
  const mixed = bars.filter((b) => b.src === "mixed").length;
  if (n === 0 || (recorded === 0 && mixed === 0)) return null;
  if (recorded === n) return "provider history";
  const parts: string[] = [];
  if (recorded) parts.push(`${recorded} of ${n} bars from provider history`);
  if (mixed) {
    parts.push(
      recorded
        ? `${mixed} part provider, part live`
        : `${mixed} of ${n} bars part provider, part live`,
    );
  }
  return parts.join(", ");
}

export function PriceChart({ symbol, height = 380 }: { symbol: string; height?: number }) {
  const [tf, setTf] = useState<Timeframe>(LIVE_TF);
  const { bars, loading, error, coverage } = useChartBars(symbol, tf);
  const alerts = useAlerts(symbol);
  const regime = useRegime(symbol);
  const store = useStore();
  const { palette } = useTheme();

  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candles = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const shade = useRef<ISeriesApi<"Area"> | null>(null);
  const view = useRef<View>(freshView(""));
  const [last, setLast] = useState<number | null>(null);
  // The tick painter is registered once and never re-created - putting it on
  // the render path is exactly what this component exists to avoid - so it
  // cannot close over `symbol`, `tf` or the palette. Refs are its read path,
  // assigned during render so they are current before any effect paints.
  const symbolRef = useRef(symbol);
  const tfRef = useRef<Timeframe>(tf);
  const paletteRef = useRef(palette);
  symbolRef.current = symbol;
  tfRef.current = tf;
  paletteRef.current = palette;

  /**
   * The candle still forming, on every timeframe.
   *
   * Built from the store, not from the price: the sealed minutes of its bucket
   * folded together, extended by the minute in progress exactly as the
   * ingestor holds it. A price that conflation dropped still has its high or
   * low in that minute, so no extreme is lost however few frames get painted.
   *
   * Only the newest candle is ever touched. When the minute moves into a new
   * bucket, the candle that was forming is drawn once more, in the plain style
   * and from the minutes it ended with, and never again by this path.
   */
  const paintLive = useCallback(() => {
    const series = candles.current;
    const v = view.current;
    if (!series) return;
    const sym = symbolRef.current;
    const timeframe = tfRef.current;
    if (v.key !== `${sym}|${timeframe}`) return; // the sync has not run for this view yet
    const f = store.forming(sym);
    if (!f) return;

    const seconds = TF_SECONDS[timeframe];
    const start = bucketStart(f.t, seconds);
    if (v.lastT !== null && start < v.lastT) return;

    const minutes = store.minutes(sym);
    const candleAt = (at: number, before?: number, ended = false) =>
      timeframe === "1d"
        ? dayCandle(store.daily(sym), minutes, at, before, ended)
        : bucketOf(minutes, at, seconds, before);

    const draw = (b: Bar, forming: boolean) => {
      series.update(point(b, forming ? paletteRef.current : null));
      remember(v, b);
      v.formingAt = forming ? b.t : v.formingAt === b.t ? null : v.formingAt;
    };

    if (v.lastT !== null && start > v.lastT && v.formingAt === v.lastT) {
      const ended = candleAt(v.lastT, undefined, true) ?? v.painted.get(v.lastT);
      if (ended) draw(ended, false);
    }

    const end = start + seconds;
    const forming = Date.now() < end * 1000;
    draw(extend(candleAt(start, f.t), f, start), forming);

    // A bucket can end with no tick to say so - a quiet minute, the Friday
    // close. Settle its style when it ends rather than whenever data resumes.
    const due = forming ? end : null;
    if (due !== v.settleAt) {
      if (v.settle !== null) clearTimeout(v.settle);
      v.settle = due === null ? null : setTimeout(paintLive, due * 1000 - Date.now() + 50);
      v.settleAt = due;
    }
  }, [store]);

  useEffect(() => {
    if (!box.current) return;

    const c = createChart(box.current, {
      height,
      layout: { background: { color: "transparent" } },
      timeScale: { timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
    });

    // Created FIRST so it paints behind the candles — Lightweight Charts draws
    // series in creation order and v4 has no z-index. Driving this from a series
    // rather than absolutely positioned divs matters because divs placed via
    // timeToCoordinate have to be recomputed on every pan and zoom, and drift by
    // a pixel while the user drags.
    //
    // A stepped AREA, not a histogram.
    //
    // A histogram draws one rectangle per bar with a gap between them. At a
    // handful of bars that reads as a shaded region; at 300 it is a barcode
    // printed over the candles. An area series is continuous, and stepping it
    // keeps the regime boundaries square instead of sloping between bars — the
    // regime changed AT a bar, it did not ramp.
    const band = c.addAreaSeries({
      priceScaleId: "regime",
      lineType: 1, // LineType.WithSteps
      lineWidth: 1,
      lastValueVisible: false,
      priceLineVisible: false,
      crosshairMarkerVisible: false,
    });
    band.priceScale().applyOptions({ scaleMargins: { top: 0, bottom: 0 } });

    const series = c.addCandlestickSeries({ borderVisible: false });

    chart.current = c;
    candles.current = series;
    shade.current = band;
    // A new chart is empty, whatever the last one showed.
    if (view.current.settle !== null) clearTimeout(view.current.settle);
    view.current = freshView("");

    // ResizeObserver rather than a window listener: the chart also has to react
    // to the side panel collapsing, which never fires a window resize.
    const ro = new ResizeObserver(([entry]) => {
      if (entry) c.applyOptions({ width: entry.contentRect.width });
    });
    ro.observe(box.current);

    return () => {
      ro.disconnect();
      if (view.current.settle !== null) clearTimeout(view.current.settle);
      c.remove();
      chart.current = null;
      candles.current = null;
      shade.current = null;
    };
  }, [height]);

  // Recolour in place. Tearing the chart down on a theme toggle would drop the
  // loaded series and the user's zoom, and leave a blank pane until the next
  // snapshot — an expensive way to change four hex codes.
  useEffect(() => {
    chart.current?.applyOptions({
      layout: { textColor: palette.textDim },
      grid: { vertLines: { color: palette.line }, horzLines: { color: palette.line } },
      timeScale: { borderColor: palette.line },
      rightPriceScale: { borderColor: palette.line },
    });
    candles.current?.applyOptions({
      upColor: palette.up,
      downColor: palette.down,
      wickUpColor: palette.up,
      wickDownColor: palette.down,
    });
    // Low alpha on purpose: this sits under the candles and must not compete
    // with them. The regime is stated in words elsewhere; here it is context.
    shade.current?.applyOptions({
      lineColor: "transparent",
      topColor: `${palette.warn}2E`,
      bottomColor: `${palette.warn}2E`,
    });
    // The forming candle carries its colours in its own data point.
    paintLive();
  }, [palette, paintLive]);

  /**
   * Bars into the series, as little at a time as is correct.
   *
   * A new symbol or timeframe replaces the whole series, and only then is the
   * view fitted: refitting on every update, as this once did on every refresh,
   * threw the user's zoom away once a minute. After that a sealed minute
   * usually changes the newest candle or adds one, which is an `update()`. A
   * change to anything older - history arriving behind the snapshot, a sealed
   * minute correcting one the chart has already moved past - redraws in full.
   *
   * The previous pair's candles are replaced even when the new pair has none
   * yet. A series kept across pairs once merged a EURUSD open, high and low
   * with a USDJPY close and drew the result as a candle.
   */
  useEffect(() => {
    const series = candles.current;
    if (!series) return;
    const key = `${symbol}|${tf}`;
    let v = view.current;
    if (v.key !== key) {
      if (v.settle !== null) clearTimeout(v.settle);
      v = view.current = freshView(key);
    }

    let full =
      v.drawn.length === 0 || bars.length < v.drawn.length || bars[0]?.t !== v.drawn[0]?.t;
    const updates: Bar[] = [];
    if (!full) {
      let i = 0;
      while (i < v.drawn.length && sameCandle(bars[i], v.drawn[i])) i++;
      for (let j = i; j < bars.length; j++) {
        const b = bars[j]!;
        if (v.lastT !== null && b.t < v.lastT) {
          if (sameCandle(b, v.painted.get(b.t))) continue;
          full = true;
          break;
        }
        updates.push(b);
      }
    }

    if (full) {
      series.setData(bars.map((b) => point(b, null)));
      v.painted.clear();
      v.lastT = null;
      for (const b of bars.slice(-PAINTED_KEPT)) remember(v, b);
      v.formingAt = null;
    } else {
      for (const b of updates) {
        series.update(point(b, null));
        remember(v, b);
        if (v.formingAt === b.t) v.formingAt = null;
      }
    }
    v.drawn = bars;
    paintLive();

    const newest = bars.at(-1);
    setLast(newest ? newest.c : null);

    // fitContent() spreads whatever it has across the full width, so on a cold
    // start eight bars render as eight enormous slabs - the chart looks broken
    // rather than empty. Below a threshold, pin the bar width instead and let
    // the series grow into the pane from the right, which is what every trading
    // terminal does while a session fills.
    const count = bars.length + (v.lastT !== null && v.lastT > (newest?.t ?? -1) ? 1 : 0);
    if (v.fitted || count === 0) return;
    v.fitted = true;
    const ts = chart.current?.timeScale();
    if (count < FILLS_PANE) ts?.applyOptions({ barSpacing: COMFORTABLE_BAR_PX, rightOffset: 4 });
    else if (count > SHOWN) ts?.setVisibleLogicalRange({ from: count - SHOWN, to: count + 2 });
    else ts?.fitContent();
    // `height` because a new chart starts empty and has to be filled again.
  }, [bars, symbol, tf, paintLive, height]);

  // Markers and shading are both derived from the same `spans()` the timeline
  // strip uses, so a transition cannot land on one boundary here and a different
  // one there.
  useEffect(() => {
    const series = candles.current;
    if (!series) return;
    if (bars.length === 0) {
      shade.current?.setData([]);
      series.setMarkers([]);
      return;
    }

    const from = bars[0]!.t;
    // Through the END of the last bar; see BAR_S in lib/regime.
    const to = bars[bars.length - 1]!.t + BAR_S;
    const hot = stressed(alerts, from, to, regime);

    // Whitespace, not zero, outside a stressed stretch.
    //
    // A histogram draws nothing at value 0; an area fills all the way down to
    // its baseline, so a calm chart came out with a solid block across the
    // lower half. A point carrying only `time` is whitespace to Lightweight
    // Charts: it holds the slot on the axis and paints nothing.
    shade.current?.setData(
      bars.map((b) =>
        hot.some((s) => b.t >= s.from && b.t < s.to)
          ? { time: b.t as UTCTimestamp, value: 1 }
          : { time: b.t as UTCTimestamp },
      ),
    );

    // Provenance as ONE marker, not a band.
    //
    // The band this replaces painted every backfilled bar amber. That was
    // legible when a handful of minutes were backfilled; with days of provider
    // history behind a young feed it becomes a solid block across the whole
    // chart, which distinguishes nothing and buries the candles.
    //
    // The information anyone actually wants is the BOUNDARY: where does the
    // provider's recorded history end and bars built from our own ticks begin.
    // One marker says that exactly, and says nothing when there is nothing to
    // say. A "mixed" candle is where the stream joined, so it is the boundary
    // even when it is the first candle in view.
    const firstLive = bars.findIndex((b) => b.src !== "backfill");
    const boundary =
      firstLive > 0 || bars[firstLive]?.src === "mixed"
        ? [
            {
              time: bars[firstLive]!.t as UTCTimestamp,
              position: "belowBar" as const,
              color: palette.textDim,
              shape: "circle" as const,
              text: "live from here",
            },
          ]
        : [];

    series.setMarkers(
      [
        ...boundary,
        ...alerts
          .filter((a) => {
            const at = Math.floor(new Date(a.ts).getTime() / 1000);
            return at >= from && at <= to;
          })
          // Oldest first: the library requires markers in ascending time and
          // silently misplaces them otherwise, while the store holds them newest
          // first for the alert list.
          .sort((a, b) => Date.parse(a.ts) - Date.parse(b.ts))
          .map((a) => {
            const escalation = a.new_regime === "stressed";
            return {
              time: (Math.floor(Date.parse(a.ts) / 60_000) * 60) as UTCTimestamp,
              position: escalation ? ("aboveBar" as const) : ("belowBar" as const),
              color: escalation ? palette.warn : palette.up,
              // Shape as well as colour, for the same reason the badge has one.
              shape: escalation ? ("arrowDown" as const) : ("arrowUp" as const),
              text: escalation ? "stress" : "clear",
            };
          }),
      ]
        // The library requires ascending time across the WHOLE list and
        // silently misplaces markers otherwise, so the boundary has to be
        // sorted in with the alerts rather than merely prepended.
        .sort((a, b) => (a.time as number) - (b.time as number)),
    );
  }, [alerts, bars, regime, palette]);

  // The 60fps path. Called from the store's rAF flush, never from a render.
  useTicks(symbol, paintLive);

  // Deliberately NOT fed from the tick callback: this is the one value the
  // screen-reader summary reads, and it is throttled hard downstream. Sampling
  // it once per sealed minute keeps React off the 50/sec path entirely.
  // Provenance in words. The marker shows WHERE the boundary is; this says HOW
  // MUCH of what you are looking at came from provider history rather than our
  // own stream, which is the part that belongs in text rather than in pixels.
  const recorded = bars.filter((b) => b.src === "backfill").length;
  const origin = provenance(bars);
  // A day is partial when we hold no bar for some minute of it the market was
  // open: its high and low are then only what was observed, not the day's.
  const partial =
    coverage !== null && coverage.expected > 0 && coverage.observed < coverage.expected
      ? `Today partial: ${coverage.observed} of ${coverage.expected} minutes observed`
      : null;

  const dp = symbol.endsWith("JPY") ? 3 : 5;
  const open = bars[0]?.o;
  const move = open && last ? ((last - open) / open) * 100 : null;
  const summary = useLiveSummary(
    last === null
      ? `${symbol} chart, waiting for data.`
      : `${symbol} ${last.toFixed(dp)}, ${move === null ? "" : `${move >= 0 ? "up" : "down"} ${Math.abs(move).toFixed(2)} percent across the window, `}regime ${regime}.${recorded ? ` ${recorded} of ${bars.length} bars are provider history.` : ""}${partial ? ` ${partial}.` : ""}`,
    20_000,
  );

  return (
    <>
      <div className="chart__bar">
        <TimeframeBar value={tf} onChange={setTf} />
        {/* Status, not decoration: an empty pane on a fetch failure is
            indistinguishable from a market with no history. */}
        {error && (
          <span className="chart__note chart__note--bad" role="status">
            Could not load {tf} history
          </span>
        )}
        {loading && !error && (
          <span className="chart__note" role="status">
            Loading {tf}…
          </span>
        )}
        {bars.length === 0 && !loading && !error && (
          <span className="chart__note" role="status">
            Waiting for {symbol} history…
          </span>
        )}
        {origin && !loading && <span className="chart__note">{origin}</span>}
        {partial && <span className="chart__note">{partial}</span>}
      </div>
      <div
        ref={box}
        className="chart"
        role="img"
        aria-label={`${symbol} ${tf} candles with regime transition markers`}
      />
      <p className="sr-only" aria-live="polite">
        {summary}
      </p>
    </>
  );
}
