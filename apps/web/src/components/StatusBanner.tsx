/**
 * Honest degradation.
 *
 * A market dashboard that keeps rendering the last price with no indication the
 * feed died is worse than one showing nothing: it looks authoritative while being
 * wrong, and someone might act on it. So whenever the ingestor's circuit breaker
 * opens or the socket drops, this banner says so and shows the age of the last
 * good data.
 *
 * "Market closed" is deliberately styled as NEUTRAL, not as an error — silence at
 * 3am on a Sunday is the FX market being shut, not a fault, and conflating the
 * two trains people to ignore the banner.
 *
 * It subscribes to conn/feed itself rather than taking props, so a status change
 * re-renders 40 pixels of header and nothing else.
 */

import { useContext, useEffect, useState } from "react";
import { humanDuration } from "../lib/regime";
import { StoreCtx, useConn, useFeed } from "../lib/stream/hooks";

type Tone = "ok" | "warn" | "bad";

/** Re-render cadence for the age readout. The number is seconds, so anything
 *  slower makes it visibly jump; anything faster is wasted work. */
const CLOCK_MS = 1_000;

export function StatusBanner() {
  const conn = useConn();
  const feed = useFeed();
  const store = useContext(StoreCtx);

  // Without this the age is computed once per store update. On a feed that has
  // STOPPED there are no more updates by definition, so "last update 12s ago"
  // froze at 12 and stayed there while the real age climbed into the hours —
  // the one situation where the number matters most is the one where it stopped
  // moving.
  const [, tick] = useState(0);
  useEffect(() => {
    const t = window.setInterval(() => tick((n) => n + 1), CLOCK_MS);
    return () => window.clearInterval(t);
  }, []);
  // `ts` is a liveness heartbeat, not the age of the last state change - see
  // FeedStatus. Reading the wrong one of those two is how this banner used to
  // report "Stale" over prices that were visibly updating.
  const ageS = feed.ts ? (Date.now() - new Date(feed.ts).getTime()) / 1000 : null;
  const heldS = feed.since ? (Date.now() - new Date(feed.since).getTime()) / 1000 : null;
  const heldFor = heldS !== null && heldS >= 60 ? ` · for ${humanDuration(heldS)}` : "";

  let tone: Tone = "ok";
  let label = "Live";
  let detail = "";

  if (conn.state === "reconnecting") {
    tone = "warn";
    label = "Reconnecting";
    detail = `attempt ${conn.attempt} · jittered backoff`;
  } else if (conn.state === "connecting") {
    tone = "warn";
    label = "Connecting";
  } else if (conn.state === "closed") {
    tone = "bad";
    label = "Disconnected";
  } else if (feed.state === "degraded") {
    tone = "warn";
    label = "Data delayed";
    detail = (feed.detail || "upstream feed reconnecting") + heldFor;
  } else if (feed.state === "fatal") {
    tone = "bad";
    label = "Feed stopped";
    detail = (feed.detail || "no upstream connection") + heldFor;
  } else if (ageS !== null && ageS > 60) {
    tone = "warn";
    label = "Stale";
    detail = `last update ${Math.round(ageS)}s ago`;
  }

  return (
    <div className={`status status--${tone}`} role="status" aria-live="polite">
      {/* The dot is decoration; `label` is the accessible truth. Screen readers
          get the words, sighted users get both. */}
      <span className="status__dot" aria-hidden />
      <span className="status__label">{label}</span>
      {detail && <span className="status__detail">{detail}</span>}
      {/* Offered only when something is actually wrong. A reconnect button on a
          healthy feed is a button whose only effect is to interrupt it. */}
      {tone !== "ok" && (
        <button
          type="button"
          className="status__retry"
          onClick={() => store?.requestRefresh()}
          title="Reconnect and reload the snapshot"
        >
          Refresh
        </button>
      )}
    </div>
  );
}
