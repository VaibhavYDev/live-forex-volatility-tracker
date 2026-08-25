/**
 * Palette lives in TS, not only in CSS, so the contrast suite can assert WCAG
 * ratios against the same source the stylesheet is generated from. A palette
 * documented in a comment is a palette that drifts.
 */

export type Theme = "dark" | "light";

export interface Palette {
  bg: string;
  surface: string;
  surfaceAlt: string;
  line: string;
  lineStrong: string;
  text: string;
  textDim: string;
  accent: string;
  up: string;
  down: string;
  warn: string;
  focus: string;
}

/** Terminal-dark: near-black slate, so data-viz hues carry all the saturation. */
export const dark: Palette = {
  bg: "#0B0F14",
  surface: "#11161D",
  surfaceAlt: "#171E27",
  line: "#222B36",
  lineStrong: "#2E3A48",
  text: "#E6EDF3",
  textDim: "#94A3B4",
  accent: "#5AA2F7",
  up: "#3DD68C",
  down: "#F1707A",
  warn: "#E8B33D",
  focus: "#7FC1FF",
};

/**
 * Not an inversion. Saturated hues that read as "crisp" on near-black turn
 * neon on white, so every accent is darkened until it clears 4.5:1 on the
 * light surface — verified in contrast.test.ts rather than eyeballed.
 *
 * `down` is deeper than a designer would pick on aesthetics alone. Candlestick
 * direction is the one thing in this UI that canvas gives us no way to encode as
 * a shape, so up and down have to be separable by luminance; at the obvious
 * #C0272D the two sit 1.10:1 apart and a deuteranope sees one colour.
 */
export const light: Palette = {
  bg: "#F6F7F9",
  surface: "#FFFFFF",
  surfaceAlt: "#EFF2F5",
  line: "#DCE1E7",
  lineStrong: "#C2CAD3",
  text: "#0E141B",
  textDim: "#576373",
  accent: "#1A62CC",
  up: "#0A7A52",
  down: "#A3161C",
  warn: "#8A5A00",
  focus: "#0B4FA8",
};

export const palettes: Record<Theme, Palette> = { dark, light };

/** Emitted into a <style> tag at boot so tokens and TS cannot diverge. */
export function cssVars(p: Palette): string {
  return Object.entries(p)
    .map(([k, v]) => `--${k.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}: ${v};`)
    .join("");
}
