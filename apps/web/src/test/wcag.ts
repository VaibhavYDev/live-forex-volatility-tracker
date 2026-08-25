/**
 * WCAG 2.1 relative luminance and contrast ratio.
 *
 * This lives in test-land rather than src because nothing in the app computes
 * contrast at runtime — but axe cannot check it either. jsdom returns "" from
 * getComputedStyle for every colour property, so axe's colour-contrast rule
 * silently marks itself inapplicable and the suite reports a clean pass over an
 * unchecked palette. Asserting the ratios numerically against tokens.ts is the
 * only way the claim is actually tested.
 *
 * Formula: WCAG 2.1 §1.4.3, (L1 + 0.05) / (L2 + 0.05).
 */

function rgb(hex: string): [number, number, number] {
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.replace(/./g, (c) => c + c) : h, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function channel(c: number): number {
  const s = c / 255;
  return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
}

export function luminance(hex: string): number {
  const [r, g, b] = rgb(hex).map(channel) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

export function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}
