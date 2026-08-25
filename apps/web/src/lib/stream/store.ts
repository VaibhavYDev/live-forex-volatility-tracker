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
  #feed: FeedStatus = { state: "unknown" };
  #conn: Conn = { state: "connecting", attempt: 0 };

  #subs = new Map<Topic, Set<() => void>>();
  #tickers = new Map<string, Set<TickFn>>();
  #alertFns = new Set<AlertFn>();

  #buf = new Map<string, Quote>();
  #frame: number | null = null;
  #dirty = new Set<Topic>();

  readonly stats: Stats = {
    frames: 0,
    conflated: 0,
    applied: 0,
    maxFlushMs: 0,
    lastFlushMs: 0,
  };

  // ------------------------------------------------------------------ reads
  quote = (s: string): Quote | undefined => this.#quotes.get(s);
  vol = (s: string): Vol | undefined => this.#vol.get(s);
  regime = (s: string): Regime => this.#regime.get(s) ?? "unknown";
  alerts = (s: string): readonly Transition[] => this.#alerts.get(s) ?? NO_ALERTS;
  bars = (s: string): readonly Bar[] => this.#bars.get(s) ?? NO_BARS;
  zhist = (s: string): readonly ZPoint[] => this.#z.get(s) ?? NO_Z;
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
      if (fns) for (const fn of fns) fn(q);
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
    const prior = this.#alerts.get(t.s) ?? NO_ALERTS;
    if (prior.some((p) => p.seq === t.seq)) return; // at-least-once delivery
    this.#alerts.set(t.s, Object.freeze([t, ...prior].slice(0, ALERT_CAP)));
    this.#setRegime(t.s, t.new_regime);
    this.#dirty.add(aKey(t.s));
    this.#emit();
    for (const fn of this.#alertFns) fn(t);
  }

  setBars(bars: Record<string, readonly Bar[]>): void {
    for (const [s, rows] of Object.entries(bars)) {
      this.#bars.set(s, Object.freeze(rows));
      this.#dirty.add(bKey(s));
    }
    this.#emit();
  }

  setFeed(f: FeedStatus): void {
    this.#feed = f;
    this.#dirty.add(FEED);
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
    });
  }

  get pendingFrame(): boolean {
    return this.#frame !== null;
  }
}
