/**
 * Timeframe selector.
 *
 * A toolbar, not a tablist. The ARIA APG reserves `tab`/`tabpanel` for controls
 * that swap which panel is shown; these swap the RESOLUTION of one panel that
 * never goes away. Announcing "tab 7 of 9" for that misdescribes the page, so
 * this is a group of toggle buttons carrying `aria-pressed`, which is what a
 * screen reader user actually needs to hear: which one is on.
 *
 * Roving tabindex rather than seven tab stops. Seven controls between the symbol
 * tabs and the chart is a long walk for a keyboard user who wants neither.
 */

import { useRef, type KeyboardEvent } from "react";

/** Mirrors fx_core.intervals.ORDER. A code the API does not know renders a
 *  button that 422s, so tests/unit/test_intervals.py pins the server side and
 *  the endpoint rejects anything unrecognised rather than quietly serving 1m. */
export const TIMEFRAMES = ["1m", "3m", "5m", "10m", "15m", "30m", "1d"] as const;
export type Timeframe = (typeof TIMEFRAMES)[number];

/** Only 1-minute has a live in-progress candle: the WebSocket carries ticks for
 *  the current minute, and coarser buckets are folded server-side on request. */
export const LIVE_TF: Timeframe = "1m";

const LABEL: Record<Timeframe, string> = {
  "1m": "1 minute",
  "3m": "3 minutes",
  "5m": "5 minutes",
  "10m": "10 minutes",
  "15m": "15 minutes",
  "30m": "30 minutes",
  "1d": "1 day",
};

export function TimeframeBar({
  value,
  onChange,
}: {
  value: Timeframe;
  onChange: (tf: Timeframe) => void;
}) {
  const box = useRef<HTMLDivElement>(null);

  const move = (event: KeyboardEvent<HTMLDivElement>) => {
    const delta = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    const jump = event.key === "Home" ? 0 : event.key === "End" ? TIMEFRAMES.length - 1 : -1;
    if (!delta && jump < 0) return;

    event.preventDefault();
    const at = TIMEFRAMES.indexOf(value);
    // Wraps, because a toolbar that dead-ends makes you reverse direction to
    // reach the item one step past the edge.
    const next =
      jump >= 0 ? jump : (at + delta + TIMEFRAMES.length) % TIMEFRAMES.length;
    onChange(TIMEFRAMES[next]!);
    // Focus follows selection: the roving stop has moved, so leaving focus on
    // the old button would strand the keyboard user on an unselected control.
    box.current?.querySelector<HTMLButtonElement>(`[data-tf="${TIMEFRAMES[next]}"]`)?.focus();
  };

  return (
    <div
      className="tfbar"
      role="group"
      aria-label="Chart timeframe"
      ref={box}
      onKeyDown={move}
    >
      {TIMEFRAMES.map((tf) => {
        const on = tf === value;
        return (
          <button
            key={tf}
            type="button"
            data-tf={tf}
            className={`tfbar__btn${on ? " tfbar__btn--on" : ""}`}
            aria-pressed={on}
            // The visible label is an abbreviation a screen reader would spell out.
            aria-label={LABEL[tf]}
            tabIndex={on ? 0 : -1}
            onClick={() => onChange(tf)}
          >
            {tf === "1d" ? "1D" : tf}
          </button>
        );
      })}
    </div>
  );
}
