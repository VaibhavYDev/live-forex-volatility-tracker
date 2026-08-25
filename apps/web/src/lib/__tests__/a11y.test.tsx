import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useLiveSummary } from "../a11y";

beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
afterEach(() => vi.useRealTimers());

function Probe({ text, every }: { text: string; every: number }) {
  return (
    <p data-testid="live" aria-live="polite">
      {useLiveSummary(text, every)}
    </p>
  );
}

const shown = () => screen.getByTestId("live").textContent;
const wait = (ms: number) => act(() => void vi.advanceTimersByTime(ms));

describe("useLiveSummary", () => {
  it("announces the first value immediately", () => {
    // A live region that waits fifteen seconds to say anything is a live region
    // that appears broken to the person relying on it.
    render(<Probe text="EURUSD 1.0900" every={15_000} />);
    expect(shown()).toBe("EURUSD 1.0900");
  });

  it("swallows a burst instead of queueing one announcement per tick", () => {
    // The whole reason this exists: fifty updates a second pointed at aria-live
    // either queues thousands of announcements or makes the reader give up.
    const { rerender } = render(<Probe text="p0" every={15_000} />);
    for (let i = 1; i <= 50; i++) rerender(<Probe text={`p${i}`} every={15_000} />);

    expect(shown()).toBe("p0");
  });

  it("announces the newest value once the interval elapses, not the one it deferred", () => {
    const { rerender } = render(<Probe text="p0" every={15_000} />);
    rerender(<Probe text="p1" every={15_000} />);
    rerender(<Probe text="p2" every={15_000} />);

    wait(15_100);
    expect(shown()).toBe("p2");
  });

  it("catches up after the stream goes quiet", () => {
    // The trailing edge. Without it the summary sits permanently one update
    // stale, which is the subtle version of being wrong.
    const { rerender } = render(<Probe text="p0" every={10_000} />);
    rerender(<Probe text="final" every={10_000} />);

    wait(10_100);
    expect(shown()).toBe("final");

    wait(60_000);
    expect(shown()).toBe("final");
  });

  it("does not re-announce a value that has not changed", () => {
    const { rerender } = render(<Probe text="same" every={5_000} />);
    wait(20_000);
    rerender(<Probe text="same" every={5_000} />);
    wait(20_000);
    expect(shown()).toBe("same");
  });
});
