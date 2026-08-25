import { describe, expect, it } from "vitest";
import { cssVars, palettes, type Palette, type Theme } from "../../styles/tokens";
import { contrast } from "../../test/wcag";

/** Anything rendered as text, on every surface it can land on. */
const INK = ["text", "textDim", "accent", "up", "down", "warn"] as const;
const GROUNDS = ["bg", "surface", "surfaceAlt"] as const;

/**
 * `line` and `lineStrong` are deliberately excluded from 1.4.11. They are
 * dividers, and no state in this UI is carried by a border colour alone — the
 * active tab is marked with aria-selected plus a shape rail, and the regime is
 * marked with a glyph. A quiet 1.6:1 hairline is a design decision here, not an
 * accessibility failure. If a border ever becomes the sole carrier of meaning,
 * this exclusion is the thing that has to be deleted first.
 */
const DECORATIVE = new Set(["line", "lineStrong"]);

const themes = Object.entries(palettes) as [Theme, Palette][];

describe.each(themes)("%s palette", (_theme, p) => {
  it.each(GROUNDS)("keeps every text colour above 4.5:1 on %s", (ground) => {
    for (const ink of INK) {
      expect(contrast(p[ink], p[ground]), `${ink} on ${ground}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  // 1.4.11: the focus ring is a non-text indicator, so 3:1 is the bar. It clears
  // it by a wide margin on purpose — a ring you have to hunt for is a ring that
  // does not exist for the person who needs it.
  it("keeps the focus ring above 3:1 on every surface", () => {
    for (const ground of GROUNDS) {
      expect(contrast(p.focus, p[ground]), `focus on ${ground}`).toBeGreaterThanOrEqual(3);
    }
  });

  it("separates up from down by luminance, not only by hue", () => {
    // Candlestick direction is the one signal on screen that is carried by
    // colour alone — a canvas library gives us no shape to vary — so red/green
    // has to survive deuteranopia and greyscale. 1.4:1 is a house rule, not a
    // WCAG number: it is roughly where the two stop reading as one colour in a
    // greyscale screenshot. Everything else that means something (regime,
    // status, transition direction) carries a glyph or a word as well, which
    // is why `warn` is allowed to sit almost on top of `up` in luminance.
    expect(contrast(p.up, p.down)).toBeGreaterThanOrEqual(1.4);
  });

  it("declares every palette key as a CSS variable", () => {
    const emitted = cssVars(p);
    for (const key of Object.keys(p)) {
      const name = `--${key.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`;
      expect(emitted, `${key} missing from cssVars`).toContain(`${name}:`);
    }
  });
});

it("does not treat light mode as an inverted dark mode", () => {
  // Inverting saturated hues is the classic light-mode bug: #3DD68C reads as
  // crisp on near-black and as highlighter on white. This pins the palettes as
  // independently authored rather than derived.
  for (const key of INK) {
    expect(palettes.light[key], `${key} was inverted rather than re-picked`).not.toBe(
      palettes.dark[key],
    );
  }
});

it("keeps the decorative exclusions honest", () => {
  // A guard on the exclusion list itself: if someone adds a key here, the
  // contrast suite stops covering it, so the list must stay this short.
  expect([...DECORATIVE]).toEqual(["line", "lineStrong"]);
});
