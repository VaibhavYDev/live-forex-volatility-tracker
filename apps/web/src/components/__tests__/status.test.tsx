import { act, render, screen } from "@testing-library/react";
import { axe } from "jest-axe";
import { beforeEach, describe, expect, it } from "vitest";
import { StoreCtx } from "../../lib/stream/hooks";
import { MarketStore } from "../../lib/stream/store";
import type { FeedState } from "../../lib/stream/types";
import { StatusBanner } from "../StatusBanner";

/**
 * The banner exists so a dead feed cannot masquerade as a calm market. It had the
 * opposite failure: `ts` was stamped only when the feed's STATE changed, while
 * this component reads it as "when did we last hear anything". A feed that
 * connected once and streamed for hours therefore reported
 * "Stale · last update 1336s ago" over prices updating on screen.
 *
 * A banner that cries wolf gets ignored exactly as fast as one that stays quiet,
 * so the false positive is as much a defect as the false negative would be.
 */

let store: MarketStore;
beforeEach(() => {
  store = new MarketStore();
});

const ago = (seconds: number) => new Date(Date.now() - seconds * 1000).toISOString();

const mount = () =>
  render(
    <StoreCtx.Provider value={store}>
      <StatusBanner />
    </StoreCtx.Provider>,
  );

const feed = (state: FeedState, opts: { ts?: string; since?: string; detail?: string } = {}) =>
  act(() => {
    store.setConn("open", 0);
    store.setFeed({ state, ...opts });
  });

describe("liveness", () => {
  it("reads Live while the heartbeat is fresh", () => {
    // The heartbeat runs every 5s, so anything inside the window is healthy.
    feed("healthy", { ts: ago(3), since: ago(7200) });
    mount();

    expect(screen.getByRole("status")).toHaveTextContent("Live");
    expect(screen.queryByText(/stale/i)).not.toBeInTheDocument();
  });

  it("does not go stale on a feed that has simply been up a long time", () => {
    // The regression, stated exactly: connected two hours ago, heartbeat one
    // second ago. Age-since-connect is irrelevant; age-since-heartbeat is not.
    feed("healthy", { ts: ago(1), since: ago(7200) });
    mount();

    expect(screen.getByRole("status")).toHaveTextContent("Live");
  });

  it("says Stale once the heartbeat stops", () => {
    // Twelve missed beats. At that point something really is wrong.
    feed("healthy", { ts: ago(120), since: ago(7200) });
    mount();

    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent("Stale");
    expect(banner).toHaveTextContent(/last update 12\ds ago/);
  });

  it("stays quiet when the feed has never reported at all", () => {
    // Socket up, no status message yet - the one path where a missing `ts` can
    // reach the stale branch. No heartbeat YET is not a heartbeat that STOPPED;
    // inventing an outage before the first status arrives is its own false
    // positive, and `ageS` must stay null rather than dating from the epoch.
    act(() => store.setConn("open", 0));
    mount();

    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent("Live");
    expect(banner).not.toHaveTextContent(/stale/i);
  });

  it("shows Connecting before the socket is up, not Live", () => {
    // Default state on a cold mount. Claiming Live while still dialling is the
    // same lie in the other direction.
    mount();
    expect(screen.getByRole("status")).toHaveTextContent("Connecting");
  });
});

describe("how long a problem has lasted", () => {
  it("says how long the feed has been degraded", () => {
    feed("degraded", { ts: ago(2), since: ago(720), detail: "upstream reconnecting" });
    mount();

    expect(screen.getByRole("status")).toHaveTextContent("Data delayed");
    expect(screen.getByRole("status")).toHaveTextContent("for 12 minutes");
  });

  it("omits the duration for a problem that just started", () => {
    // "degraded for under a minute" is noise on something that may clear before
    // anyone finishes reading it.
    feed("degraded", { ts: ago(1), since: ago(4), detail: "upstream reconnecting" });
    mount();

    expect(screen.getByRole("status")).not.toHaveTextContent(/for /);
  });

  it("reports a fatal feed with its duration", () => {
    feed("fatal", { ts: ago(2), since: ago(3600), detail: "invalid credentials" });
    mount();

    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent("Feed stopped");
    expect(banner).toHaveTextContent("invalid credentials");
    expect(banner).toHaveTextContent("for 1 hour");
  });
});

describe("connection state outranks feed state", () => {
  it.each([
    ["connecting", "Connecting"],
    ["reconnecting", "Reconnecting"],
    ["closed", "Disconnected"],
  ] as const)("%s shows %s", (state, label) => {
    // A perfect upstream feed is irrelevant if this browser cannot reach it.
    act(() => {
      store.setFeed({ state: "healthy", ts: ago(1) });
      store.setConn(state, 3);
    });
    mount();
    expect(screen.getByRole("status")).toHaveTextContent(label);
  });

  it("names the backoff attempt while reconnecting", () => {
    act(() => store.setConn("reconnecting", 4));
    mount();
    expect(screen.getByRole("status")).toHaveTextContent("attempt 4");
  });
});

it("announces politely rather than interrupting", async () => {
  // Feed health changes while someone is reading the page; it is not an alert.
  feed("healthy", { ts: ago(1) });
  const { container } = mount();

  expect(screen.getByRole("status")).toHaveAttribute("aria-live", "polite");
  expect(await axe(container)).toHaveNoViolations();
});
