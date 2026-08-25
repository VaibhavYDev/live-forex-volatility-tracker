import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as lwc from "../../test/lwc";

vi.mock("lightweight-charts", () => ({ createChart: lwc.createChart }));

import App from "../../App";

/**
 * The assembled page, not its parts.
 *
 * The audit found `<main role="tabpanel">` — an axe violation that ARIA forbids
 * outright, leaving the page with no main landmark — and the reason it shipped
 * is that every a11y test in this project rendered one component in isolation.
 * Composition defects are invisible to component tests by construction. This
 * file exists so that class of defect cannot recur, and it is worth more than
 * the one-line fix it guards.
 */

class FakeSocket {
  // The client gates send() on `WebSocket.OPEN`, so the fake has to carry the
  // readyState constants as well as the instance API.
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 1;
  sent: string[] = [];

  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  send(payload: string): void {
    this.sent.push(payload);
  }
  close(): void {
    this.readyState = 3;
    this.onclose?.();
  }
}

beforeEach(() => {
  FakeSocket.instances = [];
  lwc.reset();
  vi.stubGlobal("WebSocket", FakeSocket);
  // VolatilityPanel polls this on mount; an unhandled rejection would surface as
  // an unrelated failure.
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 409 }));
});

describe("the assembled application", () => {
  it("passes axe", async () => {
    const { container } = render(<App />);
    expect(await axe(container)).toHaveNoViolations();
  });

  it("keeps exactly one main landmark", () => {
    // main's implicit role cannot be overridden. Putting role="tabpanel" on it
    // removed the landmark entirely and took the skip link's target with it.
    render(<App />);
    expect(screen.getAllByRole("main")).toHaveLength(1);
  });

  it("puts the tab panel inside main rather than on it", () => {
    render(<App />);
    const panel = screen.getByRole("tabpanel");
    expect(panel.tagName).not.toBe("MAIN");
    expect(screen.getByRole("main").contains(panel)).toBe(true);
  });

  it("makes the panel focusable because it holds no focusable content", () => {
    // APG requirement, not a nicety: without it a keyboard user can select a
    // tab and never reach what the tab selected.
    render(<App />);
    expect(screen.getByRole("tabpanel")).toHaveAttribute("tabindex", "0");
  });

  it("offers the skip link as the very first tab stop", async () => {
    const user = userEvent.setup();
    render(<App />);

    await user.tab();
    const skip = screen.getByRole("link", { name: /skip to content/i });
    expect(skip).toHaveFocus();
    expect(skip).toHaveAttribute("href", `#${screen.getByRole("tabpanel").id}`);
  });

  it("labels the panel with the selected tab", () => {
    render(<App />);
    const selected = screen.getAllByRole("tab").find((t) => t.getAttribute("aria-selected") === "true");
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", selected!.id);
  });

  it("moves selection with the arrow keys", async () => {
    const user = userEvent.setup();
    render(<App />);

    const tabs = screen.getAllByRole("tab");
    tabs[0]!.focus();
    await user.keyboard("{ArrowRight}");

    expect(tabs[1]).toHaveFocus();
    expect(tabs[1]).toHaveAttribute("aria-selected", "true");
    expect(tabs[0]).toHaveAttribute("aria-selected", "false");
  });

  it("wraps at the end and honours Home and End", async () => {
    const user = userEvent.setup();
    render(<App />);
    const tabs = screen.getAllByRole("tab");

    tabs[0]!.focus();
    await user.keyboard("{ArrowLeft}");
    expect(tabs.at(-1)).toHaveFocus();

    await user.keyboard("{Home}");
    expect(tabs[0]).toHaveFocus();
    await user.keyboard("{End}");
    expect(tabs.at(-1)).toHaveFocus();
  });

  it("still passes axe after switching tabs", async () => {
    const user = userEvent.setup();
    const { container } = render(<App />);
    await user.click(screen.getAllByRole("tab")[2]!);
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("the stream wiring", () => {
  it("opens exactly one socket and subscribes on connect", async () => {
    render(<App />);
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));

    const ws = FakeSocket.instances[0]!;
    ws.onopen?.();
    const op = JSON.parse(ws.sent[0]!);
    expect(op.op).toBe("subscribe");
    expect(op.symbols).toContain("EURUSD");
  });

  it("survives an unparseable frame", async () => {
    // One malformed message must never take down the stream.
    render(<App />);
    await waitFor(() => expect(FakeSocket.instances).toHaveLength(1));

    const ws = FakeSocket.instances[0]!;
    expect(() => ws.onmessage?.({ data: "<html>502 Bad Gateway</html>" })).not.toThrow();
    expect(screen.getByRole("main")).toBeInTheDocument();
  });
});
