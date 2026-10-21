"""Tick -> in-memory aggregation -> Redis, in one round trip.

Per tick we must do four things: durably log it, refresh the snapshot cache,
update the in-progress bar, and fan it out to browsers. Done as four sequential
``await``s that is four network round trips - roughly 8ms of added ingest latency
on a normal connection. Pipelined into one ``MULTI``/``EXEC`` it is one, ~2ms.

At 25 ticks/sec that difference is invisible; at 2_500 it is the whole system.
Writing it correctly from the start costs nothing.

Redis writes per tick
---------------------
    XADD    stream:ticks MINID ~ <now - retention>   durable WAL -> Postgres
    HSET    q:{SYMBOL}                               snapshot cache (TTL)
    HSET    bar:{SYMBOL}:{bucket}                    in-progress bar (TTL)
    PUBLISH ch:tick:{SYMBOL}                         fan-out to browsers, with the bar so far

...and additionally, when a bar seals:

    ZADD    hist:{SYMBOL}:1m                         recent bars for cold start (TTL)
    ZADD    zhist:{SYMBOL}                           z-score series for the pane (TTL)
    HSET    vol:{SYMBOL}:1h                          volatility snapshot (TTL)
    PUBLISH ch:vol:{SYMBOL}                          volatility fan-out, with the sealed bar
    SET     regime:{SYMBOL}                          detector state (DURABLE)

...and, on the rare bars where the regime actually changes:

    XADD    stream:alerts                            durable WAL -> Postgres
    PUBLISH ch:alerts:{SYMBOL}                       instant push to the browser

``MINID ~`` trims the tick WAL by **time**, not count: "keep 15 minutes of replay
buffer" is a requirement you can put in a README and reason about during an
outage. "Keep 1,000,000 entries" means something different at 20 ticks/sec than
at 2,000. The ``~`` makes trimming approximate - Redis drops whole macro-nodes
rather than walking entries - which is what keeps XADD O(1) amortised.


THE ALERT PATH IS BOTH PUBLISHED AND LOGGED
-------------------------------------------
A regime transition goes to Pub/Sub *and* to a durable stream, in the same
``MULTI``. Neither alone is sufficient:

* Pub/Sub is fire-and-forget. It gets the alert on screen in milliseconds, and
  loses it entirely if no subscriber is listening at that instant.
* The stream never loses it, but a browser reading a consumer group per client
  would be absurd.

So each does what it is good at, in one atomic round trip: if the transition is
recorded it is also broadcast, and vice versa. A partial write here would mean
either an alert nobody saw or an alert nobody can audit.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import redis.asyncio as aioredis
import structlog
from fx_core import keys
from fx_core.alerts import DetectorConfig, Regime, RegimeDetector, RegimeTransition, TriggerConfig
from fx_core.alerts.detector import DetectorSnapshot
from fx_core.models import Bar, Estimator, Tick, VolSnapshot
from fx_core.volatility import BucketAccumulator, EwmaVariance, RollingWindow
from fx_core.volatility.buckets import floor_to_bucket
from prometheus_client import Counter, Gauge, Histogram

from fx_ingestor.providers import MarketDataProvider, ProviderError

log = structlog.get_logger(__name__)

TICKS_INGESTED = Counter("fx_ticks_ingested_total", "Ticks written to the WAL", ["symbol"])
BARS_SEALED = Counter("fx_bars_sealed_total", "Bars closed and published", ["symbol"])
INGEST_LAG = Histogram(
    "fx_ingest_lag_seconds",
    "Provider event timestamp -> ingestor process",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
STREAM_DEPTH = Gauge("fx_stream_depth", "XLEN of the tick WAL")

REGIME_TRANSITIONS = Counter(
    "fx_regime_transitions_total",
    "Committed regime transitions",
    ["symbol", "new_regime", "cause"],
)
REGIME_STATE = Gauge("fx_regime_stressed", "1 while the symbol is STRESSED", ["symbol"])
DETECTOR_ARMED = Gauge("fx_detector_armed", "1 while the detector is evaluating", ["symbol"])
BARS_GATED = Counter("fx_bars_gated_total", "Sealed bars not evaluated", ["symbol", "reason"])
REGIME_RESTORED = Counter("fx_regime_restored_total", "Detector states rehydrated on failover")

# A sealed bar this far after the previous one spans a session break or an
# outage, so its return is a gap, not a minute of trading. Excluded from every
# estimator - this is the same shape of artefact as the Sunday reopen.
GAP_BAR_MULTIPLE = 3


# How much of the provider's own past to load: a day of minutes, which is all the
# live series keeps; ten years of days for 1d/1w/1M (or as far back as the
# provider serves), of which the last week is re-read on every refresh.
_MINUTE_HISTORY = timedelta(minutes=1_440)
_DAILY_HISTORY = timedelta(days=3_650)
_DAILY_REFRESHED = timedelta(days=7)

# One atomic step, so a crash cannot leave two providers' state half-swapped.
# KEYS: the provider marker, then (live, parked, restored) per state key, then
# caches to drop. ARGV: the marker value read beforehand, the new provider, and
# the number of state keys.
_ADOPT_PROVIDER_LUA = """
if (redis.call('GET', KEYS[1]) or '') ~= ARGV[1] then
    return redis.error_reply('provider marker changed concurrently')
end
for i = 2, 3 * tonumber(ARGV[3]) + 1, 3 do
    if redis.call('EXISTS', KEYS[i]) == 1 and redis.call('EXISTS', KEYS[i + 1]) == 1 then
        return redis.error_reply('refusing to overwrite ' .. KEYS[i + 1])
    end
end
local parked = 0
for i = 2, 3 * tonumber(ARGV[3]) + 1, 3 do
    if redis.call('EXISTS', KEYS[i]) == 1 then
        redis.call('RENAME', KEYS[i], KEYS[i + 1])
        parked = parked + 1
    end
    if redis.call('EXISTS', KEYS[i + 2]) == 1 then
        redis.call('RENAME', KEYS[i + 2], KEYS[i])
    end
end
for i = 3 * tonumber(ARGV[3]) + 2, #KEYS do
    redis.call('DEL', KEYS[i])
end
redis.call('SET', KEYS[1], ARGV[2])
return parked
"""


class SymbolState:
    """Per-symbol streaming state. All O(1) per tick, all bounded."""

    __slots__ = ("bucket", "detector", "ewma", "last_price", "last_sealed", "symbol", "window")

    def __init__(
        self,
        symbol: str,
        ewma_lambda: float,
        detector: RegimeDetector,
        window_seconds: int = 3600,
    ) -> None:
        self.symbol = symbol
        self.bucket: BucketAccumulator | None = None
        self.window = RollingWindow(window_seconds=window_seconds)
        self.ewma = EwmaVariance(lam=ewma_lambda)
        self.last_price: float | None = None
        self.last_sealed: datetime | None = None
        # The regime state machine, its log-space baseline and its diurnal
        # profile. See fx_core.alerts - it is pure, so everything interesting
        # about it is unit-testable without Redis.
        self.detector = detector


class IngestPipeline:
    def __init__(
        self,
        redis: aioredis.Redis,
        symbols: list[str],
        bucket_seconds: int = 60,
        ewma_lambda: float = 0.97,
        retention_s: int = 900,
        detector_config: DetectorConfig | None = None,
        trigger_config: TriggerConfig | None = None,
    ) -> None:
        self._redis = redis
        self._bucket_seconds = bucket_seconds
        self._retention_ms = retention_s * 1000
        self._detector_config = detector_config or DetectorConfig()
        self._trigger_config = trigger_config or TriggerConfig()
        self._feed_is_stale = False
        self._state = {s: self._new_state(s, ewma_lambda) for s in symbols}
        self._ewma_lambda = ewma_lambda
        self._provider = ""
        self._held_until: datetime | None = None

    def _new_state(self, symbol: str, ewma_lambda: float) -> SymbolState:
        detector = RegimeDetector(
            symbol=symbol,
            config=self._detector_config,
            trigger_config=self._trigger_config,
        )
        # The fast signal is a single sealed bar; the baseline has a ~120-bar
        # half-life. Asserting the separation here turns a silent statistical
        # failure (z structurally suppressed because the signal is measuring
        # itself) into a startup crash with an explanation.
        detector.baseline.assert_separated_from(fast_halflife=1.0)
        return SymbolState(symbol, ewma_lambda, detector)

    def state_for(self, symbol: str) -> SymbolState:
        if symbol not in self._state:
            self._state[symbol] = self._new_state(symbol, self._ewma_lambda)
        return self._state[symbol]

    @property
    def symbols(self) -> list[str]:
        return list(self._state)

    def set_feed_stale(self, stale: bool) -> None:
        """Told by the supervisor when the watchdog trips or the breaker opens.

        A frozen price reads as exactly zero volatility, so without this the
        detector reliably reports "volatility collapsed" the moment the feed
        dies - the single most embarrassing possible false positive.
        """
        self._feed_is_stale = stale

    # ------------------------------------------------------------------ ticks
    async def handle(self, tick: Tick) -> None:
        if self._held_until is not None and tick.ts_event < self._held_until:
            return
        st = self.state_for(tick.symbol)
        mid = tick.mid
        INGEST_LAG.observe(max(tick.ingest_lag_s, 0.0))

        sealed: Bar | None = None
        bucket_start = floor_to_bucket(tick.ts_event, self._bucket_seconds)

        if st.bucket is None:
            st.bucket = BucketAccumulator.start(
                tick.symbol, tick.ts_event, mid, self._bucket_seconds
            )
        elif bucket_start > st.bucket.bucket:
            # Boundary crossed: seal the old bar and start a new one. Note we seal
            # on the *arrival of the next bucket's first tick*, not on a timer, so
            # a bar is never published before its window has actually elapsed.
            sealed = st.bucket.seal()
            st.window.push(sealed)
            st.bucket = BucketAccumulator.start(
                tick.symbol, tick.ts_event, mid, self._bucket_seconds
            )

        st.bucket.add(mid, st.last_price)
        if st.last_price is not None and st.last_price > 0.0 and mid > 0.0:
            st.ewma.update(math.log(mid / st.last_price))
        st.last_price = mid

        await self._write(tick, st, sealed)

    async def _write(self, tick: Tick, st: SymbolState, sealed: Bar | None) -> None:
        assert st.bucket is not None
        bucket_epoch = int(st.bucket.bucket.timestamp())

        # transaction=True wraps this in MULTI/EXEC: one round trip, all-or-nothing.
        pipe = self._redis.pipeline(transaction=True)

        pipe.xadd(
            keys.STREAM_TICKS,
            {
                "s": tick.symbol,
                "b": repr(tick.bid),
                "a": repr(tick.ask),
                "t": tick.ts_event.isoformat(),
                "q": tick.seq,
                "p": self._provider,
            },
            minid=int(datetime.now(UTC).timestamp() * 1000) - self._retention_ms,
            approximate=True,
        )
        pipe.hset(
            keys.quote(tick.symbol),
            mapping={
                "bid": repr(tick.bid),
                "ask": repr(tick.ask),
                "mid": repr(tick.mid),
                "ts": tick.ts_event.isoformat(),
            },
        )
        pipe.expire(keys.quote(tick.symbol), keys.TTL_QUOTE_S)

        pipe.hset(
            keys.bar_bucket(tick.symbol, bucket_epoch),
            mapping={
                "o": repr(st.bucket.open),
                "h": repr(st.bucket.high),
                "l": repr(st.bucket.low),
                "c": repr(st.bucket.close),
                "n": st.bucket.tick_count,
                "sr": repr(st.bucket.sum_ret),
                "sr2": repr(st.bucket.sum_ret_sq),
            },
        )
        pipe.expire(keys.bar_bucket(tick.symbol, bucket_epoch), keys.TTL_BAR_S)

        pipe.publish(
            keys.channel_tick(tick.symbol),
            json.dumps(
                {
                    "s": tick.symbol,
                    "b": tick.bid,
                    "a": tick.ask,
                    "m": tick.mid,
                    "t": tick.ts_event.isoformat(),
                    # The minute so far, from every tick this process has seen
                    # in it. Cumulative, so a browser that only receives some of
                    # these frames - the API conflates them for a slow reader,
                    # the browser once more per frame it paints - still draws
                    # the high and low it missed the ticks of.
                    "bar": {
                        "t": bucket_epoch,
                        "o": st.bucket.open,
                        "h": st.bucket.high,
                        "l": st.bucket.low,
                        "c": st.bucket.close,
                        "n": st.bucket.tick_count,
                    },
                }
            ),
        )

        transition: RegimeTransition | None = None
        if sealed is not None:
            pipe.zadd(
                keys.history(tick.symbol),
                {json.dumps(_bar_payload(sealed)): int(sealed.bucket.timestamp())},
            )
            # Bound the sorted set: keep the most recent 1_440 bars (24h of 1m).
            pipe.zremrangebyrank(keys.history(tick.symbol), 0, -1441)
            pipe.expire(keys.history(tick.symbol), keys.TTL_HISTORY_S)
            transition = self._seal_bar(pipe, st, sealed)

        await pipe.execute()
        TICKS_INGESTED.labels(symbol=tick.symbol).inc()

        if transition is not None:
            log.warning(
                "regime.transition",
                symbol=transition.symbol,
                seq=transition.seq,
                old=str(transition.old_regime),
                new=str(transition.new_regime),
                cause=str(transition.cause),
                z=round(transition.trigger_value, 2)
                if math.isfinite(transition.trigger_value)
                else None,
                reason=transition.reason,
            )

    # ------------------------------------------------------------------ bars
    def _seal_bar(
        self, pipe: aioredis.client.Pipeline, st: SymbolState, sealed: Bar
    ) -> RegimeTransition | None:
        """Everything that happens once per minute, not once per tick."""
        realized = st.window.sigma()

        # Is this bar contiguous with the last one, or does it span a hole?
        gap = st.last_sealed is not None and (sealed.bucket - st.last_sealed) > timedelta(
            seconds=self._bucket_seconds * GAP_BAR_MULTIPLE
        )
        st.last_sealed = sealed.bucket

        # The fast signal: realised volatility OF THIS MINUTE, not the rolling
        # window and not the per-tick EWMA. The rolling window is far too slow to
        # be the numerator of a z-score whose denominator is a 120-bar baseline -
        # they would move together and z would collapse. See
        # `assert_separated_from` in _new_state.
        transition = st.detector.update(
            sealed.realized_vol,
            sealed.bucket,
            is_gap_bar=gap,
            is_backfill=sealed.source != "stream",
            feed_is_stale=self._feed_is_stale,
        )

        armed = st.detector.armed
        DETECTOR_ARMED.labels(symbol=st.symbol).set(1.0 if armed else 0.0)
        REGIME_STATE.labels(symbol=st.symbol).set(
            1.0 if st.detector.regime is Regime.STRESSED else 0.0
        )
        if not armed:
            BARS_GATED.labels(symbol=st.symbol, reason=st.detector.trigger.gate_reason).inc()

        self._queue_vol_snapshot(pipe, st, sealed, realized)
        self._queue_detector_snapshot(pipe, st)
        if transition is not None:
            self._queue_transition(pipe, transition)
        return transition

    def _queue_vol_snapshot(
        self, pipe: aioredis.client.Pipeline, st: SymbolState, sealed: Bar, realized: float
    ) -> None:
        """Publish the volatility reading for a freshly sealed bar."""
        snap = VolSnapshot.build(
            symbol=st.symbol,
            estimator=Estimator.EWMA,
            window_s=st.window.window_seconds,
            # Per-tick variance times the bar's tick count is the bar's variance, which
            # the bar basis annualises. Annualising per tick as if ticks were one second
            # apart understated sigma by the square root of the tick rate.
            sigma=st.ewma.sigma * math.sqrt(sealed.tick_count),
            sample_count=st.ewma.n,
            bar_seconds=self._bucket_seconds,
            zscore=st.detector.last_zscore,
        )
        payload = {
            "s": st.symbol,
            "ts": snap.ts.isoformat(),
            "estimator": str(snap.estimator),
            "window_s": snap.window_s,
            "sigma": snap.sigma,
            "sigma_annualized": snap.sigma_annualized,
            "realized_sigma": realized,
            "z": st.detector.last_zscore,
            "regime": str(st.detector.regime),
            "armed": st.detector.armed,
            "warmed_up": st.ewma.warmed_up,
            "bars": len(st.window),
            # The two Schmitt levels travel WITH the reading rather than sitting
            # in a config endpoint, so the pane can never draw a band that does
            # not match the z it is plotting. They are cheap and they are the
            # only way a viewer can tell "3.1" from "about to escalate".
            "enter_z": self._trigger_config.enter_z,
            "exit_z": self._trigger_config.exit_z,
        }
        pipe.hset(
            keys.vol_state(st.symbol, "1h"),
            mapping={k: json.dumps(v) for k, v in payload.items()},
        )
        pipe.expire(keys.vol_state(st.symbol, "1h"), keys.TTL_VOL_S)
        # The frame carries the sealed bar; the stored reading does not. It is
        # the minute the history set just received, so a browser keeps its
        # minutes current without a request, and gets every tick of a minute
        # even when conflation dropped the last tick frames of it.
        pipe.publish(
            keys.channel_vol(st.symbol), json.dumps({**payload, "bar": _bar_payload(sealed)})
        )
        self._queue_zhist(pipe, st, sealed)
        BARS_SEALED.labels(symbol=st.symbol).inc()

    def _queue_zhist(self, pipe: aioredis.client.Pipeline, st: SymbolState, sealed: Bar) -> None:
        """Append this bar's z to the series the volatility pane draws.

        Gated bars are skipped rather than written as null. A gap in the series
        is honest - we could not measure - whereas a zero would be plotted as
        "calm" and a null would have to be special-cased in every consumer. The
        pane draws the gap as a break in the line, which is what actually
        happened.

        The committed regime rides along per point so the pane can shade the line
        without re-deriving state from the transition list, which would give a
        different answer during the confirmation window: z crosses before the
        trigger commits, and that difference IS the hysteresis.
        """
        z = st.detector.last_zscore
        if z is None or not math.isfinite(z):
            return

        epoch = int(sealed.bucket.timestamp())
        key = keys.zhist(st.symbol)
        pipe.zadd(
            key,
            {json.dumps({"t": epoch, "z": z, "r": str(st.detector.regime)}): epoch},
        )
        pipe.zremrangebyrank(key, 0, -(keys.ZHIST_MAXLEN + 1))
        pipe.expire(key, keys.TTL_ZHIST_S)

    def _queue_detector_snapshot(self, pipe: aioredis.client.Pipeline, st: SymbolState) -> None:
        """Persist detector state on EVERY sealed bar, not on a timer.

        Snapshotting periodically would mean a failover could lose whichever
        minutes fell after the last write - including, in the worst case, the
        escalation itself, leaving the promoted standby blind to an event already
        in progress. Once a minute per symbol is a handful of small writes
        already inside the existing MULTI: there is no reason to be clever.

        Deliberately no TTL (see fx_core.keys.regime_state): the diurnal profile
        takes days to learn, so an expiry during a quiet weekend would silently
        cost days of alerting quality.
        """
        pipe.set(keys.regime_state(st.symbol), json.dumps(st.detector.snapshot()))

    def _queue_transition(
        self, pipe: aioredis.client.Pipeline, transition: RegimeTransition
    ) -> None:
        """Durable log AND instant broadcast, in the same transaction."""
        payload = _transition_payload(transition)

        pipe.xadd(
            keys.STREAM_ALERTS,
            {k: ("" if v is None else str(v)) for k, v in payload.items()},
            maxlen=keys.ALERT_MAXLEN,
            approximate=True,
        )
        pipe.publish(keys.channel_alert(transition.symbol), json.dumps(payload))
        REGIME_TRANSITIONS.labels(
            symbol=transition.symbol,
            new_regime=str(transition.new_regime),
            cause=str(transition.cause),
        ).inc()

    # ----------------------------------------------------------- rehydration
    async def adopt_provider(self, provider: str) -> tuple[str, int] | None:
        """Leave only this provider's history and detector state live.

        The previous provider's are renamed under ``keys.archive`` rather than
        deleted, and this provider's own archive, if any, comes back. State with
        no recorded provider is parked as "unverified": nothing proves which feed
        produced it. Quote and volatility caches describe the old feed and go.
        Ticks are dropped until the minute after the previous feed's last tick:
        bars_1m is keyed by (symbol, bucket), so a minute both feeds wrote would
        merge their prices into one row.
        Returns (previous provider, keys parked) when the provider changed.
        """
        self._provider = provider
        previous = await self._redis.get(keys.PROVIDER)
        if previous == provider:
            return None
        label = str(previous or "unverified")
        state = [
            key
            for symbol in self.symbols
            for key in (
                keys.history(symbol),
                keys.history(symbol, "1d"),
                keys.zhist(symbol),
                keys.regime_state(symbol),
            )
        ]
        moves = [
            k for key in state for k in (key, keys.archive(label, key), keys.archive(provider, key))
        ]
        caches = [k for s in self.symbols for k in (keys.quote(s), keys.vol_state(s, "1h"))]
        parked = await self._redis.eval(
            _ADOPT_PROVIDER_LUA,
            1 + len(moves) + len(caches),
            keys.PROVIDER,
            *moves,
            *caches,
            previous or "",
            provider,
            len(state),
        )
        newest: Any = await self._redis.xrevrange(keys.STREAM_TICKS, count=1)
        if newest:
            last = floor_to_bucket(datetime.fromisoformat(newest[0][1]["t"]), self._bucket_seconds)
            self._held_until = last + timedelta(seconds=self._bucket_seconds)
        return label, int(parked)

    async def seed_history(self, provider: MarketDataProvider) -> int:
        """Load the provider's own minute bars into an EMPTY minute series.

        Only an empty series is written, so bars the feed observed are never
        replaced. When the provider has no history to give - no endpoint, no
        entitlement, an outage - the series stays empty and the API reports
        warming_up: an absent past is shown as absent, never invented.
        """
        written = 0
        for symbol in self.symbols:
            if not await self._redis.exists(keys.history(symbol)):
                written += await self._store_history(provider, symbol, "1m", _MINUTE_HISTORY)
        return written

    async def refresh_daily_history(self, provider: MarketDataProvider) -> int:
        """Keep the provider's daily series current while the feed runs.

        Nothing but provider bars lives in it, so the last week is re-read and
        replaces the stored days, which carries the forming day and any revision;
        a missing series is loaded whole. Each refresh renews the TTL, so the
        series lasts as long as the feed does rather than until a restart.
        """
        written = 0
        for symbol in self.symbols:
            stored = await self._redis.exists(keys.history(symbol, "1d"))
            span = _DAILY_REFRESHED if stored else _DAILY_HISTORY
            written += await self._store_history(provider, symbol, "1d", span)
        return written

    async def _store_history(
        self, provider: MarketDataProvider, symbol: str, interval: str, span: timedelta
    ) -> int:
        end = datetime.now(UTC)
        try:
            bars = await provider.backfill(symbol, end - span, end, interval)
        except ProviderError as exc:
            log.warning("history.unavailable", symbol=symbol, interval=interval, error=str(exc))
            return 0
        if not bars:
            return 0
        key = keys.history(symbol, interval)
        epochs = [int(b.bucket.timestamp()) for b in bars]
        # One transaction, so no reader sees the replaced stretch missing.
        pipe = self._redis.pipeline(transaction=True)
        pipe.zremrangebyscore(key, min(epochs), max(epochs))
        # Chunked: one ZADD of thousands of members is a single huge command
        # that blocks the event loop on serialisation.
        for first in range(0, len(bars), 1_000):
            pipe.zadd(
                key,
                {
                    json.dumps(
                        {
                            "t": t,
                            "o": b.open,
                            "h": b.high,
                            "l": b.low,
                            "c": b.close,
                            "n": b.tick_count,
                            "src": str(b.source),
                        }
                    ): t
                    for b, t in zip(
                        bars[first : first + 1_000], epochs[first : first + 1_000], strict=True
                    )
                },
            )
        pipe.expire(key, keys.TTL_HISTORY_S)
        await pipe.execute()
        log.info("history.stored", symbol=symbol, interval=interval, bars=len(bars))
        return len(bars)

    async def restore_detectors(self) -> int:
        """Rehydrate every symbol's detector from Redis. Called on lease acquisition.

        Without this a promoted standby starts every symbol in NORMAL, spends
        ``min_samples`` bars re-learning a baseline FROM THE ELEVATED DATA, and
        then concludes that the crisis is normal. It reports NORMAL for the rest
        of the event and never emits the clear, so the stored history ends on a
        'stressed' row nothing will ever close. That is measured in
        tests/chaos/test_regime_pipeline.py - we had assumed the failure would be
        a duplicate escalation, which would have been noisier and far kinder.

        A missing or corrupt snapshot is not fatal: we log it and start cold,
        which is exactly what would have happened anyway. Refusing to start
        because a cache key is malformed would turn a degraded feed into no feed.
        """
        symbols = self.symbols
        if not symbols:
            return 0

        pipe = self._redis.pipeline(transaction=False)
        for symbol in symbols:
            pipe.get(keys.regime_state(symbol))
        raw_states = await pipe.execute()

        restored = 0
        for symbol, raw in zip(symbols, raw_states, strict=True):
            if not raw:
                continue
            try:
                snapshot: DetectorSnapshot = json.loads(raw)
                self._state[symbol].detector.restore(snapshot)
            except (ValueError, KeyError, TypeError) as exc:
                log.warning("regime.restore_failed", symbol=symbol, error=str(exc))
                continue
            restored += 1
            detector = self._state[symbol].detector
            REGIME_STATE.labels(symbol=symbol).set(
                1.0 if detector.regime is Regime.STRESSED else 0.0
            )
            log.info(
                "regime.restored",
                symbol=symbol,
                regime=str(detector.regime),
                seq=detector.trigger.seq,
                baseline_samples=detector.baseline.n,
                learned_hours=detector.profile.learned_hours,
            )

        # A seq identifies a transition in alert_events whichever feed issued it,
        # and the persister drops a reused one as a collision: never fall below
        # what a parked provider's detector has already used.
        async for key in self._redis.scan_iter(match=keys.archive("*", keys.regime_state("*"))):
            symbol = key.rsplit(":", 1)[1]
            raw = await self._redis.get(key)
            try:
                parked_seq = int(json.loads(raw or "")["trigger"]["seq"])
            except (TypeError, ValueError, KeyError):
                continue
            if symbol in self._state:
                trigger = self._state[symbol].detector.trigger
                trigger.seq = max(trigger.seq, parked_seq)

        REGIME_RESTORED.inc(restored)
        log.info("regime.rehydrated", restored=restored, of=len(symbols))
        return restored

    async def refresh_depth_metric(self) -> None:
        STREAM_DEPTH.set(await self._redis.xlen(keys.STREAM_TICKS))


def _bar_payload(bar: Bar) -> dict[str, object]:
    """A sealed bar as the history set stores it and the vol frame carries it."""
    return {
        "t": int(bar.bucket.timestamp()),
        "o": bar.open,
        "h": bar.high,
        "l": bar.low,
        "c": bar.close,
        "n": bar.tick_count,
        "src": str(bar.source),
    }


def _transition_payload(transition: RegimeTransition) -> dict[str, object]:
    """One wire format for both the stream and the channel.

    Two serialisations of the same event would drift, and the drift would only
    show up as "the dashboard and the database disagree about an alert" - the
    single worst place to discover a bug.
    """
    z = transition.trigger_value
    return {
        "s": transition.symbol,
        "seq": transition.seq,
        "ts": transition.ts.isoformat(),
        "old_regime": str(transition.old_regime),
        "new_regime": str(transition.new_regime),
        "trigger_value": z if math.isfinite(z) else None,
        "threshold_value": transition.threshold_value,
        "sigma": transition.sigma if math.isfinite(transition.sigma) else None,
        "cause": str(transition.cause),
        "reason": transition.reason,
    }
