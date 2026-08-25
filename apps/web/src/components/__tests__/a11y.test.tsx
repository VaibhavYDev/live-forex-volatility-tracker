import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { axe } from "jest-axe";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { StoreCtx } from "../../lib/stream/hooks";
import { MarketStore } from "../../lib/stream/store";
import { ThemeProvider } from "../../lib/theme";
import { RegimeBadge } from "../RegimeBadge";
import { StatusBanner } from "../StatusBanner";
import { SymbolTab } from "../SymbolTab";
import { ThemeToggle } from "../ThemeToggle";

/**
 * axe is a floor, not a ceiling. It catches missing names, bad roles and broken
 * parent/child relationships; it cannot see that a green pill and an amber pill
 * are the same pill to a colourblind user, and under jsdom it cannot check
 * contrast at all (see contrast.test.ts). The assertions below cover what axe
 * structurally cannot.
 */

let store: MarketStore;
beforeEach(() => {
  store = new MarketStore();
  localStorage.clear();
});

const withStore = (ui: ReactNode) =>
  render(<StoreCtx.Provider value={store}>{ui}</StoreCtx.Provider>);

/**
 * The panel is part of the fixture on purpose. `aria-controls` pointing at an id
 * that is not in the document is itself a violation, and a fixture that renders
 * the tabs alone would have let that ship.
 */
const tabs = (active = "EURUSD") => (
  <>
    <div role="tablist" aria-label="Currency pairs">
      {["EURUSD", "GBPUSD"].map((s) => (
        <SymbolTab key={s} symbol={s} active={s === active} onSelect={() => {}} />
      ))}
    </div>
    <div id="symbol-panel" role="tabpanel" aria-labelledby={`tab-${active}`} />
  </>
);

describe("axe", () => {
  it("passes on the symbol tablist", async () => {
    const { container } = withStore(tabs());
    expect(await axe(container)).toHaveNoViolations();
  });

  it("passes on the status banner", async () => {
    store.setConn("reconnecting", 3);
    const { container } = withStore(<StatusBanner />);
    expect(await axe(container)).toHaveNoViolations();
  });

  it("passes on the theme toggle", async () => {
    const { container } = render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>,
    );
    expect(await axe(container)).toHaveNoViolations();
  });
});

describe("regime is never colour alone (WCAG 1.4.1)", () => {
  it("gives each regime its own glyph", () => {
    const shapes = new Set<string>();
    for (const regime of ["normal", "stressed", "unknown"] as const) {
      const { container, unmount } = render(<RegimeBadge regime={regime} />);
      const svg = container.querySelector("svg");
      expect(svg, `${regime} lost its glyph`).not.toBeNull();
      shapes.add(svg!.innerHTML);
      unmount();
    }
    // Three regimes, three distinct silhouettes. If two ever collapse to the
    // same shape the badge is back to being colour-only.
    expect(shapes.size).toBe(3);
  });

  it("names the regime in text, not just in the class", () => {
    render(<RegimeBadge regime="stressed" />);
    expect(screen.getByText("Stressed")).toBeInTheDocument();
  });

  it("hides the glyph from screen readers so the name is announced once", () => {
    const { container } = render(<RegimeBadge regime="normal" />);
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });
});

describe("theme toggle", () => {
  it("announces the destination, not the current state", async () => {
    // "Dark theme" as a label leaves the user guessing whether it describes
    // what is on or what pressing it will do.
    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>,
    );
    const btn = screen.getByRole("button");
    expect(btn).toHaveAccessibleName("Switch to light theme");

    await userEvent.click(btn);
    expect(btn).toHaveAccessibleName("Switch to dark theme");
    expect(btn).toHaveAttribute("aria-pressed", "true");
  });

  it("writes the theme onto the document so CSS can act on it", async () => {
    render(
      <ThemeProvider>
        <ThemeToggle />
      </ThemeProvider>,
    );
    expect(document.documentElement.dataset.theme).toBe("dark");
    await userEvent.click(screen.getByRole("button"));
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(localStorage.getItem("fx.theme")).toBe("light");
  });
});

describe("symbol tabs", () => {
  it("exposes selection through aria-selected rather than a class", () => {
    withStore(tabs("GBPUSD"));
    expect(screen.getByRole("tab", { name: /GBPUSD/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /EURUSD/ })).toHaveAttribute("aria-selected", "false");
  });

  it("keeps one tab stop for the whole set", () => {
    // Roving tabindex: Tab reaches the tablist once, arrows move within it.
    // Five tab stops for five pairs is how keyboard users end up trapped.
    withStore(tabs("EURUSD"));
    expect(screen.getByRole("tab", { name: /EURUSD/ })).toHaveAttribute("tabindex", "0");
    expect(screen.getByRole("tab", { name: /GBPUSD/ })).toHaveAttribute("tabindex", "-1");
  });

  it("renders a placeholder rather than a stale or zero price", () => {
    // A dashboard that shows 0.00000 before the first tick is asserting a
    // price it does not have.
    withStore(tabs());
    expect(screen.getByRole("tab", { name: /EURUSD/ })).toHaveTextContent("—");
  });
});
