import type { Regime, Transition } from "./stream/types";

/**
 * Turning a list of transitions into the spans between them.
 *
 * The chart shading and the timeline strip must agree exactly — two views of the
 * same event drawing different boundaries is the kind of detail that makes a
 * reviewer stop trusting the rest of the page. So the derivation lives here
 * once, with its own tests, instead of being written twice slightly differently.
 *
 * The authority for the span BEFORE a transition is that transition's own
 * `old_regime`, not the previous transition's `new_regime`. They should be
 * identical — the alternation invariant is enforced server-side — but if an
 * event were ever dropped in transit, trusting `old_regime` self-heals the gap
 * using the server's own account of what it believed at the time, rather than
 * silently painting a plausible-looking fiction from our end.
 */

/**
 * One bar of wall-clock. The window a chart covers runs to the END of its last
 * bar, not to that bar's opening stamp — leaving it out means an in-progress
 * event stops being shaded exactly one bar short of the right edge, which is the
 * most misleading place for shading to stop.
 */
export const BAR_S = 60;

export interface Span {
  /** Epoch seconds, inclusive. */
  readonly from: number;
  /** Epoch seconds, exclusive. `to === now` for the span still in progress. */
  readonly to: number;
  readonly regime: Regime;
}

const secs = (iso: string) => Math.floor(new Date(iso).getTime() / 1000);

/**
 * @param alerts  newest first, as the store holds them
 * @param from    window start, epoch seconds
 * @param to      window end, epoch seconds
 * @param current the committed regime right now
 */
export function spans(
  alerts: readonly Transition[],
  from: number,
  to: number,
  current: Regime,
): Span[] {
  if (to <= from) return [];

  const inside = alerts
    .map((a) => ({ at: secs(a.ts), old: a.old_regime, next: a.new_regime }))
    .filter((a) => a.at > from && a.at < to)
    .sort((a, b) => a.at - b.at);

  if (inside.length === 0) {
    // No boundary in view. The whole window is whatever we are now — including
    // "unknown", which callers render as nothing rather than as calm.
    return [{ from, to, regime: current }];
  }

  const out: Span[] = [];
  let cursor = from;
  for (const a of inside) {
    out.push({ from: cursor, to: a.at, regime: a.old });
    cursor = a.at;
  }
  out.push({ from: cursor, to, regime: inside[inside.length - 1]!.next });
  return out;
}

/** Just the stressed spans — what the chart shades and the strip highlights. */
export function stressed(
  alerts: readonly Transition[],
  from: number,
  to: number,
  current: Regime,
): Span[] {
  return spans(alerts, from, to, current).filter((s) => s.regime === "stressed");
}

/**
 * Human phrasing for a duration, for the screen-reader summaries and the strip's
 * tooltips. Deliberately coarse: "about 2 hours" is what someone wants from a
 * glance, and "2h 13m 41s" reads as precision the underlying bar cadence does
 * not actually have.
 */
export function humanDuration(seconds: number): string {
  // Guard before rounding, not after: 30 seconds rounds UP to one minute, so a
  // post-round check would report "1 minute" for every sub-minute span and the
  // honest branch would never fire.
  if (seconds < 60) return "under a minute";
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m} minute${m === 1 ? "" : "s"}`;
  const h = Math.round(m / 60);
  return `${h} hour${h === 1 ? "" : "s"}`;
}
