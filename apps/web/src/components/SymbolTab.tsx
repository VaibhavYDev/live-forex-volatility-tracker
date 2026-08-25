import { useQuote, useRegime } from "../lib/stream/hooks";
import { RegimeBadge } from "./RegimeBadge";

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
      <span className="tab__px">{quote ? quote.mid.toFixed(dp) : "—"}</span>
      {regime === "stressed" && <RegimeBadge regime="stressed" compact />}
    </button>
  );
}
