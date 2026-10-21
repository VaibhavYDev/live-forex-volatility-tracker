import { join } from "../candles";
import type { Bar } from "./types";

/**
 * One pair's one-minute bars, as every source reported them.
 *
 * Minutes reach the browser four ways: the snapshot on (re)subscribe, the
 * history request behind it, the sealed bar each vol frame carries, and - for
 * the minute that just ended, until its seal lands - the last forming bar the
 * tick frames carried. They overlap, arrive in any order and can repeat. So each
 * minute keeps one report per source, a later report from a source replaces its
 * earlier one, and the minute shown is the `join` of what is held: reports
 * collapse instead of stacking, and no source's high or low is lost to another.
 *
 * A provisional minute is the last forming bar of a minute that has ended. It
 * is shown, so a minute's range never shrinks while its seal is in flight, and
 * dropped the moment a sealed report for that minute arrives: the sealed bar
 * has every tick, the provisional only those whose frames reached us.
 */

const PROVISIONAL = "~provisional";
const EMPTY: readonly Bar[] = Object.freeze([]);

/** Two days of minutes, twice the server's hot cache; the oldest go first. */
export const MINUTE_CAP = 2_880;
/** Trimmed in batches so the chart redraws its whole series hourly, not per minute. */
const TRIM_SLACK = 60;

interface Slot {
  kinds: string[];
  parts: Bar[];
  merged: Bar;
}

const sameBar = (a: Bar, b: Bar): boolean =>
  a.o === b.o && a.h === b.h && a.l === b.l && a.c === b.c && a.n === b.n && a.src === b.src;

/** A report from our own stream, sealed: it supersedes a provisional minute. */
const isSealed = (kind: string): boolean => kind !== "backfill" && kind !== PROVISIONAL;

export class MinuteBook {
  #slots = new Map<number, Slot>();
  #sorted: readonly Bar[] = EMPTY;
  #stale = false;
  /** The minute in progress, exactly as the last tick frame carried it. */
  forming: Bar | undefined;

  get size(): number {
    return this.#slots.size;
  }

  /** Record one report of a minute. True when the minute as shown changed. */
  put(bar: Bar, kind: string = bar.src): boolean {
    const slot = this.#slots.get(bar.t);
    if (!slot) {
      this.#slots.set(bar.t, { kinds: [kind], parts: [bar], merged: bar });
      this.#changed();
      return true;
    }
    if (kind === PROVISIONAL && slot.kinds.some(isSealed)) return false;

    const at = slot.kinds.indexOf(kind);
    if (at >= 0) {
      if (sameBar(slot.parts[at]!, bar)) return false;
      slot.parts[at] = bar;
    } else {
      slot.kinds.push(kind);
      slot.parts.push(bar);
    }
    if (isSealed(kind)) {
      const p = slot.kinds.indexOf(PROVISIONAL);
      if (p >= 0) {
        slot.kinds.splice(p, 1);
        slot.parts.splice(p, 1);
      }
    }

    const merged = join(slot.parts);
    if (sameBar(merged, slot.merged)) return false;
    slot.merged = merged;
    this.#changed();
    return true;
  }

  /** Hold the last forming bar of a minute that has ended, until it seals. */
  provisional(bar: Bar): boolean {
    return this.put({ ...bar, src: "stream" }, PROVISIONAL);
  }

  /**
   * Drop provisional minutes inside `[from, to]` that `present` lacks.
   *
   * The ingestor seals minutes in order, and the snapshot and the history
   * request return every sealed minute in their range. A provisional minute
   * inside that range which they do not hold was never sealed - the ingestor
   * restarted, or a standby took over, while it was forming - so the server's
   * history will never have it, and neither does the chart from then on.
   */
  dropUnsealed(from: number, to: number, present: ReadonlySet<number>): boolean {
    let dropped = false;
    for (const [t, slot] of this.#slots) {
      if (t < from || t > to || present.has(t)) continue;
      const p = slot.kinds.indexOf(PROVISIONAL);
      if (p < 0) continue;
      slot.kinds.splice(p, 1);
      slot.parts.splice(p, 1);
      if (slot.parts.length === 0) this.#slots.delete(t);
      else slot.merged = join(slot.parts);
      dropped = true;
    }
    if (dropped) this.#stale = true;
    return dropped;
  }

  /** True once our stream's sealed bar for minute `t` is held. */
  sealed(t: number): boolean {
    return this.#slots.get(t)?.kinds.some(isSealed) ?? false;
  }

  /** Newest minute held, or undefined. */
  get newest(): number | undefined {
    return this.sorted().at(-1)?.t;
  }

  /** Every minute held, oldest first. Same reference until something changes. */
  sorted(): readonly Bar[] {
    if (this.#stale) {
      this.#sorted = Object.freeze(
        [...this.#slots.values()].map((s) => s.merged).sort((a, b) => a.t - b.t),
      );
      this.#stale = false;
    }
    return this.#sorted;
  }

  clear(): void {
    this.#slots.clear();
    this.#sorted = EMPTY;
    this.#stale = false;
    this.forming = undefined;
  }

  #changed(): void {
    this.#stale = true;
    if (this.#slots.size <= MINUTE_CAP + TRIM_SLACK) return;
    const oldest = [...this.#slots.keys()].sort((a, b) => a - b);
    for (const t of oldest.slice(0, oldest.length - MINUTE_CAP)) this.#slots.delete(t);
  }
}
