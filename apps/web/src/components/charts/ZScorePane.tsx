import { useEffect, useMemo, useRef } from "react";
import { useLiveSummary } from "../../lib/a11y";
import { useBars, useVol, useZHist } from "../../lib/stream/hooks";
import type { ZPoint } from "../../lib/stream/types";
import { useTheme } from "../../lib/theme";

/**
 * How unusual is now, against this pair's own history.
 *
 * The band is a positioned div rather than two lines painted on the canvas, and
 * that is a deliberate split of responsibilities: the thresholds change roughly
 * never, the series changes every minute. Keeping the static geometry in the DOM
 * means a redraw repaints only the line, the band gets CSS transitions for free
 * when a threshold does change, and the two levels stay selectable text for
 * anyone poking at the page.
 *
 * There is no rAF here on purpose. A z arrives once per sealed bar — once a
 * minute — so this is structural change, and putting it on the tick path would
 * be paying 60fps for a 1/60Hz signal.
 */

const H = 132;
const PAD = 6;
const BAR_S = 60;
/** Two bars apart means a minute we could not evaluate. Break the line there. */
const GAP_S = BAR_S * 2;

interface Scale {
  lo: number;
  hi: number;
}

/**
 * Quantised to whole z units. An exactly-fitted domain would rescale on every
 * new point, so the band — positioned as a percentage of that domain — would
 * slide a pixel or two every minute. A threshold line that drifts is a threshold
 * line nobody believes.
 */
function scaleFor(points: readonly ZPoint[], enter?: number): Scale {
  let lo = -1;
  let hi = Math.max(enter ?? 3, 3);
  for (const p of points) {
    if (p.z < lo) lo = p.z;
    if (p.z > hi) hi = p.z;
  }
  return { lo: Math.floor(lo) - 1, hi: Math.ceil(hi) + 1 };
}

const pct = (v: number, s: Scale) => ((s.hi - v) / (s.hi - s.lo)) * 100;

export function ZScorePane({ symbol }: { symbol: string }) {
  const points = useZHist(symbol);
  const bars = useBars(symbol);
  const vol = useVol(symbol);
  const { palette } = useTheme();
  const canvas = useRef<HTMLCanvasElement>(null);
  const box = useRef<HTMLDivElement>(null);

  const enter = vol?.enterZ;
  const exit = vol?.exitZ;
  const scale = useMemo(() => scaleFor(points, enter), [points, enter]);

  useEffect(() => {
    const el = canvas.current;
    const parent = box.current;
    if (!el || !parent) return;

    const draw = () => {
      const w = parent.clientWidth;
      if (w === 0) return;

      // Backing store in device pixels, CSS box in logical ones. Skipping this
      // is why most hand-rolled canvas charts look soft on a laptop.
      const dpr = window.devicePixelRatio || 1;
      el.width = Math.round(w * dpr);
      el.height = Math.round(H * dpr);
      el.style.width = `${w}px`;
      el.style.height = `${H}px`;

      const ctx = el.getContext("2d");
      if (!ctx) return;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, H);

      const x = (i: number) =>
        points.length < 2 ? w - PAD : PAD + (i / (points.length - 1)) * (w - PAD * 2);
      const y = (z: number) => PAD + ((scale.hi - z) / (scale.hi - scale.lo)) * (H - PAD * 2);

      ctx.strokeStyle = palette.line;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(PAD, y(0));
      ctx.lineTo(w - PAD, y(0));
      ctx.stroke();

      if (points.length === 0) return;

      // Segmented by committed regime, and broken across gaps. Drawing one
      // continuous path would join across the minutes we were blind and imply
      // a measurement that was never taken.
      ctx.lineWidth = 1.75;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";

      let i = 0;
      while (i < points.length) {
        const from = points[i]!;
        let j = i;
        while (
          j + 1 < points.length &&
          points[j + 1]!.r === from.r &&
          points[j + 1]!.t - points[j]!.t <= GAP_S
        ) {
          j++;
        }
        ctx.strokeStyle = from.r === "stressed" ? palette.warn : palette.accent;
        ctx.beginPath();
        ctx.moveTo(x(i), y(from.z));
        for (let k = i + 1; k <= j; k++) ctx.lineTo(x(k), y(points[k]!.z));
        // A run of one is a point between two holes; a zero-length path draws
        // nothing, so give it a dot rather than losing the sample.
        if (j === i) ctx.lineTo(x(i) + 0.01, y(from.z));
        ctx.stroke();
        i = j + 1;
      }

      const last = points[points.length - 1]!;
      ctx.fillStyle = last.r === "stressed" ? palette.warn : palette.accent;
      ctx.beginPath();
      ctx.arc(x(points.length - 1), y(last.z), 2.5, 0, Math.PI * 2);
      ctx.fill();
    };

    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(parent);
    return () => ro.disconnect();
  }, [points, scale, palette]);

  const summary = useLiveSummary(describe(symbol, points, enter, exit), 30_000);
  const banded = enter !== undefined && exit !== undefined;

  return (
    <section className="zpane" aria-labelledby="zpane-h">
      <div className="zpane__head">
        <h2 id="zpane-h">Z-score vs. trailing baseline</h2>
        {banded && (
          <span className="zpane__legend">
            escalate ≥ {enter} · clear ≤ {exit}
          </span>
        )}
      </div>

      <div className="zpane__plot" ref={box} style={{ height: H }}>
        {banded && (
          <>
            {/* The hysteresis band: the gap between the two levels IS the
                mechanism. Drawn as one shape rather than two lines because the
                interesting thing is the width of the dead zone, not either
                edge on its own. */}
            <div
              className="zband"
              style={{ top: `${pct(enter, scale)}%`, height: `${pct(exit, scale) - pct(enter, scale)}%` }}
              aria-hidden
            />
            <div className="zband__edge zband__edge--enter" style={{ top: `${pct(enter, scale)}%` }} aria-hidden />
            <div className="zband__edge zband__edge--exit" style={{ top: `${pct(exit, scale)}%` }} aria-hidden />
          </>
        )}
        <canvas ref={canvas} role="img" aria-label={describe(symbol, points, enter, exit)} />
        {points.length === 0 && <p className="zpane__empty">{warmupNote(bars)}</p>}
      </div>

      <p className="sr-only" aria-live="polite">
        {summary}
      </p>
    </section>
  );
}

/** WARM_UP_BARS mirrors the detector: `session_warmup` is 30 minutes and
 *  `rearm_bars` is 3, so no z-score exists until roughly the 33rd sealed bar.
 *  See packages/core/fx_core/alerts/detector.py. */
const WARM_UP_BARS = 33;

/**
 * The empty state used to read "Waiting for the first sealed bar", which is
 * false the moment a single candle is on screen directly above it - and it is
 * on screen within a minute. Saying something demonstrably untrue about the
 * system's own state is worse than saying nothing, because the reader stops
 * trusting the rest of the panel.
 *
 * The z-score is genuinely absent, but for a reason worth stating: a z-score is
 * a distance from a baseline, and the baseline has not been estimated yet.
 */
function warmupNote(bars: readonly { t: number }[]): string {
  if (bars.length === 0) return "No sealed bars yet.";
  const left = WARM_UP_BARS - bars.length;
  if (left <= 0) return "Baseline ready — waiting for the next sealed bar.";
  return `Estimating the baseline — ${left} more ${left === 1 ? "bar" : "bars"}.`;
}

function describe(
  symbol: string,
  points: readonly ZPoint[],
  enter?: number,
  exit?: number,
): string {
  if (points.length === 0) return `${symbol} z-score chart, no readings yet.`;

  const last = points[points.length - 1]!;
  const window = points.slice(-30);
  const first = window[0]!;
  const trend = last.z > first.z + 0.3 ? "rising" : last.z < first.z - 0.3 ? "falling" : "flat";
  const band =
    enter !== undefined && exit !== undefined
      ? ` Escalation threshold ${enter}, clear threshold ${exit}.`
      : "";

  return (
    `${symbol} z-score ${last.z.toFixed(2)}, ${trend} over the last ${window.length} bars, ` +
    `regime ${last.r}.${band}`
  );
}
