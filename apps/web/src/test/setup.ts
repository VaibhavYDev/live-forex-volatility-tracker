import "@testing-library/jest-dom/vitest";
import { toHaveNoViolations } from "jest-axe";
import { afterEach, expect, vi } from "vitest";
import { cleanup } from "@testing-library/react";

expect.extend(toHaveNoViolations);
afterEach(cleanup);

// jsdom has no rAF. A polyfill on setTimeout would make every frame-scheduling
// test depend on timer flushing; a manual queue lets tests step frames exactly
// and assert that an idle store schedules *zero* of them.
const frames = new Map<number, FrameRequestCallback>();
let nextId = 1;

vi.stubGlobal("requestAnimationFrame", (cb: FrameRequestCallback) => {
  const id = nextId++;
  frames.set(id, cb);
  return id;
});
vi.stubGlobal("cancelAnimationFrame", (id: number) => void frames.delete(id));

/** Run every frame queued so far. Returns how many fired. */
export function flushFrames(ts = performance.now()): number {
  const due = [...frames.entries()];
  frames.clear();
  for (const [, cb] of due) cb(ts);
  return due.length;
}

export const pendingFrames = () => frames.size;

afterEach(() => {
  frames.clear();
  nextId = 1;
});

// jsdom ships no ResizeObserver, and both canvas panes observe their container
// rather than the window so they redraw when the side panel collapses. The stub
// records the callback but never fires it: the initial draw is synchronous, so
// tests exercise the real path without a fake layout engine deciding when.
class NoopResizeObserver implements ResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}
vi.stubGlobal("ResizeObserver", NoopResizeObserver);

// jsdom returns "" for every computed property, so axe silently skips
// colour-contrast. Rather than let that read as a pass, contrast is asserted
// numerically against the design tokens in lib/__tests__/contrast.test.ts.
if (!window.matchMedia) {
  vi.stubGlobal(
    "matchMedia",
    (query: string) =>
      ({
        matches: false,
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      }) as unknown as MediaQueryList,
  );
}
