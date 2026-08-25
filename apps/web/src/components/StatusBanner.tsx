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

import { useConn, useFeed } from "../lib/stream/hooks";

type Tone = "ok" | "warn" | "bad";

export function StatusBanner() {
  const conn = useConn();
  const feed = useFeed();
  const ageS = feed.ts ? (Date.now() - new Date(feed.ts).getTime()) / 1000 : null;

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
    detail = feed.detail ?? "upstream feed reconnecting";
  } else if (feed.state === "fatal") {
    tone = "bad";
    label = "Feed stopped";
    detail = feed.detail ?? "";
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
    </div>
  );
}
