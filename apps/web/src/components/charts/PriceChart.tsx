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

import { createChart, type IChartApi, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { useLiveSummary } from "../../lib/a11y";
import { BAR_S, stressed } from "../../lib/regime";
import { useAlerts, useRegime, useTicks } from "../../lib/stream/hooks";
import { useChartBars } from "../../lib/useChartBars";
import { LIVE_TF, TimeframeBar, type Timeframe } from "../TimeframeBar";
import type { Bar } from "../../lib/stream/types";
import { useTheme } from "../../lib/theme";

/** Below this many bars, stretching them to fill the pane looks like a fault
 *  rather than a young session. Chosen so a ~950px chart still shows candles
 *  narrower than they are tall. */
const FILLS_PANE = 60;
/** Roughly the bar width a full chart settles at, so the transition from
 *  fixed-width to fitted is not a visible jump. */
const COMFORTABLE_BAR_PX = 8;

export function PriceChart({ symbol, height = 380 }: { symbol: string; height?: number }) {
  const [tf, setTf] = useState<Timeframe>(LIVE_TF);
  const { bars, loading, error } = useChartBars(symbol, tf);
  const alerts = useAlerts(symbol);
  const regime = useRegime(symbol);
  const { palette } = useTheme();

  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candles = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const shade = useRef<ISeriesApi<"Area"> | null>(null);
  const live = useRef<Bar | null>(null);
  const [last, setLast] = useState<number | null>(null);
  // The tick callback is registered once and never re-created - putting it on
  // the render path is exactly what this component exists to avoid - so it
  // cannot close over `tf`. A ref is the read path for state the 60fps handler
  // needs.
  const tfRef = useRef<Timeframe>(LIVE_TF);
  useEffect(() => {
    tfRef.current = tf;
  }, [tf]);

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

    // ResizeObserver rather than a window listener: the chart also has to react
    // to the side panel collapsing, which never fires a window resize.
    const ro = new ResizeObserver(([entry]) => {
      if (entry) c.applyOptions({ width: entry.contentRect.width });
    });
    ro.observe(box.current);

    return () => {
      ro.disconnect();
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
  }, [palette]);

  /**
   * The in-flight bar belongs to one symbol. Without this reset, switching to a
   * pair that has no cached history yet left the previous pair's bar in place —
   * the snapshot effect below returns early on an empty series and never clears
   * it — so the next tick merged a EURUSD open, high and low with a USDJPY close
   * and drew the result as a candle. Wrong data, rendered confidently.
   */
  useEffect(() => {
    live.current = null;
    setLast(null);
  }, [symbol]);

  // Snapshot: replace the whole series in one call rather than N updates.
  useEffect(() => {
    const series = candles.current;
    if (!series || bars.length === 0) return;

    series.setData(
      bars.map((b) => ({ time: b.t as UTCTimestamp, open: b.o, high: b.h, low: b.l, close: b.c })),
    );
    live.current = { ...bars[bars.length - 1]! };
    setLast(bars[bars.length - 1]!.c);

    // fitContent() spreads whatever it has across the full width, so on a cold
    // start eight bars render as eight enormous slabs - the chart looks broken
    // rather than empty. Below a threshold, pin the bar width instead and let
    // the series grow into the pane from the right, which is what every trading
    // terminal does while a session fills.
    const ts = chart.current?.timeScale();
    if (bars.length >= FILLS_PANE) ts?.fitContent();
    else ts?.applyOptions({ barSpacing: COMFORTABLE_BAR_PX, rightOffset: 4 });
  }, [bars]);

  // Markers and shading are both derived from the same `spans()` the timeline
  // strip uses, so a transition cannot land on one boundary here and a different
  // one there.
  useEffect(() => {
    const series = candles.current;
    if (!series || bars.length === 0) return;

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
    // The band this replaces painted every synthesised bar amber. That was
    // legible when a handful of minutes were backfilled; with 30 days of
    // synthesised history behind a young feed it becomes a solid block across
    // the whole chart, which distinguishes nothing and buries the candles.
    //
    // The information anyone actually wants is the BOUNDARY: where does the
    // synthesised past end and observed data begin. One marker says that
    // exactly, and says nothing when there is nothing to say.
    const firstLive = bars.findIndex((b) => b.src !== "backfill");
    const boundary =
      firstLive > 0
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
  useTicks(symbol, (quote) => {
    const series = candles.current;
    if (!series) return;
    // Only the 1-minute view owns a live candle. On coarser timeframes the
    // trailing bucket is folded server-side, so merging a tick into it here
    // would draw a bar the server never agreed to and that the next refresh
    // silently replaces.
    if (tfRef.current !== LIVE_TF) return;

    const minute = (Math.floor(new Date(quote.ts).getTime() / 60_000) * 60) as UTCTimestamp;
    const bar = live.current;

    if (!bar || minute > bar.t) {
      live.current = {
        t: minute,
        o: quote.mid,
        h: quote.mid,
        l: quote.mid,
        c: quote.mid,
        n: 1,
        src: "stream",
      };
    } else {
      // Mutating the in-flight bar is safe precisely because it never crosses
      // into React — nothing is diffing this object.
      live.current = {
        ...bar,
        h: Math.max(bar.h, quote.mid),
        l: Math.min(bar.l, quote.mid),
        c: quote.mid,
        n: bar.n + 1,
      };
    }

    const b = live.current;
    series.update({ time: b.t as UTCTimestamp, open: b.o, high: b.h, low: b.l, close: b.c });
  });

  // Deliberately NOT fed from the tick callback: this is the one value the
  // screen-reader summary reads, and it is throttled hard downstream. Sampling
  // it once per snapshot keeps React off the 50/sec path entirely.
  // Provenance in words. The marker shows WHERE the boundary is; this says HOW
  // MUCH of what you are looking at was synthesised, which is the part that
  // belongs in text rather than in pixels.
  const synthetic = bars.filter((b) => b.src === "backfill").length;

  const dp = symbol.endsWith("JPY") ? 3 : 5;
  const open = bars[0]?.o;
  const move = open && last ? ((last - open) / open) * 100 : null;
  const summary = useLiveSummary(
    last === null
      ? `${symbol} chart, waiting for data.`
      : `${symbol} ${last.toFixed(dp)}, ${move === null ? "" : `${move >= 0 ? "up" : "down"} ${Math.abs(move).toFixed(2)} percent across the window, `}regime ${regime}.${synthetic ? ` ${synthetic} of ${bars.length} bars are synthesised history.` : ""}`,
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
        {synthetic > 0 && !loading && (
          <span className="chart__note">
            {synthetic === bars.length
              ? "synthesised history"
              : `${synthetic} of ${bars.length} bars synthesised`}
          </span>
        )}
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
