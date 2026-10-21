import type {
  Bar,
  Conn,
  ConnState,
  FeedStatus,
  Quote,
  Regime,
  Transition,
  Vol,
  ZPoint,
} from "./types";
import { MinuteBook } from "./minutes";

/**
 * Market state outside React.
 *
 * Two problems this solves that `useState` in a hook cannot:
 *
 * 1. A tick must not schedule a render. Ticks arrive ~50/sec; React
 *    reconciliation is not a 50Hz budget. Writes land in a plain Map and a
 *    single rAF-scheduled flush notifies whoever cares.
 *
 * 2. A EURUSD tick must not re-render GBPUSD. Subscriptions are keyed per
 *    topic, so a price change wakes exactly the cells bound to that symbol —
 *    not the tree.
 *
 * `getSnapshot` contract: every accessor returns a stored reference and never
 * allocates. Returning a fresh object or a `.filter()` result would make React
 * see a new value on every read and re-render forever. `store.test.ts` pins
 * that with identity assertions rather than trusting the discipline.
 */

// One frozen empty array per type, shared by every symbol that has no data yet.
// Allocating `[]` in the getter instead would hand React a new identity on every
// read and re-render forever — the single most common way this pattern is broken.
const NO_ALERTS: readonly Transition[] = Object.freeze([]);
const NO_BARS: readonly Bar[] = Object.freeze([]);
const NO_Z: readonly ZPoint[] = Object.freeze([]);
const ALERT_CAP = 60;
/** 24h of one-minute bars, matching the server's own bound on zhist. */
const Z_CAP = 1_440;

type Topic = string;

const qKey = (s: string) => `q:${s}`;
const vKey = (s: string) => `v:${s}`;
const aKey = (s: string) => `a:${s}`;
const bKey = (s: string) => `b:${s}`;
const zKey = (s: string) => `z:${s}`;
const mKey = (s: string) => `m:${s}`;
const dKey = (s: string) => `d:${s}`;
const FEED = "feed";
const CONN = "conn";

export interface Stats {
  frames: number;
  /** Updates superseded before a frame could paint them — backpressure working. */
  conflated: number;
  applied: number;
  /** Worst single flush. The number that decides whether we drop frames. */
  maxFlushMs: number;
  lastFlushMs: number;
  /** Tick listeners that threw. Non-zero means a chart is broken, not the feed. */
  painterErrors: number;
}

type TickFn = (q: Quote) => void;
type AlertFn = (t: Transition) => void;

export class MarketStore {
  #quotes = new Map<string, Quote>();
  #vol = new Map<string, Vol>();
  #regime = new Map<string, Regime>();
  #alerts = new Map<string, readonly Transition[]>();
  #bars = new Map<string, readonly Bar[]>();
  #z = new Map<string, readonly ZPoint[]>();
  #books = new Map<string, MinuteBook>();
  /** Bumped when a pair's minutes may have a hole only the history request can fill. */
  #gen = new Map<string, number>();
  /** The generation each pair's history was last loaded at. */
  #loaded = new Map<string, number>();
  #daily = new Map<string, { bars: readonly Bar[]; at: number }>();
  #feed: FeedStatus = { state: "unknown" };
  #conn: Conn = { state: "connecting", attempt: 0 };

  #subs = new Map<Topic, Set<() => void>>();
  #tickers = new Map<string, Set<TickFn>>();
  #alertFns = new Set<AlertFn>();
  #refreshFns = new Set<() => void>();

  #buf = new Map<string, Quote>();
  #frame: number | null = null;
  #dirty = new Set<Topic>();

  readonly stats: Stats = {
    frames: 0,
    conflated: 0,
    applied: 0,
    maxFlushMs: 0,
    lastFlushMs: 0,
    painterErrors: 0,
  };

  // ------------------------------------------------------------------ reads
  quote = (s: string): Quote | undefined => this.#quotes.get(s);
  vol = (s: string): Vol | undefined => this.#vol.get(s);
  regime = (s: string): Regime => this.#regime.get(s) ?? "unknown";
  alerts = (s: string): readonly Transition[] => this.#alerts.get(s) ?? NO_ALERTS;
  bars = (s: string): readonly Bar[] => this.#bars.get(s) ?? NO_BARS;
  zhist = (s: string): readonly ZPoint[] => this.#z.get(s) ?? NO_Z;
  /** Sealed (and just-ended provisional) one-minute bars, oldest first. */
  minutes = (s: string): readonly Bar[] => this.#books.get(s)?.sorted() ?? NO_BARS;
  /** The minute in progress, as the ingestor aggregates it. Read by the chart's
   *  tick painter, so it is not a render topic: it changes on every tick. */
  forming = (s: string): Bar | undefined => this.#books.get(s)?.forming;
  minuteGen = (s: string): number => this.#gen.get(s) ?? 0;
  /** True until the history request has filled this pair's minutes. */
  needsHistory = (s: string): boolean => this.#loaded.get(s) !== this.minuteGen(s);
  daily = (s: string): readonly Bar[] => this.#daily.get(s)?.bars ?? NO_BARS;
  /** When the daily series was fetched (ms), or 0. */
  dailyAt = (s: string): number => this.#daily.get(s)?.at ?? 0;
  feed = (): FeedStatus => this.#feed;
  conn = (): Conn => this.#conn;

  // ----------------------------------------------------------- subscription
  subscribe = (topic: Topic, cb: () => void): (() => void) => {
    let set = this.#subs.get(topic);
    if (!set) this.#subs.set(topic, (set = new Set()));
    set.add(cb);
    return () => {
      set.delete(cb);
      if (set.size === 0) this.#subs.delete(topic);
    };
  };

  subQuote = (s: string, cb: () => void) => this.subscribe(qKey(s), cb);
  subVol = (s: string, cb: () => void) => this.subscribe(vKey(s), cb);
  subAlerts = (s: string, cb: () => void) => this.subscribe(aKey(s), cb);
  subBars = (s: string, cb: () => void) => this.subscribe(bKey(s), cb);
  subZ = (s: string, cb: () => void) => this.subscribe(zKey(s), cb);
  subMinutes = (s: string, cb: () => void) => this.subscribe(mKey(s), cb);
  subDaily = (s: string, cb: () => void) => this.subscribe(dKey(s), cb);
  subFeed = (cb: () => void) => this.subscribe(FEED, cb);
  subConn = (cb: () => void) => this.subscribe(CONN, cb);

  /**
   * Imperative per-tick callback for canvas charts, invoked inside the flush.
   * Charts paint pixels; routing that through React state would put the
   * reconciler on the 60fps path for no benefit. Only mounted charts register,
   * so an unmounted symbol costs one failed Map lookup.
   */
  onTick = (s: string, fn: TickFn): (() => void) => {
    let set = this.#tickers.get(s);
    if (!set) this.#tickers.set(s, (set = new Set()));
    set.add(fn);
    return () => {
      set.delete(fn);
      if (set.size === 0) this.#tickers.delete(s);
    };
  };

  /**
   * Fires once per genuinely new transition — dedupe happens before this, so a
   * redelivered seq never reaches it. Toasts live here rather than deriving
   * themselves from the alert list: a toast is an ephemeral reaction to an
   * event, not a view of state, and diffing a list to rediscover "what just
   * happened" would re-toast the whole history on every remount.
   */
  onAlert = (fn: AlertFn): (() => void) => {
    this.#alertFns.add(fn);
    return () => void this.#alertFns.delete(fn);
  };

  // ------------------------------------------------------------- hot path
  /** Never blocks, never allocates beyond one Map slot per symbol. */
  offer = (q: Quote): void => {
    if (this.#buf.has(q.symbol)) this.stats.conflated++;
    this.#buf.set(q.symbol, q);
    this.#schedule();
  };

  #schedule(): void {
    // The idle guard. Re-arming unconditionally costs 60 wakeups/sec through a
    // closed weekend for zero work.
    if (this.#frame !== null) return;
    this.#frame = requestAnimationFrame(this.#flush);
  }

  #flush = (): void => {
    this.#frame = null;
    if (this.#buf.size === 0) return;

    const t0 = performance.now();
    for (const q of this.#buf.values()) {
      this.#quotes.set(q.symbol, q);
      this.#dirty.add(qKey(q.symbol));
      const fns = this.#tickers.get(q.symbol);
      if (fns) {
        for (const fn of fns) {
          try {
            fn(q);
          } catch (err) {
            // One chart throwing must not take the feed down. This runs inside
            // a rAF callback, which is outside React's call stack, so no error
            // boundary can reach it: an uncaught throw here abandoned the
            // buffer mid-drain, skipped the notify, and left every OTHER
            // symbol's UI frozen until the next tick happened to arrive.
            this.stats.painterErrors++;
            console.error("[MarketStore] tick listener threw", err);
          }
        }
      }
    }
    this.stats.applied += this.#buf.size;
    this.#buf.clear();
    this.#emit();

    const ms = performance.now() - t0;
    this.stats.lastFlushMs = ms;
    if (ms > this.stats.maxFlushMs) this.stats.maxFlushMs = ms;
    this.stats.frames++;

    // Anything that arrived while we were painting.
    if (this.#buf.size > 0) this.#schedule();
  };

  // --------------------------------------------------------------- minutes
  #book(s: string): MinuteBook {
    let book = this.#books.get(s);
    if (!book) this.#books.set(s, (book = new MinuteBook()));
    return book;
  }

  #touchMinutes(s: string): void {
    this.#dirty.add(mKey(s));
    this.#emit();
  }

  /**
   * The minute so far, from a tick frame or the snapshot. Hot path: applied on
   * arrival rather than in the rAF flush, so the frame conflation in `offer`
   * cannot lose the last bar of a minute.
   *
   * Every bar is cumulative, so a newer one replaces an older one outright. An
   * older minute, or the same minute with fewer ticks, is stale - a snapshot
   * read before the frames queued behind it - and changes nothing. When the
   * minute moves on, the one that ended is held provisionally until its sealed
   * bar arrives, so its candle never loses the range it had.
   */
  applyForming(s: string, bar: Bar): void {
    const book = this.#book(s);
    const cur = book.forming;
    if (cur && (bar.t < cur.t || (bar.t === cur.t && bar.n < cur.n))) return;
    if (book.sealed(bar.t)) return;
    book.forming = bar;
    if (cur && bar.t > cur.t && book.provisional(cur)) this.#touchMinutes(s);
  }

  /**
   * A server that predates the forming bar sends prices only. The minute is
   * then built from the prices that reached us - the old behaviour, kept so a
   * rolled-back ingestor leaves a moving chart. Its tick count stays zero
   * rather than counting frames as if they were ticks.
   */
  applyQuote(s: string, mid: number, ts: string): void {
    const t = Math.floor(Date.parse(ts) / 60_000) * 60;
    const cur = this.#books.get(s)?.forming;
    const bar =
      cur && cur.t === t
        ? { ...cur, h: Math.max(cur.h, mid), l: Math.min(cur.l, mid), c: mid }
        : { t, o: mid, h: mid, l: mid, c: mid, n: 0, src: "stream" };
    this.applyForming(s, bar);
  }

  /** A minute the ingestor sealed: final, and every tick of it. */
  sealMinute(s: string, bar: Bar): void {
    const book = this.#book(s);
    let changed = book.put(bar);
    const f = book.forming;
    if (f && f.t <= bar.t) {
      // Frames for that minute were missed; hold what we had of it.
      if (f.t < bar.t) changed = book.provisional(f) || changed;
      book.forming = undefined;
    }
    if (changed) this.#touchMinutes(s);
  }

  /**
   * Minutes from the snapshot or the history request. A snapshot that does not
   * reach back to the newest minute already held leaves a hole - the tab was
   * hidden, or the socket was down, for longer than it covers - which only the
   * history request can fill, so the pair is marked for one.
   */
  mergeMinutes(s: string, bars: readonly Bar[], from: "snapshot" | "history" = "snapshot"): void {
    const book = this.#book(s);
    const newest = book.newest;
    if (from === "snapshot" && newest !== undefined && bars.length > 0 && bars[0]!.t > newest) {
      this.#gen.set(s, this.minuteGen(s) + 1);
      this.#dirty.add(mKey(s));
    }
    let changed = false;
    for (const bar of bars) changed = book.put(bar) || changed;
    if (bars.length > 0) {
      // Authoritative over the span it covers: a minute held only
      // provisionally inside it was never sealed and never will be.
      const present = new Set(bars.map((b) => b.t));
      const from = Math.min(...present);
      const to = Math.max(...present);
      changed = book.dropUnsealed(from, to, present) || changed;
    }
    const f = book.forming;
    if (f && book.sealed(f.t)) book.forming = undefined;
    if (changed) this.#dirty.add(mKey(s));
    this.#emit();
  }

  /** Record that the history request for generation `gen` has been merged. */
  historyLoaded(s: string, gen: number): void {
    if (this.#loaded.get(s) === gen) return;
    this.#loaded.set(s, gen);
    this.#touchMinutes(s);
  }

  setDaily(s: string, bars: readonly Bar[], at = Date.now()): void {
    this.#daily.set(s, { bars: Object.freeze([...bars]), at });
    this.#dirty.add(dKey(s));
    this.#emit();
  }

  // -------------------------------------------------------- structural sets
  setVol(v: Vol): void {
    this.#vol.set(v.symbol, v);
    this.#dirty.add(vKey(v.symbol));
    if (v.regime !== "unknown") this.#setRegime(v.symbol, v.regime);
    this.#emit();
  }

  /** The z series, seeded from the snapshot and extended one point per bar. */
  setZHist(symbol: string, points: readonly ZPoint[]): void {
    this.#z.set(symbol, Object.freeze([...points].sort((a, b) => a.t - b.t)));
    this.#dirty.add(zKey(symbol));
    this.#emit();
  }

  pushZ(symbol: string, p: ZPoint): void {
    const prior = this.#z.get(symbol) ?? NO_Z;
    const last = prior[prior.length - 1];
    // A vol frame can restate the bar the snapshot already contained; replacing
    // in place keeps the series strictly increasing in time, which is the one
    // property the pane's renderer relies on.
    const kept = last && last.t === p.t ? prior.slice(0, -1) : prior;
    this.#z.set(symbol, Object.freeze([...kept, p].slice(-Z_CAP)));
    this.#dirty.add(zKey(symbol));
    this.#emit();
  }

  /**
   * Transitions are discrete events, not last-value-wins state: "escalated at
   * 14:03" is not made redundant by "cleared at 14:31". They bypass the
   * conflating buffer entirely and prepend to a per-symbol list, so the getter
   * can hand back a stable reference without filtering.
   */
  pushAlert(t: Transition): void {
    // Only notify when this is genuinely new. The stream is at-least-once, so a
    // redelivered transition would otherwise toast twice for one event.
    if (this.#recordAlert(t)) {
      for (const fn of this.#alertFns) fn(t);
    }
  }

  /**
   * History, not news.
   *
   * A transition that happened before this tab existed belongs in the alert
   * LIST, but firing it at the toast stack would greet every visitor with a
   * wall of pop-ups announcing events they were not present for. `pushAlert`
   * stays the path for anything arriving live.
   */
  seedAlert(t: Transition): void {
    this.#recordAlert(t);
  }

  /** True when the transition was new to us. */
  #recordAlert(t: Transition): boolean {
    const prior = this.#alerts.get(t.s) ?? NO_ALERTS;
    if (prior.some((p) => p.seq === t.seq)) return false; // at-least-once delivery
    this.#alerts.set(t.s, Object.freeze([t, ...prior].slice(0, ALERT_CAP)));
    this.#setRegime(t.s, t.new_regime);
    this.#dirty.add(aKey(t.s));
    this.#emit();
    return true;
  }

  setBars(bars: Record<string, readonly Bar[]>): void {
    for (const [s, rows] of Object.entries(bars)) {
      this.#bars.set(s, Object.freeze(rows));
      this.#dirty.add(bKey(s));
    }
    this.#emit();
  }

  /**
   * "Try again", from the UI to whatever owns the connection.
   *
   * The store deliberately does not know what a socket is — it is fed by one in
   * production and by a baked dataset on the published build. A callback keeps
   * that split intact: the banner asks, and whoever is driving decides what
   * reconnecting means.
   */
  onRefreshRequest = (fn: () => void): (() => void) => {
    this.#refreshFns.add(fn);
    return () => void this.#refreshFns.delete(fn);
  };

  requestRefresh(): void {
    for (const fn of this.#refreshFns) fn();
  }

  setFeed(f: FeedStatus): void {
    this.#feed = f;
    this.#dirty.add(FEED);
    this.#emit();
  }

  /** Another provider's prices, z-scores and alerts are not this one's past. */
  clearMarket(): void {
    this.#quotes.clear();
    this.#vol.clear();
    this.#regime.clear();
    this.#alerts.clear();
    this.#bars.clear();
    this.#z.clear();
    this.#buf.clear();
    for (const book of this.#books.values()) book.clear();
    for (const s of this.#books.keys()) this.#gen.set(s, this.minuteGen(s) + 1);
    this.#daily.clear();
    for (const topic of this.#subs.keys()) this.#dirty.add(topic);
    this.#emit();
  }

  setConn(state: ConnState, attempt: number): void {
    this.#conn = { state, attempt };
    this.#dirty.add(CONN);
    this.#emit();
  }

  /**
   * Snapshot regime is authoritative on connect but must never clobber a live
   * transition that landed while the snapshot was still in flight.
   */
  seedRegime(s: string, r: Regime): void {
    if (this.#regime.has(s)) return;
    this.#setRegime(s, r);
    this.#emit();
  }

  #setRegime(s: string, r: Regime): void {
    if (this.#regime.get(s) === r) return;
    this.#regime.set(s, r);
    this.#dirty.add(vKey(s));
    this.#dirty.add(aKey(s));
  }

  #emit(): void {
    if (this.#dirty.size === 0) return;
    // React 18 batches these into one render pass; the Set means a symbol
    // touched twice in a flush still notifies once.
    for (const topic of this.#dirty) {
      const set = this.#subs.get(topic);
      if (set) for (const cb of set) cb();
    }
    this.#dirty.clear();
  }

  /** Test seam. Never call in app code — it drops queued work on the floor. */
  reset(): void {
    if (this.#frame !== null) cancelAnimationFrame(this.#frame);
    this.#frame = null;
    this.#quotes.clear();
    this.#vol.clear();
    this.#regime.clear();
    this.#alerts.clear();
    this.#bars.clear();
    this.#z.clear();
    this.#books.clear();
    this.#gen.clear();
    this.#loaded.clear();
    this.#daily.clear();
    this.#buf.clear();
    this.#dirty.clear();
    this.#subs.clear();
    this.#tickers.clear();
    this.#alertFns.clear();
    this.#feed = { state: "unknown" };
    this.#conn = { state: "connecting", attempt: 0 };
    Object.assign(this.stats, {
      frames: 0,
      conflated: 0,
      applied: 0,
      maxFlushMs: 0,
      lastFlushMs: 0,
      painterErrors: 0,
    });
  }

  get pendingFrame(): boolean {
    return this.#frame !== null;
  }
}
