import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StoreCtx } from "../../lib/stream/hooks";
import { MarketStore } from "../../lib/stream/store";
import type { Transition } from "../../lib/stream/types";
import { ToastStack } from "../ToastStack";

let store: MarketStore;

beforeEach(() => {
  store = new MarketStore();
  vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => {
  vi.useRealTimers();
});

const t = (
  seq: number,
  to: "normal" | "stressed",
  s = "EURUSD",
): Transition => ({
  s,
  seq,
  ts: "2026-08-22T12:00:00Z",
  old_regime: to === "stressed" ? "normal" : "stressed",
  new_regime: to,
  trigger_value: 3.42,
  threshold_value: to === "stressed" ? 3 : 1.5,
  sigma: 0.0004,
  cause: "threshold",
  reason: "held",
});

const mount = () => render(<StoreCtx.Provider value={store}>{<ToastStack />}</StoreCtx.Provider>);
const fire = (x: Transition) => act(() => store.pushAlert(x));
const wait = (ms: number) => act(() => void vi.advanceTimersByTime(ms));

describe("timing (WCAG 2.2.1)", () => {
  it("never auto-dismisses an escalation", () => {
    // The thing someone would be paged about does not leave the screen because
    // ten seconds elapsed. Explicit dismissal only.
    mount();
    fire(t(1, "stressed"));
    expect(screen.getByRole("alert")).toBeInTheDocument();

    wait(120_000);
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("auto-dismisses a clear", () => {
    mount();
    fire(t(1, "normal"));
    expect(screen.getByRole("status")).toBeInTheDocument();

    wait(10_500);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("pauses the countdown while the pointer is over the stack", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    mount();
    fire(t(1, "normal"));

    await user.hover(screen.getByRole("status"));
    wait(30_000);
    expect(screen.getByRole("status")).toBeInTheDocument();

    await user.unhover(screen.getByRole("status"));
    wait(10_500);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("pauses the countdown while focus is inside", async () => {
    // Keyboard users get the same reprieve as mouse users. Hover-only pausing
    // is the version of this that passes a manual test and fails a real one.
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    mount();
    fire(t(1, "normal"));

    await user.tab();
    expect(screen.getByRole("button", { name: /dismiss/i })).toHaveFocus();

    wait(30_000);
    expect(screen.getByRole("status")).toBeInTheDocument();
  });
});

describe("dismissal", () => {
  it("closes on the button", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    mount();
    fire(t(1, "stressed"));

    await user.click(screen.getByRole("button", { name: /dismiss/i }));
    wait(300);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("closes the newest on Escape", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    mount();
    fire(t(1, "stressed", "EURUSD"));
    fire(t(2, "stressed", "GBPUSD"));

    await user.keyboard("{Escape}");
    wait(300);
    expect(screen.getByText("EURUSD")).toBeInTheDocument();
    expect(screen.queryByText("GBPUSD")).not.toBeInTheDocument();
  });
});

describe("event semantics", () => {
  it("does not re-toast a redelivered transition", () => {
    // Redis consumer groups are at-least-once; the store dedupes by seq before
    // the callback fires, and this pins that the toast path inherits it.
    mount();
    fire(t(7, "stressed"));
    fire(t(7, "stressed"));
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });

  it("keeps two symbols escalating at the same seq apart", () => {
    // seq is per-symbol. Keying the toast on seq alone would collapse these.
    mount();
    fire(t(1, "stressed", "EURUSD"));
    fire(t(1, "stressed", "GBPUSD"));
    expect(screen.getAllByRole("alert")).toHaveLength(2);
  });

  it("caps the stack rather than covering the screen", () => {
    mount();
    // Distinct z per toast so "which four survived" is actually observable —
    // identical bodies would let a cap that keeps the WRONG four pass.
    for (let i = 1; i <= 9; i++) fire({ ...t(i, "stressed"), trigger_value: i });

    expect(screen.getAllByRole("alert")).toHaveLength(4);
    // The newest survive: an old escalation matters less than the one that just
    // happened, and the full history is in the panel either way.
    expect(screen.getByText(/z 9\.00/)).toBeInTheDocument();
    expect(screen.getByText(/z 6\.00/)).toBeInTheDocument();
    expect(screen.queryByText(/z 5\.00/)).not.toBeInTheDocument();
  });

  it("shows nothing at all when nothing has happened", () => {
    const { container } = mount();
    expect(container).toBeEmptyDOMElement();
  });
});

describe("announcement", () => {
  it("interrupts for an escalation and waits its turn for a clear", () => {
    mount();
    fire(t(1, "stressed", "EURUSD"));
    fire(t(2, "normal", "GBPUSD"));

    expect(screen.getByRole("alert")).toHaveTextContent("EURUSD");
    expect(screen.getByRole("status")).toHaveTextContent("GBPUSD");
  });

  it("names the cause, because 'back to normal' means three different things", () => {
    mount();
    fire({ ...t(1, "normal"), cause: "observation_lost" });
    expect(screen.getByRole("status")).toHaveTextContent(/feed unobservable/i);
  });

  it("carries a shape as well as a colour", () => {
    mount();
    fire(t(1, "stressed"));
    expect(within(screen.getByRole("alert")).getByText("Stressed")).toBeInTheDocument();
  });

  it("passes axe", async () => {
    const { container } = mount();
    fire(t(1, "stressed"));
    expect(await axe(container)).toHaveNoViolations();
  });
});
