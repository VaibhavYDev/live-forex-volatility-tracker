"""The Redis key namespace, in one place.

Three processes touch Redis (ingestor, worker, api). If each spelled its own key
names, a typo would look like "no data" rather than an error, and would only show
up at runtime. Centralising them here - in the dependency-free core - means the
namespace physically cannot drift between services.

THE INVARIANT
-------------
    Keys WITHOUT a TTL are durable state.  Keys WITH a TTL are disposable.

That is what makes ``maxmemory-policy volatile-lru`` safe on a single Redis:
the cache is evictable, the write-ahead log and the leader lease are structurally
immune because they carry no TTL. Asserted statically in
``tests/unit/test_resilience.py`` and against a real Redis in
``tests/integration/test_redis_pipeline.py``.

Note that ``maxmemory-policy`` is a SERVER-level directive - logical databases
(``SELECT 1``) do NOT get separate policies, a very common misconception. And
under ``allkeys-lru`` Redis evicts a whole stream key at once: it does not
politely trim your WAL, it deletes it along with every unacknowledged tick.
See ``docs/adr/0002-redis-eviction-policy.md``.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "DURABLE_KEYS",
    "FEED_STATUS",
    "STREAM_ALERTS",
    "STREAM_TICKS",
    "bar_bucket",
    "channel_alert",
    "channel_status",
    "channel_tick",
    "channel_vol",
    "history",
    "quote",
    "regime_state",
    "vol_state",
    "zhist",
]

# --- durable: no TTL, never evictable ---------------------------------------
STREAM_TICKS: Final = "stream:ticks"
CONSUMER_GROUP: Final = "cg:persisters"
LEADER_LEASE: Final = "lock:ingestor:leader"

# Regime transitions get their OWN write-ahead log, not a second message type on
# stream:ticks. Three reasons, all of which bite later if you merge them:
#
#   * Retention. The tick WAL is trimmed to 15 minutes because it is a crash
#     buffer. An alert must survive far longer than that - it is the thing a
#     human is going to be paged about - so it is trimmed by count, generously,
#     and only after Postgres has it.
#   * Volume. Ticks arrive tens per second; transitions arrive a few times a
#     week. Sharing a stream would bury every alert under six orders of
#     magnitude of price data, and XAUTOCLAIM recovery would have to walk it.
#   * Failure isolation. A poison tick that stalls the tick persister must not
#     also stall alert persistence.
STREAM_ALERTS: Final = "stream:alerts"
ALERT_GROUP: Final = "cg:alert-persisters"
ALERT_MAXLEN: Final = 10_000  # ~ years of transitions; trimmed only as a backstop

# Per-symbol detector state (regime + baseline + diurnal profile), written on
# every sealed bar so a promoted standby rehydrates instead of starting cold.
#
# DELIBERATELY DURABLE - no TTL. The diurnal profile takes days of samples to
# learn, so an expiry would silently cost days of alerting quality every time a
# pair went quiet over a long weekend. It is ~1 KB per symbol; the invariant is
# worth more than the bytes.
DURABLE_KEYS: Final = frozenset({STREAM_TICKS, STREAM_ALERTS, LEADER_LEASE})

# --- disposable: always TTL'd ------------------------------------------------
TTL_QUOTE_S: Final = 300
TTL_BAR_S: Final = 3600
TTL_VOL_S: Final = 300
TTL_HISTORY_S: Final = 86_400

# The z-score series backing the volatility pane. Disposable by construction:
# every point is recomputable from bars_1m and the detector, so losing it to an
# eviction costs a redraw, not data.
TTL_ZHIST_S: Final = 86_400
ZHIST_MAXLEN: Final = 1_440  # 24h of one-minute bars, same bound as `history`

# Feed health, refreshed by the ingestor as a heartbeat.
#
# Publishing status on Pub/Sub alone is not enough: Pub/Sub is fire-and-forget,
# so an API replica that starts (or restarts, or is scaled up) AFTER the last
# status change never learns the feed state and reports "unknown" forever. So
# status is BOTH published (for immediacy) and stored (for late joiners) - the
# same snapshot-then-delta pattern used for prices, applied to health.
#
# The TTL is doing real work: if the ingestor dies, the key simply expires and
# every replica sees the feed as stale WITHOUT anyone having to send a message.
# Absence of a heartbeat is a more reliable signal than a "goodbye" that a dead
# process cannot send.
FEED_STATUS: Final = "feed:status"
TTL_STATUS_S: Final = 30


def quote(symbol: str) -> str:
    """Last top-of-book. Serves the cold-start snapshot without touching Postgres."""
    return f"q:{symbol}"


def bar_bucket(symbol: str, bucket_epoch: int) -> str:
    """In-progress bucket: count / sum / sum_sq / OHLC."""
    return f"bar:{symbol}:{bucket_epoch}"


def vol_state(symbol: str, window: str) -> str:
    """EWMA + Welford state for every estimator at one window."""
    return f"vol:{symbol}:{window}"


def history(symbol: str, interval: str = "1m") -> str:
    """Sorted set of recent sealed bars, scored by epoch. Snapshot without a JOIN."""
    return f"hist:{symbol}:{interval}"


def zhist(symbol: str) -> str:
    """Recent z-scores, one per sealed bar, scored by epoch.

    Exists so the z-score pane has a shape on first paint instead of drawing
    itself one minute at a time while the user watches. Without it a browser
    opening mid-session sees an empty chart for four hours, which reads as
    "broken" rather than "new".

    Deliberately separate from ``vol_state``: that hash is the LATEST reading and
    is overwritten every bar, so it can never answer "was this elevated ten
    minutes ago". Deliberately separate from ``history`` too - z is derived from
    a baseline that a backfilled bar does not have, so the two series legitimately
    have different lengths and merging them would imply a correspondence that
    does not hold.
    """
    return f"zhist:{symbol}"


def regime_state(symbol: str) -> str:
    """Serialised ``DetectorSnapshot``. Durable - see DURABLE_KEYS above.

    Read once when a replica wins the leader lease, written on every sealed bar.

    Without it a promoted standby starts cold, re-baselines on the elevated data,
    and silently reports NORMAL through the rest of an ongoing event - never
    emitting the clear that the stored 'stressed' row is waiting for. Measured in
    tests/chaos/test_regime_pipeline.py; we had assumed it would merely re-fire
    the escalation, which would have been the kinder bug.
    """
    return f"regime:{symbol}"


# --- pub/sub channels (not keys; nothing to evict) ---------------------------
def channel_tick(symbol: str = "*") -> str:
    return f"ch:tick:{symbol}"


def channel_vol(symbol: str = "*") -> str:
    return f"ch:vol:{symbol}"


def channel_alert(symbol: str = "*") -> str:
    """Regime transitions, pushed to the browser the instant they commit.

    Pub/Sub is the right transport here for the same reason it is right for
    ticks - immediacy, no per-client cost - but with one difference that matters:
    an alert MUST NOT be lost, so Pub/Sub is not the only path. Every transition
    is also XADDed to ``stream:alerts`` for durable persistence. Pub/Sub gets it
    on screen in milliseconds; the stream guarantees it reaches Postgres.

    A browser that was disconnected when the transition fired does not replay the
    channel - it gets the current regime in its subscribe snapshot instead, which
    is the more useful answer anyway ("what is the state now", not "what did I
    miss").
    """
    return f"ch:alerts:{symbol}"


def channel_status() -> str:
    """Feed health. Drives the 'Data delayed - reconnecting' banner in the UI."""
    return "ch:status"
