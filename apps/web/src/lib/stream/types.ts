export type Regime = "normal" | "stressed" | "unknown";
export type TransitionCause = "threshold" | "baseline_thaw" | "observation_lost";
export type FeedState = "healthy" | "degraded" | "fatal" | "stopped" | "unknown";
export type ConnState = "connecting" | "open" | "reconnecting" | "closed";

export interface Quote {
  readonly symbol: string;
  readonly bid: number;
  readonly ask: number;
  readonly mid: number;
  readonly ts: string;
}

export interface Vol {
  readonly symbol: string;
  readonly sigma: number;
  readonly sigmaAnn: number;
  readonly z: number | null;
  readonly regime: Regime;
  readonly warm: boolean;
  readonly ts: string;
  /** The two Schmitt levels, shipped with the reading so the band the pane
   *  draws can never disagree with the z it is drawing. Absent until the first
   *  bar seals. */
  readonly enterZ?: number;
  readonly exitZ?: number;
}

/** One evaluated bar's z. Bars we could not evaluate are absent, not zero — the
 *  pane draws the hole, because that is what happened. */
export interface ZPoint {
  readonly t: number;
  readonly z: number;
  /** Committed regime at that bar, not "was z over the line" — the gap between
   *  those two is the hysteresis, which is the whole point of the pane. */
  readonly r: Exclude<Regime, "unknown">;
}

/** Mirrors alert_events. `cause` is load-bearing: "back to normal" means three
 *  different things and only one of them is a claim about the market. */
export interface Transition {
  readonly s: string;
  readonly seq: number;
  readonly ts: string;
  readonly old_regime: Exclude<Regime, "unknown">;
  readonly new_regime: Exclude<Regime, "unknown">;
  readonly trigger_value: number | null;
  readonly threshold_value: number;
  readonly sigma: number | null;
  readonly cause: TransitionCause;
  readonly reason: string;
}

export interface Bar {
  readonly t: number;
  readonly o: number;
  readonly h: number;
  readonly l: number;
  readonly c: number;
  readonly n: number;
  readonly src: string;
}

export interface FeedStatus {
  readonly state: FeedState;
  readonly detail?: string;
  /** When the ingestor last CONFIRMED this — refreshed by its 5s heartbeat, so
   *  a growing age means we have stopped hearing from it, not that nothing has
   *  happened. Stamping this only on a state change is what once made a healthy
   *  feed report "Stale · last update 1336s ago". */
  readonly ts?: string;
  /** When the state last CHANGED. Lets the banner say "degraded for 12 minutes"
   *  instead of just "degraded". */
  readonly since?: string;
}

export interface Conn {
  readonly state: ConnState;
  readonly attempt: number;
}
