import { useCallback, useRef, useState, type KeyboardEvent } from "react";
import { PriceChart } from "./components/charts/PriceChart";
import { ZScorePane } from "./components/charts/ZScorePane";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { PerfOverlay, perfRequested } from "./components/PerfOverlay";
import { RegimeTimeline } from "./components/RegimeTimeline";
import { StatusBanner } from "./components/StatusBanner";
import { SymbolTab } from "./components/SymbolTab";
import { ThemeToggle } from "./components/ThemeToggle";
import { ToastStack } from "./components/ToastStack";
import { VolatilityPanel } from "./components/VolatilityPanel";
import { StreamProvider } from "./lib/stream/provider";
import { ThemeProvider } from "./lib/theme";

const WS_URL = import.meta.env.VITE_WS_URL ?? `ws://${window.location.hostname}:8000/ws/stream`;
const SYMBOLS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCHF"];

export default function App() {
  return (
    <ThemeProvider>
      <StreamProvider url={WS_URL} symbols={SYMBOLS}>
        <Terminal />
      </StreamProvider>
    </ThemeProvider>
  );
}

/**
 * Note what this component does NOT subscribe to: prices, volatility, alerts,
 * connection state. It owns which symbol is selected and nothing else, so the
 * only thing that re-renders it is a click. Every live value is read by the leaf
 * that displays it — that split is the entire reason the store exists.
 */
function Terminal() {
  const [active, setActive] = useState(SYMBOLS[0]!);
  const tablist = useRef<HTMLDivElement>(null);

  // A `role="tablist"` promises arrow-key navigation. Declaring the role without
  // implementing it is worse than plain buttons: it tells assistive tech the
  // keys work and then swallows them.
  const onKeyDown = useCallback((e: KeyboardEvent<HTMLDivElement>) => {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (!step && e.key !== "Home" && e.key !== "End") return;
    e.preventDefault();

    const i = SYMBOLS.indexOf(document.activeElement?.id.replace("tab-", "") ?? "");
    const next =
      e.key === "Home"
        ? 0
        : e.key === "End"
          ? SYMBOLS.length - 1
          : (i + step + SYMBOLS.length) % SYMBOLS.length;

    const target = SYMBOLS[next]!;
    setActive(target);
    tablist.current?.querySelector<HTMLButtonElement>(`#tab-${target}`)?.focus();
  }, []);

  return (
    <div className="app">
      {/* First thing in the tab order. Five pairs plus a theme toggle is six
          stops between the top of the page and the content on every load. */}
      <a className="skip" href="#symbol-panel">
        Skip to content
      </a>

      <header className="masthead">
        <div>
          <h1>Live Forex Volatility Tracker</h1>
          <p className="masthead__sub">
            Realised volatility from streaming ticks · Welford + EWMA + range estimators
          </p>
        </div>
        <div className="masthead__right">
          <StatusBanner />
          <ThemeToggle />
        </div>
      </header>

      <div
        className="tabs"
        role="tablist"
        aria-label="Currency pairs"
        ref={tablist}
        onKeyDown={onKeyDown}
      >
        {SYMBOLS.map((symbol) => (
          <SymbolTab
            key={symbol}
            symbol={symbol}
            active={symbol === active}
            onSelect={setActive}
          />
        ))}
      </div>

      {/* <main> keeps its own implicit role: ARIA forbids overriding it, and
          putting role="tabpanel" here left the page with no main landmark at
          all. tabIndex on the panel is not decoration — it holds no focusable
          content, so without it a keyboard user cannot reach what the tabs
          select. */}
      <main>
        <div
          id="symbol-panel"
          role="tabpanel"
          aria-labelledby={`tab-${active}`}
          tabIndex={0}
          className="panels"
        >
          <div className="stack">
            <ErrorBoundary region="Price chart">
              <section className="card">
                <PriceChart symbol={active} />
              </section>
            </ErrorBoundary>
            <ErrorBoundary region="Volatility pane">
              <section className="card">
                <ZScorePane symbol={active} />
                <RegimeTimeline symbol={active} />
              </section>
            </ErrorBoundary>
          </div>
          <ErrorBoundary region="Estimator panel">
            <VolatilityPanel symbol={active} />
          </ErrorBoundary>
        </div>
      </main>

      {/* Outside the tabpanel: a transition on GBPUSD matters while you are
          looking at EURUSD, and nesting it here would unmount the stack — and
          any alert still on screen — on every tab switch. */}
      <ErrorBoundary region="Alerts">
        <ToastStack />
      </ErrorBoundary>

      {perfRequested() && <PerfOverlay />}

      <footer>
        σ is labelled with its estimator, window and annualisation basis (252 trading days × 24h =
        362,880 one-minute bars/year). An unlabelled σ is meaningless.
      </footer>
    </div>
  );
}
