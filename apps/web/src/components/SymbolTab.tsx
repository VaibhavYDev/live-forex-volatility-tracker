import { useEffect, useState } from "react";
import { useQuote, useRegime } from "../lib/stream/hooks";
import { RegimeBadge } from "./RegimeBadge";

/** Same threshold as the banner's "Stale". */
const QUOTE_STALE_S = 60;
const RECHECK_MS = 15_000;

/**
 * The leaf that pays for the whole store architecture.
 *
 * This component — not App — is what subscribes to a price. A EURUSD tick wakes
 * this one button and leaves the other four, the chart and the panel untouched.
 * Hoisting `useQuote` one level up would put the entire terminal on the 50/sec
 * path again and quietly undo the point of `MarketStore`.
 */
export function SymbolTab({
  symbol,
  active,
  onSelect,
}: {
  symbol: string;
  active: boolean;
  onSelect: (s: string) => void;
}) {
  const quote = useQuote(symbol);
  // The COMMITTED regime, not the instantaneous z-score. A z above the threshold
  // that has not survived confirmation is exactly the flicker the Schmitt trigger
  // exists to suppress; rendering it here would put the noise back on screen.
  const regime = useRegime(symbol);
  const dp = symbol.endsWith("JPY") ? 3 : 5;

  // A closed market sends no ticks, so nothing else would re-render this tab
  // when its last quote crosses into stale.
  const [, recheck] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => recheck((n) => n + 1), RECHECK_MS);
    return () => window.clearInterval(t);
  }, []);
  const quotedAt = quote ? new Date(quote.ts) : null;
  const stale = quotedAt !== null && (Date.now() - quotedAt.getTime()) / 1000 > QUOTE_STALE_S;

  return (
    <button
      type="button"
      role="tab"
      id={`tab-${symbol}`}
      aria-selected={active}
      aria-controls="symbol-panel"
      tabIndex={active ? 0 : -1}
      className="tab"
      data-regime={regime}
      onClick={() => onSelect(symbol)}
    >
      <span className="tab__sym">{symbol}</span>
      <span className="tab__px" data-stale={stale || undefined}>
        {quote ? quote.mid.toFixed(dp) : "—"}
      </span>
      {stale && quotedAt && (
        <span className="tab__at">as of {quotedAt.toISOString().slice(11, 16)} UTC</span>
      )}
      {regime === "stressed" && <RegimeBadge regime="stressed" compact />}
    </button>
  );
}
