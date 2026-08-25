/**
 * Reconnecting WebSocket client.
 *
 * Uses the SAME full-jitter backoff formula as the Python ingestor
 * (`fx_ingestor/supervisor.py`). That symmetry is deliberate: when the API
 * restarts, every open browser tab tries to reconnect at once. Plain exponential
 * backoff would synchronise them into a thundering herd against a server that is
 * still warming up — the browser is a client of *our* infrastructure exactly as
 * we are a client of Tiingo's, and the same rule applies.
 *
 *     sleep = random(0, min(cap, base * 2^attempt))
 */

import type {
  Bar,
  ConnState,
  FeedState,
  FeedStatus,
  Regime,
  Transition,
  ZPoint,
} from "./types";

/**
 * Wire shapes, kept snake_case and abbreviated exactly as the server emits them.
 * Renaming here would hide a protocol change behind a passing type check; the
 * translation to the app's own vocabulary happens once, in useForexStream.
 */
export type ServerFrame =
  | { type: "hello"; protocol: number; server_time: string; max_symbols: number; feed: FeedStatus }
  | { type: "snapshot"; data: Record<string, SymbolSnapshot> }
  | { type: "tick"; s: string; b: number; a: number; m: number; t: string }
  | {
      type: "vol";
      s: string;
      ts: string;
      sigma: number;
      sigma_annualized: number;
      z: number | null;
      warmed_up: boolean;
      regime: Regime;
      enter_z: number;
      exit_z: number;
    }
  | ({ type: "alert" } & Transition)
  | { type: "status"; state: FeedState; detail?: string; ts: string }
  | { type: "pong"; ts: string }
  | { type: "error"; message: string };

export interface SymbolSnapshot {
  quote: { bid?: number; ask?: number; mid?: number; ts?: string };
  bars: Bar[];
  vol: Record<string, unknown>;
  /** Same window as `bars` — they are the same minutes, on the same axis. */
  zhist: ZPoint[];
  /** Current regime, so a client connecting MID-EVENT is not shown a calm dashboard. */
  regime: { regime: Regime; seq: number; since?: string | null };
}

interface ClientOptions {
  url: string;
  symbols: string[];
  onFrame: (frame: ServerFrame) => void;
  onStateChange: (state: ConnState, attempt: number) => void;
  baseDelayMs?: number;
  capDelayMs?: number;
}

export class ForexStreamClient {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private closedByUs = false;
  private reconnectTimer: number | null = null;
  private readonly base: number;
  private readonly cap: number;

  constructor(private readonly opts: ClientOptions) {
    this.base = opts.baseDelayMs ?? 500;
    this.cap = opts.capDelayMs ?? 30_000;
  }

  /** Full jitter — uniform in [0, min(cap, base * 2^attempt)]. */
  private delay(): number {
    const ceiling = Math.min(this.cap, this.base * 2 ** this.attempt);
    return Math.random() * ceiling;
  }

  connect(): void {
    this.closedByUs = false;
    this.opts.onStateChange(this.attempt === 0 ? "connecting" : "reconnecting", this.attempt);

    const ws = new WebSocket(this.opts.url);
    this.ws = ws;

    ws.onopen = () => {
      this.attempt = 0; // a successful connection resets the ladder
      this.opts.onStateChange("open", 0);
      this.send({ op: "subscribe", symbols: this.opts.symbols, bars: 240 });
    };

    ws.onmessage = (event) => {
      try {
        this.opts.onFrame(JSON.parse(event.data as string) as ServerFrame);
      } catch {
        // One unparseable frame must never take down the stream.
      }
    };

    ws.onclose = () => {
      this.ws = null;
      if (this.closedByUs) {
        this.opts.onStateChange("closed", 0);
        return;
      }
      const wait = this.delay();
      this.attempt += 1;
      this.opts.onStateChange("reconnecting", this.attempt);
      this.reconnectTimer = window.setTimeout(() => this.connect(), wait);
    };

    ws.onerror = () => ws.close();
  }

  send(payload: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(payload));
  }

  subscribe(symbols: string[]): void {
    this.send({ op: "subscribe", symbols, bars: 240 });
  }

  unsubscribe(symbols: string[]): void {
    this.send({ op: "unsubscribe", symbols });
  }

  close(): void {
    this.closedByUs = true;
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer);
    this.ws?.close();
  }
}
