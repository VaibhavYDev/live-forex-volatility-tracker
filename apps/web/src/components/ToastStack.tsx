import { useCallback, useEffect, useRef, useState } from "react";
import { useAlertFeed } from "../lib/stream/hooks";
import type { Transition, TransitionCause } from "../lib/stream/types";
import { RegimeBadge } from "./RegimeBadge";

/**
 * Regime transitions, surfaced the moment they commit.
 *
 * WCAG 2.2.1 is the constraint that shapes this. Content that disappears on a
 * timer has to be adjustable, and the honest reading here is that an escalation
 * is not the kind of thing to take off someone's screen because four seconds
 * elapsed — so escalations do not auto-dismiss at all. Clears do, after
 * CLEAR_MS, and that timer pauses whenever the pointer is over the stack or
 * focus is inside it (2.2.1 pause, and 1.4.13 for the hover case).
 *
 * The stack subscribes to the store's transition callback rather than to the
 * alert list. A list is state and would re-toast its whole history on remount;
 * a toast is a reaction to an event that happened while you were watching.
 */

const CLEAR_MS = 10_000;
const CAP = 4;
/** Long enough for the exit animation; under reduced motion it just delays a
 *  removal nobody sees. */
const EXIT_MS = 220;

interface Toast {
  readonly t: Transition;
  readonly id: string;
  leaving?: boolean;
}

const CAUSE: Record<TransitionCause, string> = {
  threshold: "z-score crossed and held",
  baseline_thaw: "re-baselined — the level was accepted, not the market calming",
  observation_lost: "feed unobservable — not a claim about the market",
};

export function ToastStack() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [held, setHeld] = useState(false);
  const timers = useRef(new Map<string, number>());
  /**
   * Absolute expiry per toast, NOT a duration.
   *
   * The effect below re-runs whenever the list changes, and its cleanup clears
   * every pending timer. Re-arming those timers from a fixed CLEAR_MS restarted
   * each countdown from zero, so a clear toast never dismissed while other
   * alerts kept arriving — during an actual market event, which is exactly when
   * the stack must not grow without bound. Deadlines survive the re-arm; a
   * duration does not.
   */
  const deadlines = useRef(new Map<string, number>());
  const exits = useRef(new Set<number>());
  const pausedAt = useRef<number | null>(null);

  const drop = useCallback((id: string) => {
    deadlines.current.delete(id);
    setToasts((prev) => prev.map((x) => (x.id === id ? { ...x, leaving: true } : x)));
    const t = window.setTimeout(() => {
      exits.current.delete(t);
      setToasts((prev) => prev.filter((x) => x.id !== id));
    }, EXIT_MS);
    exits.current.add(t);
  }, []);

  // Exit timers outlive the toast they animate, so unmounting mid-animation
  // would otherwise leave them queued against a component that is gone.
  useEffect(() => {
    const pending = exits.current;
    return () => {
      for (const t of pending) window.clearTimeout(t);
      pending.clear();
    };
  }, []);

  useAlertFeed((t) => {
    // seq is per-symbol, so the id needs both or two pairs escalating in the
    // same minute would collapse into one toast.
    const id = `${t.s}:${t.seq}`;
    setToasts((prev) => (prev.some((x) => x.id === id) ? prev : [...prev, { t, id }].slice(-CAP)));
  });

  useEffect(() => {
    const live = timers.current;
    const due = deadlines.current;

    if (held) {
      // Freeze the clock rather than the timers: on resume every deadline moves
      // forward by exactly how long the pointer or focus was inside, so a
      // paused toast resumes with the time it had left, not with a full budget.
      pausedAt.current ??= performance.now();
      return;
    }

    if (pausedAt.current !== null) {
      const paused = performance.now() - pausedAt.current;
      for (const [id, at] of due) due.set(id, at + paused);
      pausedAt.current = null;
    }

    for (const toast of toasts) {
      if (toast.leaving || toast.t.new_regime === "stressed" || live.has(toast.id)) continue;
      const at = due.get(toast.id) ?? performance.now() + CLEAR_MS;
      due.set(toast.id, at);
      live.set(
        toast.id,
        window.setTimeout(
          () => {
            live.delete(toast.id);
            drop(toast.id);
          },
          Math.max(0, at - performance.now()),
        ),
      );
    }

    return () => {
      for (const timer of live.values()) window.clearTimeout(timer);
      live.clear();
    };
  }, [toasts, held, drop]);

  // Escape is the shortcut everyone already tries. Bound at the document rather
  // than on the toast so it works without having focused the stack first.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const top = toasts.findLast((x) => !x.leaving);
      if (top) drop(top.id);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [toasts, drop]);

  if (toasts.length === 0) return null;

  return (
    <div
      className="toasts"
      onPointerEnter={() => setHeld(true)}
      onPointerLeave={() => setHeld(false)}
      onFocusCapture={() => setHeld(true)}
      onBlurCapture={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setHeld(false);
      }}
    >
      {toasts.map(({ t, id, leaving }) => {
        const escalation = t.new_regime === "stressed";
        return (
          // A plain div, not <article>: an element with an implicit role cannot
          // have it overridden, and ARIA forbids alert on article outright.
          <div
            key={id}
            className="toast"
            data-to={t.new_regime}
            data-leaving={leaving || undefined}
            // Escalations interrupt; clears wait their turn. A clear is good
            // news and does not deserve to cut across whatever is being read.
            role={escalation ? "alert" : "status"}
          >
            <RegimeBadge regime={t.new_regime} />
            <div className="toast__body">
              <strong>{t.s}</strong>{" "}
              {escalation ? "entered stress" : "returned to normal"}
              <span className="toast__why">
                {t.trigger_value !== null && `z ${t.trigger_value.toFixed(2)} · `}
                {CAUSE[t.cause]}
              </span>
            </div>
            <button
              type="button"
              className="toast__close"
              onClick={() => drop(id)}
              aria-label={`Dismiss ${t.s} ${t.new_regime} alert`}
            >
              <svg viewBox="0 0 12 12" width="11" height="11" aria-hidden focusable="false">
                <path d="M2 2l8 8M10 2l-8 8" stroke="currentColor" strokeWidth="1.6" fill="none" />
              </svg>
            </button>
          </div>
        );
      })}
    </div>
  );
}
