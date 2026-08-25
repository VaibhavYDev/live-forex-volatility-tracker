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
import { stressed } from "../../lib/regime";
import { useAlerts, useBars, useRegime, useTicks } from "../../lib/stream/hooks";
import type { Bar } from "../../lib/stream/types";
import { useTheme } from "../../lib/theme";

export function PriceChart({ symbol, height = 380 }: { symbol: string; height?: number }) {
  const bars = useBars(symbol);
  const alerts = useAlerts(symbol);
  const regime = useRegime(symbol);
  const { palette } = useTheme();

  const box = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candles = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const provenance = useRef<ISeriesApi<"Histogram"> | null>(null);
  const shade = useRef<ISeriesApi<"Histogram"> | null>(null);
  const live = useRef<Bar | null>(null);
  const [last, setLast] = useState<number | null>(null);

  useEffect(() => {
    if (!box.current) return;

    const c = createChart(box.current, {
      height,
      layout: { background: { color: "transparent" } },
      timeScale: { timeVisible: true, secondsVisible: false },
      crosshair: { mode: 1 },
    });

    // Created FIRST so it paints behind the candles — Lightweight Charts draws
    // series in creation order and v4 has no z-index. A full-height histogram is
    // the idiomatic way to get a vertical span here: the alternative, absolutely
    // positioned divs driven by timeToCoordinate, has to be recomputed on every
    // pan and zoom and drifts by a pixel while the user drags.
    const band = c.addHistogramSeries({
      priceScaleId: "regime",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    band.priceScale().applyOptions({ scaleMargins: { top: 0, bottom: 0 } });

    const series = c.addCandlestickSeries({ borderVisible: false });

    // Provenance, rendered. Backfilled minutes get a marker band so nobody
    // mistakes "we fetched this later" for "this arrived live". A number you
    // cannot trace is a number you cannot trust.
    const src = c.addHistogramSeries({
      priceScaleId: "src",
      priceFormat: { type: "volume" },
      lastValueVisible: false,
      priceLineVisible: false,
    });
    src.priceScale().applyOptions({ scaleMargins: { top: 0.94, bottom: 0 } });

    chart.current = c;
    candles.current = series;
    provenance.current = src;
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
      provenance.current = null;
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
    provenance.current?.applyOptions({ color: palette.warn });
    // Low alpha on purpose: this sits under the candles and must not compete
    // with them. The regime is stated in words elsewhere; here it is context.
    shade.current?.applyOptions({ color: `${palette.warn}22` });
  }, [palette]);

  // Snapshot: replace the whole series in one call rather than N updates.
  useEffect(() => {
    const series = candles.current;
    if (!series || bars.length === 0) return;

    series.setData(
      bars.map((b) => ({ time: b.t as UTCTimestamp, open: b.o, high: b.h, low: b.l, close: b.c })),
    );
    provenance.current?.setData(
      bars.map((b) => ({ time: b.t as UTCTimestamp, value: b.src === "backfill" ? 1 : 0 })),
    );
    live.current = { ...bars[bars.length - 1]! };
    setLast(bars[bars.length - 1]!.c);
    chart.current?.timeScale().fitContent();
  }, [bars]);

  // Markers and shading are both derived from the same `spans()` the timeline
  // strip uses, so a transition cannot land on one boundary here and a different
  // one there.
  useEffect(() => {
    const series = candles.current;
    if (!series || bars.length === 0) return;

    const from = bars[0]!.t;
    const to = bars[bars.length - 1]!.t;
    const hot = stressed(alerts, from, to, regime);

    shade.current?.setData(
      bars.map((b) => ({
        time: b.t as UTCTimestamp,
        value: hot.some((s) => b.t >= s.from && b.t < s.to) ? 1 : 0,
      })),
    );

    series.setMarkers(
      alerts
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
    );
  }, [alerts, bars, regime, palette]);

  // The 60fps path. Called from the store's rAF flush, never from a render.
  useTicks(symbol, (quote) => {
    const series = candles.current;
    if (!series) return;

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
  const dp = symbol.endsWith("JPY") ? 3 : 5;
  const open = bars[0]?.o;
  const move = open && last ? ((last - open) / open) * 100 : null;
  const summary = useLiveSummary(
    last === null
      ? `${symbol} chart, waiting for data.`
      : `${symbol} ${last.toFixed(dp)}, ${move === null ? "" : `${move >= 0 ? "up" : "down"} ${Math.abs(move).toFixed(2)} percent across the window, `}regime ${regime}.`,
    20_000,
  );

  return (
    <>
      <div
        ref={box}
        className="chart"
        role="img"
        aria-label={`${symbol} one-minute candles with regime transition markers`}
      />
      <p className="sr-only" aria-live="polite">
        {summary}
      </p>
    </>
  );
}
