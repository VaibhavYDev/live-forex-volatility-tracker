# 0006 — Conflating per-client queues instead of buffering

**Status:** Accepted · **Date:** 2026-08-22

## Context

A client on hotel wifi, or a backgrounded tab whose browser has throttled its
timers, cannot drain 50 messages/second. The obvious broadcast loop —
`for client in clients: await client.ws.send_json(tick)` — fails two ways, both
fatal: one slow client blocks the broadcast for every other client, or (wrapped in
a task / unbounded queue) memory grows without limit until the process is OOM-killed.

## Decision

Exploit a property of the data: **market prices are last-value-wins.** Each client
holds `pending: dict[key, payload]` containing only the newest update per routing
key. Publishing is `pending[key] = payload; wakeup.set()` — O(1), never blocks,
bounded by the number of *subscribed symbols* rather than by message rate. A
dedicated writer task per client drains it at that client's own pace.

Fast clients receive everything. Slow clients receive decimated but always *current*
data. Nobody blocks anybody. A client whose queue stays saturated past a deadline is
disconnected with close code 4003.

## Alternatives rejected

- **`asyncio.Queue(maxsize=N)` with drop-oldest.** Bounded, but keeps stale prices
  a client will never care about, and needs a tuned `N`. Conflation is strictly
  better for last-value-wins data and needs no tuning.
- **Dropping slow clients immediately.** Punishes a mobile user for having a mobile
  connection.
- **Sending everything and filtering in the browser.** Wastes the user's bandwidth
  and hides the problem rather than solving it.

## Consequences

**Conflation is only safe because this data is last-value-wins.** It would be
*wrong* for an order stream, a trade tape, or anything where each event is
semantically required. If such a stream is ever added it must use a different
transport — this is a property of the data, not a general-purpose mechanism.

The dropped count is exported as `fx_ws_frames_conflated_total`, so "we handle
backpressure" is a graph rather than a claim.
