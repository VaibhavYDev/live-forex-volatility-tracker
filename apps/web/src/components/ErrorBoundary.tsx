import { Component, type ErrorInfo, type ReactNode } from "react";

/**
 * Contains a render failure to one region instead of blanking the terminal.
 *
 * The backend is careful about honest degradation — StatusBanner exists entirely
 * so a dead feed cannot masquerade as a calm market — and the frontend had no
 * equivalent applied to itself. One throw inside Lightweight Charts or the
 * z-pane renderer unmounted the whole tree to a white page, taking the status
 * banner with it: the component whose job is to tell you something is wrong was
 * the first casualty of something going wrong.
 *
 * Still a class, because `getDerivedStateFromError` has no hook equivalent.
 *
 * NOTE ON SCOPE: this catches render, lifecycle and constructor errors only. It
 * cannot catch a throw inside the store's rAF flush — that is not React's call
 * stack — which is why `MarketStore` isolates its own tick listeners.
 */

interface Props {
  /** Named in the fallback, so the user knows which pane died. */
  region: string;
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Console rather than a toast: the toast stack may be the thing that broke,
    // and a fallback that depends on the failing subsystem is not a fallback.
    console.error(`[${this.props.region}]`, error, info.componentStack);
  }

  #retry = () => this.setState({ error: null });

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="boundary" role="alert">
        <p className="boundary__title">{this.props.region} stopped rendering</p>
        <p className="boundary__detail">
          The rest of the terminal is unaffected and the feed is still connected.
        </p>
        <button type="button" className="boundary__retry" onClick={this.#retry}>
          Try again
        </button>
      </div>
    );
  }
}
