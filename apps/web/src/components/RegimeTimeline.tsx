import { useMemo } from "react";
import { useAlerts, useBars, useRegime } from "../lib/stream/hooks";
import { BAR_S, humanDuration, spans } from "../lib/regime";

/**
 * The last N minutes as one bar: where this pair was calm, where it was not.
 *
 * The price chart answers "what happened", this answers "when were we worried",
 * and the two share `spans()` so their boundaries cannot disagree. It is
 * deliberately not a chart — no axis, no hover crosshair — because the only
 * question it exists to answer is glanceable.
 *
 * Segments are flex-basis percentages rather than absolute pixels so the strip
 * reflows with the panel without any measurement or a resize listener.
 */
export function RegimeTimeline({ symbol }: { symbol: string }) {
  const alerts = useAlerts(symbol);
  const bars = useBars(symbol);
  const regime = useRegime(symbol);

  const { segments, from, to } = useMemo(() => {
    const first = bars[0]?.t;
    const last = bars[bars.length - 1]?.t;
    if (first === undefined || last === undefined || last <= first) {
      return { segments: [], from: 0, to: 0 };
    }
    // Anchored to the bars actually on screen, so the strip and the chart above
    // it cover the same minutes. Using "now minus four hours" instead would
    // drift apart from the chart the moment the feed had a gap.
    const end = last + BAR_S; // through the end of the last bar, not its stamp
    return { segments: spans(alerts, first, end, regime), from: first, to: end };
  }, [alerts, bars, regime]);

  if (segments.length === 0) return null;

  const total = to - from;
  const stressedFor = segments
    .filter((s) => s.regime === "stressed")
    .reduce((acc, s) => acc + (s.to - s.from), 0);

  return (
    <section className="strip" aria-labelledby="strip-h">
      <div className="strip__head">
        <h2 id="strip-h">Regime, last {humanDuration(total)}</h2>
        <span className="strip__stat">
          {stressedFor === 0 ? "calm throughout" : `${humanDuration(stressedFor)} stressed`}
        </span>
      </div>

      {/* The visual is decoration over the sentence below it, which is why the
          track is aria-hidden rather than carrying a pile of ARIA that would
          announce twelve segments one at a time. */}
      <div className="strip__track" aria-hidden>
        {segments.map((s) => (
          <span
            key={s.from}
            className="strip__seg"
            data-regime={s.regime}
            style={{ flexBasis: `${((s.to - s.from) / total) * 100}%` }}
            title={`${s.regime} · ${humanDuration(s.to - s.from)}`}
          />
        ))}
      </div>

      <p className="sr-only">
        {segments
          .map((s) => `${humanDuration(s.to - s.from)} ${s.regime}`)
          .join(", then ")}
        .
      </p>
    </section>
  );
}
