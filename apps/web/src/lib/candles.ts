/**
 * Candles from minutes, in the browser.
 *
 * Every intraday timeframe is folded here from one series of one-minute bars,
 * which is what lets a timeframe switch be instant and lets every timeframe's
 * last candle move with the market: the minute in progress extends whichever
 * candle it falls in, a 30-minute one as readily as a 1-minute one.
 *
 * The rules are the server's (`fx_core.intervals.fold`), so a candle drawn here
 * is the candle `/api/bars` would serve from the same minutes:
 *
 * - open from the first minute, close from the last, high and low the extremes,
 *   tick count summed - nothing averaged, nothing filled in. A minute with no
 *   bar contributes nothing; it is not assumed to have stood still.
 * - buckets start on UTC multiples of their length, so a 15-minute candle opens
 *   at :00, :15, :30 and :45 whenever the page was loaded.
 * - provider history sorts before our own stream within a minute: the provider's
 *   bar covers the minute from its start, ours may have joined mid-minute.
 * - `src` is the shared source, or "mixed" when both contributed.
 *
 * `apps/web/src/test/candle_parity.json` holds the server's own folds of a set of
 * minutes, and both implementations are tested against it.
 */

import { isMarketOpen } from "./market";
import type { Bar } from "./stream/types";

export const MINUTE_S = 60;
export const DAY_S = 86_400;

/** Seconds per candle for every timeframe the dashboard offers. Mirrors
 *  fx_core.intervals.INTERVALS; the 1D candle is a UTC day, as on the server. */
export const TF_SECONDS = {
  "1m": 60,
  "3m": 180,
  "5m": 300,
  "10m": 600,
  "15m": 900,
  "30m": 1_800,
  "1d": DAY_S,
} as const;

export const bucketStart = (t: number, seconds: number): number =>
  t - (((t % seconds) + seconds) % seconds);

/** Provider history first, our stream last, anything else in between. */
const rank = (src: string): number =>
  src === "backfill" ? 0 : src === "mixed" ? 1 : src === "stream" ? 3 : 2;

/**
 * Several reports of the same span as one bar.
 *
 * Open from the earliest-ranked report, close from the latest; high and low the
 * extremes, so nothing either source saw is lost. The tick count is the larger,
 * not the sum: only our stream counts ticks (provider history carries an honest
 * zero) and a "mixed" bar already holds the stream's count, so summing would
 * count the same ticks twice when a minute arrives both ways.
 */
export function join(parts: readonly Bar[]): Bar {
  if (parts.length === 1) return parts[0]!;
  const sorted = [...parts].sort((a, b) => rank(a.src) - rank(b.src));
  const first = sorted[0]!;
  const last = sorted[sorted.length - 1]!;
  let h = first.h;
  let l = first.l;
  let n = first.n;
  let src = first.src;
  for (const p of sorted) {
    if (p.h > h) h = p.h;
    if (p.l < l) l = p.l;
    if (p.n > n) n = p.n;
    if (p.src !== src) src = "mixed";
  }
  return { t: first.t, o: first.o, h, l, c: last.c, n, src };
}

/**
 * Ascending, one-per-minute bars folded into candles of `seconds`.
 *
 * The input is the store's minute series, already unique per minute and sorted;
 * this does not re-sort it, because it runs on every sealed minute.
 */
export function foldMinutes(minutes: readonly Bar[], seconds: number): Bar[] {
  if (seconds === MINUTE_S) return minutes as Bar[];
  const out: Bar[] = [];
  let cur: { t: number; o: number; h: number; l: number; c: number; n: number; src: string } | null =
    null;
  for (const m of minutes) {
    const start = bucketStart(m.t, seconds);
    if (cur && cur.t === start) {
      if (m.h > cur.h) cur.h = m.h;
      if (m.l < cur.l) cur.l = m.l;
      cur.c = m.c;
      cur.n += m.n;
      if (cur.src !== m.src) cur.src = "mixed";
    } else {
      if (cur) out.push(cur);
      cur = { t: start, o: m.o, h: m.h, l: m.l, c: m.c, n: m.n, src: m.src };
    }
  }
  if (cur) out.push(cur);
  return out;
}

/** Index of the first minute at or after `at`. */
export function lowerBound(minutes: readonly Bar[], at: number): number {
  let lo = 0;
  let hi = minutes.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (minutes[mid]!.t < at) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** The candle for `[start, start + seconds)` from the minutes before `before`. */
export function bucketOf(
  minutes: readonly Bar[],
  start: number,
  seconds: number,
  before = Number.POSITIVE_INFINITY,
): Bar | null {
  const end = Math.min(start + seconds, before);
  const from = lowerBound(minutes, start);
  const to = lowerBound(minutes, end);
  if (from >= to) return null;
  const [candle] = foldMinutes(minutes.slice(from, to), seconds);
  return candle ? { ...candle, t: start } : null;
}

/**
 * A candle extended by the minute still forming.
 *
 * The forming minute is always the newest observation, so it supplies the
 * close; the open stays the candle's own unless the forming minute is all there
 * is. A candle that so far held only provider history becomes "mixed".
 */
export function extend(base: Bar | null, forming: Bar, start: number): Bar {
  if (!base) return { ...forming, t: start, src: "stream" };
  return {
    t: start,
    o: base.o,
    h: Math.max(base.h, forming.h),
    l: Math.min(base.l, forming.l),
    c: forming.c,
    n: base.n + forming.n,
    src: base.src === "stream" ? "stream" : "mixed",
  };
}

/** Same pixels: what decides whether a candle needs redrawing. */
export const sameCandle = (a: Bar | undefined, b: Bar | undefined): boolean =>
  !!a && !!b && a.t === b.t && a.o === b.o && a.h === b.h && a.l === b.l && a.c === b.c;

/** How much of today's trading we hold a minute for. */
export interface Coverage {
  /** Open-market minutes of today, so far, with a bar. */
  readonly observed: number;
  /** Open-market minutes of today that have finished. */
  readonly expected: number;
}

/** Whether each UTC hour of `day` is a trading hour; the week turns on the hour. */
function openHours(day: number): boolean[] {
  return Array.from({ length: 24 }, (_, h) => isMarketOpen(day + h * 3_600));
}

/**
 * Today's candle: the provider's daily bar for today, if any, joined with the
 * minutes we observed today. The provider's bar covers today from midnight to
 * its last refresh (hourly, server-side); ours cover what the stream saw. Their
 * union keeps every high and low either saw, and the close is ours, the newer.
 */
export function dayCandle(
  daily: readonly Bar[],
  minutes: readonly Bar[],
  day: number,
  before = Number.POSITIVE_INFINITY,
): Bar | null {
  const at = lowerBound(daily, day);
  const provider = daily[at]?.t === day ? daily[at]! : null;
  const observed = bucketOf(minutes, day, DAY_S, before);
  const parts = [provider, observed].filter((b): b is Bar => b !== null);
  return parts.length > 0 ? { ...join(parts), t: day } : null;
}

/**
 * The 1D series: the provider's days, with today's reconciled against the
 * minutes we hold, and how complete today is.
 *
 * Days before today are the provider's, as `/api/bars?interval=1d` serves them.
 * Today is complete only if we hold a bar for every open-market minute of it
 * that has ended; anything less and its high and low are only what was
 * observed, which the chart says. A missing minute is never taken to have
 * stood still. The minute still forming counts as held: it has not sealed only
 * because the next tick has not arrived yet.
 */
export function dailySeries(
  daily: readonly Bar[],
  minutes: readonly Bar[],
  nowSec: number,
  forming?: Bar,
): { bars: Bar[]; coverage: Coverage | null } {
  const today = bucketStart(nowSec, DAY_S);
  const past = daily.filter((b) => b.t < today);
  const candle = dayCandle(daily, minutes, today);

  const elapsed = bucketStart(nowSec, MINUTE_S);
  const hours = openHours(today);
  let expected = 0;
  for (let h = 0; h < 24; h++) {
    const from = today + h * 3_600;
    if (hours[h] && from < elapsed) expected += (Math.min(from + 3_600, elapsed) - from) / 60;
  }
  let observed = 0;
  for (let i = lowerBound(minutes, today); i < minutes.length; i++) {
    const t = minutes[i]!.t;
    if (t >= elapsed) break;
    if (hours[Math.floor((t - today) / 3_600)]) observed++;
  }
  if (
    forming &&
    forming.t >= today &&
    forming.t < elapsed &&
    hours[Math.floor((forming.t - today) / 3_600)] &&
    minutes[lowerBound(minutes, forming.t)]?.t !== forming.t
  ) {
    observed++;
  }

  return {
    bars: candle ? [...past, candle] : past,
    coverage: candle || expected > 0 ? { observed, expected } : null,
  };
}
