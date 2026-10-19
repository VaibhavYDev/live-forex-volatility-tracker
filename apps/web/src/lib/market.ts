/**
 * When the FX market is open, in the browser.
 *
 * A port of `fx_core.calendar.is_market_open`, for one question only: how many
 * minutes of today could have traded. The daily candle needs that to say whether
 * its high and low are complete or only what was observed.
 *
 * Same convention: the week runs Sunday 17:00 to Friday 17:00 New York time, and
 * New York is resolved through `Intl`, so DST moves the UTC open between 21:00
 * and 22:00 without anything here knowing the dates. Holidays are not modelled,
 * exactly as on the server. `src/test/candle_parity.json` holds the
 * server's answers for the boundary minutes and both sides are tested on them.
 */

const NY = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  weekday: "short",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

const DOW: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
const BOUNDARY_MIN = 17 * 60;
const HOUR_S = 3_600;

/** True if the interbank market is open at `epoch` (seconds). */
export function isMarketOpen(epoch: number): boolean {
  let dow = 0;
  let minutes = 0;
  for (const part of NY.formatToParts(new Date(epoch * 1000))) {
    if (part.type === "weekday") dow = DOW[part.value] ?? 0;
    else if (part.type === "hour") minutes += Number(part.value) * 60;
    else if (part.type === "minute") minutes += Number(part.value);
  }
  if (dow === 5) return minutes < BOUNDARY_MIN;
  if (dow === 6) return false;
  if (dow === 0) return minutes >= BOUNDARY_MIN;
  return true;
}

/**
 * How many minute starts in `[from, to)` fall while the market is open.
 *
 * Resolved an hour at a time, not a minute at a time: New York is a whole number
 * of hours from UTC and the week opens and closes on its hour, so the answer
 * cannot change inside a UTC hour. 24 lookups for a day instead of 1,440.
 */
export function openMinutes(from: number, to: number): number {
  let count = 0;
  for (let at = from; at < to; ) {
    const next = Math.min(to, at - (at % HOUR_S) + HOUR_S);
    if (isMarketOpen(at)) count += Math.ceil(next / 60) - Math.ceil(at / 60);
    at = next;
  }
  return count;
}
