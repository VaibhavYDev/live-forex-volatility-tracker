import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { TIMEFRAMES, TimeframeBar, type Timeframe } from "../TimeframeBar";

/**
 * Nine controls in a row is where keyboard access usually goes wrong: either
 * every button is a tab stop, so reaching the chart takes nine presses, or the
 * roving stop is implemented without moving focus, so the keyboard user selects
 * something and their focus stays behind on a button that is no longer on.
 */

function Harness({ initial = "1m" as Timeframe }) {
  const [tf, setTf] = useState<Timeframe>(initial);
  return <TimeframeBar value={tf} onChange={setTf} />;
}

/** Looked up by ACCESSIBLE NAME ("1 week"), never by the visible abbreviation:
 *  "1m" and "1M" collide case-insensitively, which is the whole reason the
 *  buttons carry spelled-out labels. */
const btn = (name: string) => screen.getByRole("button", { name: new RegExp(`^${name}$`, "i") });

describe("selection", () => {
  it("offers every timeframe", () => {
    render(<Harness />);
    expect(screen.getAllByRole("button")).toHaveLength(TIMEFRAMES.length);
  });

  it("marks exactly one as pressed", () => {
    render(<Harness initial="4h" />);
    const on = screen.getAllByRole("button").filter((b) => b.getAttribute("aria-pressed") === "true");
    expect(on).toHaveLength(1);
    expect(on[0]).toHaveTextContent("4h");
  });

  it("reports the chosen timeframe", async () => {
    const onChange = vi.fn();
    render(<TimeframeBar value="1m" onChange={onChange} />);
    await userEvent.click(btn("1 week"));
    expect(onChange).toHaveBeenCalledWith("1w");
  });

  it("distinguishes 1m from 1M to a screen reader", () => {
    // The visible labels differ by CASE ALONE. A screen reader announcing both
    // as "one em" makes two of the nine buttons indistinguishable.
    render(<Harness />);
    expect(screen.getByRole("button", { name: /1 minute/i })).toHaveTextContent("1m");
    expect(screen.getByRole("button", { name: /1 month/i })).toHaveTextContent("1M");
  });
});

describe("keyboard", () => {
  it("is a single tab stop, not nine", () => {
    // Otherwise reaching the chart below means nine presses past controls the
    // user did not ask for.
    render(<Harness initial="1h" />);
    const stops = screen.getAllByRole("button").filter((b) => b.tabIndex === 0);
    expect(stops).toHaveLength(1);
    expect(stops[0]).toHaveTextContent("1h");
  });

  it("moves selection with the arrow keys", async () => {
    render(<Harness initial="1m" />);
    btn("1 minute").focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(btn("5 minutes")).toHaveAttribute("aria-pressed", "true");
  });

  it("takes focus with it", async () => {
    // Selecting without moving focus strands the user on an unselected button,
    // so the next arrow press jumps from the wrong place.
    render(<Harness initial="1m" />);
    btn("1 minute").focus();
    await userEvent.keyboard("{ArrowRight}");
    expect(btn("5 minutes")).toHaveFocus();
  });

  it("wraps at both ends", async () => {
    render(<Harness initial="1m" />);
    btn("1 minute").focus();
    await userEvent.keyboard("{ArrowLeft}");
    expect(btn("1 month")).toHaveAttribute("aria-pressed", "true");
  });

  it("jumps to the ends with Home and End", async () => {
    render(<Harness initial="1h" />);
    btn("1 hour").focus();
    await userEvent.keyboard("{End}");
    expect(btn("1 month")).toHaveAttribute("aria-pressed", "true");
    await userEvent.keyboard("{Home}");
    expect(btn("1 minute")).toHaveAttribute("aria-pressed", "true");
  });

  it("ignores keys it does not own", async () => {
    // ArrowDown belongs to the page; swallowing it would trap scrolling.
    const onChange = vi.fn();
    render(<TimeframeBar value="1h" onChange={onChange} />);
    btn("1 hour").focus();
    await userEvent.keyboard("{ArrowDown}");
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("accessibility", () => {
  it("is a group, not a tablist", () => {
    // These change the resolution of one panel rather than swapping panels, so
    // announcing "tab 7 of 9" would misdescribe the page.
    render(<Harness />);
    expect(screen.getByRole("group", { name: /timeframe/i })).toBeInTheDocument();
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
  });

  it("does not signal the selection with colour alone", () => {
    // WCAG 1.4.1. The selected button carries weight and an inset underline in
    // CSS; here we pin the programmatic signal that survives any stylesheet.
    render(<Harness initial="4h" />);
    expect(btn("4 hours")).toHaveAttribute("aria-pressed", "true");
    expect(btn("1 minute")).toHaveAttribute("aria-pressed", "false");
  });

  it("passes axe", async () => {
    const { container } = render(<Harness />);
    expect(await axe(container)).toHaveNoViolations();
  });
});
