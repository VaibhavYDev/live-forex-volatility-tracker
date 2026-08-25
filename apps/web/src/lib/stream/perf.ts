/**
 * Main-thread health, measured rather than asserted.
 *
 * The README claims a 50 msg/sec feed renders smoothly. A long task is the
 * thing that makes that false — anything over 50ms blocks input, scrolling and
 * paint, and it is invisible in a screenshot. Same standard as the backend
 * benchmarks: publish the number or delete the adjective.
 */

export interface FrameStats {
  longTasks: number;
  longTaskMs: number;
  worstTaskMs: number;
  frames: number;
  droppedFrames: number;
  fps: number;
}

const BUDGET_MS = 1000 / 60;
/** Two budgets missed back to back is a visible stutter, not scheduler jitter. */
const DROP_THRESHOLD = BUDGET_MS * 2;

export class PerfMonitor {
  readonly stats: FrameStats = {
    longTasks: 0,
    longTaskMs: 0,
    worstTaskMs: 0,
    frames: 0,
    droppedFrames: 0,
    fps: 0,
  };

  #obs: PerformanceObserver | null = null;
  #raf: number | null = null;
  #last = 0;
  #windowStart = 0;
  #windowFrames = 0;

  start(): void {
    if (typeof PerformanceObserver !== "undefined") {
      try {
        this.#obs = new PerformanceObserver((list) => {
          for (const e of list.getEntries()) {
            this.stats.longTasks++;
            this.stats.longTaskMs += e.duration;
            if (e.duration > this.stats.worstTaskMs) this.stats.worstTaskMs = e.duration;
          }
        });
        this.#obs.observe({ entryTypes: ["longtask"] });
      } catch {
        // Safari and Firefox do not ship longtask. Frame timing below still
        // works, so degrade instead of throwing.
        this.#obs = null;
      }
    }
    this.#last = this.#windowStart = performance.now();
    this.#tick();
  }

  #tick = (): void => {
    const now = performance.now();
    const delta = now - this.#last;
    this.#last = now;

    this.stats.frames++;
    this.#windowFrames++;
    if (delta > DROP_THRESHOLD) {
      this.stats.droppedFrames += Math.round(delta / BUDGET_MS) - 1;
    }
    if (now - this.#windowStart >= 1000) {
      this.stats.fps = (this.#windowFrames * 1000) / (now - this.#windowStart);
      this.#windowStart = now;
      this.#windowFrames = 0;
    }
    this.#raf = requestAnimationFrame(this.#tick);
  };

  stop(): void {
    this.#obs?.disconnect();
    this.#obs = null;
    if (this.#raf !== null) cancelAnimationFrame(this.#raf);
    this.#raf = null;
  }

  report(): string {
    const s = this.stats;
    return [
      `fps ${s.fps.toFixed(1)}`,
      `dropped ${s.droppedFrames}`,
      `longtasks ${s.longTasks} (${s.longTaskMs.toFixed(0)}ms, worst ${s.worstTaskMs.toFixed(0)}ms)`,
    ].join("  ·  ");
  }
}
