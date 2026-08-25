import { useEffect, useRef, useState } from "react";

/**
 * A live region that a 50 msg/sec feed cannot flood.
 *
 * `aria-live` announces every change. Pointed at a price that updates fifty
 * times a second, a screen reader either queues thousands of announcements or
 * gives up — either way the page becomes unusable for the exact user the region
 * was added to help. Silently dropping the region is not the answer; a chart is
 * pure canvas and there is nothing else for a non-sighted user to read.
 *
 * So the text is throttled with a trailing edge: announce immediately, then at
 * most once per interval, and always announce the final value once the stream
 * goes quiet. The last part is what stops the summary from being permanently one
 * update stale.
 */
export function useLiveSummary(text: string, everyMs = 15_000): string {
  const [shown, setShown] = useState(text);
  const last = useRef(0);
  const pending = useRef(text);

  pending.current = text;

  useEffect(() => {
    const now = performance.now();
    const wait = last.current + everyMs - now;

    if (wait <= 0) {
      last.current = now;
      setShown(pending.current);
      return;
    }

    const timer = window.setTimeout(() => {
      last.current = performance.now();
      setShown(pending.current);
    }, wait);
    return () => window.clearTimeout(timer);
  }, [text, everyMs]);

  return shown;
}

/** Matches the CSS `prefers-reduced-motion` guard, for the JS that needs to know. */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false,
  );

  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!mq) return;
    const onChange = (e: MediaQueryListEvent) => setReduced(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  return reduced;
}
