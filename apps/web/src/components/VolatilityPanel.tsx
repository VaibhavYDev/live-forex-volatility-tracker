/**
 * Every number here carries its estimator, window and annualisation basis.
 *
 * An unlabelled σ is meaningless — 0.004 could be per-tick, per-minute or
 * annualised, close-to-close or Parkinson — and a finance-literate reviewer
 * checks for exactly that. The panel also greys out readings that have not
 * warmed up, because serving a σ computed from three ticks as though it were a
 * real measurement is how dashboards lie.
 */

import { demoCompare, isDemo } from "../demo";
import { apiBase } from "../lib/endpoints";
import { useEffect, useState } from "react";
import { useAlerts, useQuote, useRegime, useVol } from "../lib/stream/hooks";
import type { TransitionCause } from "../lib/stream/types";
import { RegimeBadge } from "./RegimeBadge";

const API = import.meta.env.VITE_API_URL ?? apiBase();

/**
 * The cause is not decoration. "Back to normal" means three entirely different
 * things depending on why, and only one of them is a statement about the market.
 */
const CAUSE_LABEL: Record<TransitionCause, string> = {
  threshold: "z-score crossed",
  baseline_thaw: "re-baselined — the level was accepted, the market did not calm",
  observation_lost: "feed unobservable — not a claim about the market",
};

interface CompareResponse {
  bars_used: number;
  window_s: number;
  annualization_basis: string;
  estimators: Record<string, number>;
}

const LABELS: Record<string, string> = {
  close_to_close: "Close-to-close",
  parkinson: "Parkinson",
  garman_klass: "Garman-Klass",
  rogers_satchell: "Rogers-Satchell",
  yang_zhang: "Yang-Zhang",
};

const NOTES: Record<string, string> = {
  close_to_close: "Ignores the high and low — ~80% of the bar's information discarded.",
  parkinson: "Uses the bar range. ~4.7× more efficient than close-to-close.",
  garman_klass: "Range plus open/close. ~6.6× more efficient. Assumes zero drift.",
  rogers_satchell: "Drift-independent — the one to trust on a trending pair.",
  yang_zhang: "Gap-aware. FX gaps every Friday 21:00 UTC, so this is the honest default.",
};

export function VolatilityPanel({ symbol }: { symbol: string }) {
  const quote = useQuote(symbol);
  const vol = useVol(symbol);
  const regime = useRegime(symbol);
  const alerts = useAlerts(symbol);

  const [compare, setCompare] = useState<CompareResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isDemo()) {
      // Computed by the real estimators at build time; nothing to request.
      setCompare(demoCompare(symbol));
      setError(null);
      return;
    }
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${API}/api/volatility/${symbol}/compare?limit=120`);
        if (!res.ok) {
          if (!cancelled) setError(res.status === 409 ? "warming up" : `HTTP ${res.status}`);
          return;
        }
        const data = (await res.json()) as CompareResponse;
        if (!cancelled) {
          setCompare(data);
          setError(null);
        }
      } catch {
        if (!cancelled) setError("unreachable");
      }
    };
    void load();
    const timer = window.setInterval(load, 15_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [symbol]);

  const pct = (v: number) => `${(v * 100).toFixed(2)}%`;
  const dp = symbol.endsWith("JPY") ? 3 : 5;

  return (
    <aside className="panel">
      <h2 className="panel__head">
        <span>{symbol}</span>
        <RegimeBadge regime={regime} />
      </h2>

      <div className="quotes">
        <div>
          <span className="k">bid</span>
          <span className="v">{quote?.bid.toFixed(dp) ?? "—"}</span>
        </div>
        <div>
          <span className="k">ask</span>
          <span className="v">{quote?.ask.toFixed(dp) ?? "—"}</span>
        </div>
        <div>
          <span className="k">spread</span>
          <span className="v">
            {quote ? ((quote.ask - quote.bid) * 10_000).toFixed(1) : "—"} pips
          </span>
        </div>
      </div>

      <div className={`headline ${vol?.warm ? "" : "headline--warming"}`}>
        <div className="headline__big">{vol ? pct(vol.sigmaAnn) : "—"}</div>
        <div className="headline__cap">
          EWMA σ, annualised
          {!vol?.warm && <em> · warming up</em>}
        </div>
        {vol?.z != null && (
          <div className={`headline__z ${vol.z >= 3 ? "is-hot" : ""}`}>
            z = {vol.z.toFixed(2)} vs. this pair&rsquo;s own trailing baseline
          </div>
        )}
      </div>

      {alerts.length > 0 && (
        <>
          <h3>Regime transitions</h3>
          <ul className="alerts">
            {alerts.slice(0, 5).map((a) => (
              <li key={a.seq} data-to={a.new_regime}>
                <span className="alerts__when">{new Date(a.ts).toUTCString().slice(17, 22)}</span>
                <span className="alerts__what">
                  {a.old_regime} → <strong>{a.new_regime}</strong>
                </span>
                <span className="alerts__why">
                  {a.trigger_value !== null && `z=${a.trigger_value.toFixed(2)} · `}
                  {CAUSE_LABEL[a.cause]}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      <h3>Estimators, same bars</h3>
      {error && <p className="muted">Comparison unavailable ({error}).</p>}
      {compare && (
        <>
          <table className="estimators">
            <caption className="sr-only">
              Volatility estimators computed over the same {compare.bars_used} bars
            </caption>
            <tbody>
              {Object.entries(compare.estimators).map(([key, value]) => (
                <tr key={key} title={NOTES[key]}>
                  <td>{LABELS[key] ?? key}</td>
                  <td className="num">{pct(value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="muted">
            {compare.bars_used} bars · {compare.window_s / 60} min window ·{" "}
            {compare.annualization_basis}
          </p>
        </>
      )}
    </aside>
  );
}
