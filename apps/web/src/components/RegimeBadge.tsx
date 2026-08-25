import type { ReactElement } from "react";
import type { Regime } from "../lib/stream/types";

/**
 * WCAG 1.4.1: colour is never the only carrier.
 *
 * Roughly 8% of men have some form of red-green deficiency, and this badge is
 * the one control on screen that says whether the market is calm. A green pill
 * and an amber pill are the same pill to a deuteranope, so each regime gets a
 * distinct silhouette — circle, triangle, hollow dash — that survives greyscale,
 * a printed screenshot, and a projector with the saturation cranked down.
 */

const SHAPE: Record<Regime, ReactElement> = {
  normal: <circle cx="6" cy="6" r="4" />,
  stressed: <path d="M6 1.4 11 10.6H1z" />,
  unknown: <path d="M2 6h8" strokeWidth="1.6" stroke="currentColor" fill="none" />,
};

const LABEL: Record<Regime, string> = {
  normal: "Normal",
  stressed: "Stressed",
  unknown: "Unknown",
};

export function RegimeBadge({ regime, compact = false }: { regime: Regime; compact?: boolean }) {
  return (
    <span className={`regime regime--${regime}`} data-compact={compact || undefined}>
      <svg viewBox="0 0 12 12" width="10" height="10" aria-hidden focusable="false" fill="currentColor">
        {SHAPE[regime]}
      </svg>
      <span className="regime__text">{LABEL[regime]}</span>
    </span>
  );
}
