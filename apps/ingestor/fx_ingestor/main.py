"""Ingestor entrypoint.

Reads top to bottom as the lifecycle it implements:

    acquire lease  ->  connect  ->  stream  ->  disconnect  ->  back off  ->  retry
         ^                                                                      |
         +----------------------------- lose lease ----------------------------+

Every failure path in ``docs/architecture.md``'s failure-mode matrix has a branch
here, and every branch has a test in ``tests/chaos/``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import asyncpg
import structlog
from fx_core import keys
from fx_platform import LeaderLease, close_redis, configure_logging, make_redis, serve_metrics
from pydantic import ValidationError
from redis.exceptions import RedisError

from fx_ingestor.config import IngestorSettings
from fx_ingestor.pipeline import IngestPipeline
from fx_ingestor.providers import (
    MarketDataProvider,
    ProviderAuthError,
    ProviderError,
    build_provider,
)
from fx_ingestor.quota import RequestQuota
from fx_ingestor.recovery import HistoryRecovery, RecoveryConfig, RecoveryStore
from fx_ingestor.supervisor import BackoffPolicy, CircuitBreaker, StalenessWatchdog

log = structlog.get_logger(__name__)

# A session shorter than this did not work, whatever the socket said. Below it we
# treat a clean disconnect as a failed attempt: it escalates the backoff ladder
# and counts against the circuit breaker. See tests/unit/test_reconnect_ladder.py
# for what this costs a provider when it is missing.
MIN_HEALTHY_SESSION_S = 30.0

# Feed status is re-stamped and re-published this often. It must divide
# keys.TTL_STATUS_S comfortably - the TTL expiring is how every replica learns
# the ingestor died without anyone sending a goodbye - and it must be well under
# the 60s staleness thresholds in /readyz and the browser banner, so a single
# missed write never reads as an outage.
HEARTBEAT_S = 5.0

# The provider's daily series is re-read this often while leading, so it stays
# current and never expires mid-run. Hourly is far inside Tiingo's request limits.
DAILY_HISTORY_REFRESH_S = 3600.0


class Ingestor:
    def __init__(self, cfg: IngestorSettings) -> None:
        self.cfg = cfg
        self.redis = make_redis(cfg.redis_url)
        self.quota = RequestQuota(self.redis, cfg.provider, cfg.quota_limits)
        self.pipeline = IngestPipeline(
            self.redis,
            cfg.symbol_list,
            bucket_seconds=cfg.bucket_seconds,
            ewma_lambda=cfg.ewma_lambda,
            retention_s=cfg.stream_retention_s,
            quota=self.quota,
        )
        self.lease = LeaderLease(
            self.redis, keys.LEADER_LEASE, cfg.lease_ttl_ms, cfg.lease_renew_ms
        )
        self.backoff = BackoffPolicy(cfg.backoff_base_s, cfg.backoff_cap_s)
        self.breaker = CircuitBreaker(cfg.breaker_fail_threshold, cfg.breaker_reset_s)
        self._shutdown = asyncio.Event()
        self._force_reconnect = asyncio.Event()
        started = datetime.now(UTC).isoformat()
        self._last_status: dict[str, str] = {
            "state": "starting",
            "detail": "",
            "provider": cfg.provider,
            "ts": started,
            "since": started,
        }
        # Injected so the ladder can be tested without spending real minutes.
        self._monotonic: Callable[[], float] = time.monotonic

    async def _publish_status(self, state: str, detail: str = "") -> None:
        """Tell the UI the truth about the feed.

        A market dashboard that keeps rendering the last price with no indication
        the feed died is worse than one that shows nothing: it looks authoritative
        while being wrong. This is what drives the 'Data delayed' banner.

        TWO TIMESTAMPS, BECAUSE THERE ARE TWO QUESTIONS
        -----------------------------------------------
        ``ts`` answers "when did the ingestor last confirm this?" - liveness. The
        heartbeat refreshes it, so an unchanging ``ts`` means we have stopped
        talking, not that nothing has happened.

        ``since`` answers "when did the state last change?" - so the UI can say
        "degraded for 12 minutes" rather than just "degraded".

        Collapsing the two is what broke this before: ``ts`` was stamped only on a
        state CHANGE and the heartbeat rewrote it verbatim, so a feed that
        connected once and streamed happily for hours reported an age of hours.
        Consumers read that as staleness, so the banner said "Stale" over live
        prices and /readyz returned 503 on every healthy replica.
        """
        now = datetime.now(UTC).isoformat()
        payload = {
            "state": state,
            "detail": detail,
            "provider": self.cfg.provider,
            "ts": now,
            "since": now if state != self._last_status.get("state") else self._since(),
        }
        self._last_status = payload
        with contextlib.suppress(Exception):
            pipe = self.redis.pipeline(transaction=True)
            # Stored for late joiners, published for immediacy. Both, not either.
            pipe.set(keys.FEED_STATUS, json.dumps(payload), ex=keys.TTL_STATUS_S)
            pipe.publish(keys.channel_status(), json.dumps(payload))
            await pipe.execute()

    async def _consume_once(self) -> None:
        """One connection lifetime. Returns cleanly when the socket closes."""
        provider = self._build_provider()

        async def on_stale() -> None:
            log.warning("feed.stale_forcing_reconnect")
            # Tell the regime detector to stop evaluating BEFORE we tear the
            # socket down. A frozen price reads as exactly zero volatility, so a
            # detector still running during a dead feed reliably reports
            # "volatility collapsed" - the most embarrassing false positive
            # available to a market dashboard.
            self.pipeline.set_feed_stale(True)
            self._force_reconnect.set()
            await provider.close()

        async with provider:
            # A socket is not a feed: "healthy" waits for the first real quote.
            await self._publish_status("connecting", "waiting for the first quote")
            self.pipeline.set_feed_stale(False)
            self._force_reconnect.clear()
            quoted = False

            async with StalenessWatchdog(self.cfg.staleness_timeout_s, on_stale) as watchdog:
                async for tick in provider.stream():
                    watchdog.pet()
                    await self.pipeline.handle(tick)
                    if not quoted:
                        quoted = True
                        await self._publish_status("healthy")
                    if self._shutdown.is_set() or self.lease.lost.is_set():
                        break

        log.info(
            "feed.disconnected",
            received=provider.received_frames,
            dropped=provider.dropped_frames,
        )

    async def _run_as_leader(self) -> None:
        attempt = 0
        while not self._shutdown.is_set() and not self.lease.lost.is_set():
            if not self.breaker.allows_attempt():
                await self._publish_status("degraded", "circuit breaker open")
                await asyncio.sleep(1.0)
                continue

            started = self._monotonic()
            try:
                await self._consume_once()
            except ProviderAuthError as exc:
                # Never retry a credential failure. An infinite backoff loop on a
                # 401 looks healthy on a dashboard while ingesting nothing, and
                # will get the key banned. Fail loudly.
                log.error("provider.auth_failed", error=str(exc))
                await self._publish_status("fatal", str(exc))
                self._shutdown.set()
                raise
            except (ProviderError, OSError) as exc:
                # A dropped socket ends almost every session, healthy or not, so a
                # session that worked first must still reset the ladder. Without
                # this, drops hours apart on a flaky network would climb it to the
                # cap and reopen the breaker after every drop past the fifth.
                if (self._monotonic() - started) >= MIN_HEALTHY_SESSION_S:
                    self.breaker.record_success()
                    attempt = 0
                self.breaker.record_failure()
                self.pipeline.set_feed_stale(True)
                await self._publish_status("degraded", str(exc))
                delay = self.backoff.delay(attempt)
                log.warning(
                    "feed.retrying",
                    attempt=attempt,
                    delay_s=round(delay, 2),
                    breaker=str(self.breaker.state),
                    error=str(exc),
                )
                attempt += 1
                await asyncio.sleep(delay)
            else:
                # A session that ended in milliseconds did not work, whatever the
                # socket reported. Resetting the ladder on ANY clean return meant
                # a provider stuck in accept-then-hangup was retried at the base
                # delay forever - roughly four times a second, against someone
                # else's infrastructure, with the breaker held closed by a
                # success recorded on connect.
                healthy = (self._monotonic() - started) >= MIN_HEALTHY_SESSION_S
                if healthy:
                    self.breaker.record_success()
                    attempt = 0
                else:
                    self.breaker.record_failure()
                    log.warning(
                        "feed.session_too_short",
                        lasted_s=round(self._monotonic() - started, 3),
                        attempt=attempt,
                        breaker=str(self.breaker.state),
                    )

                if not self._shutdown.is_set() and not self.lease.lost.is_set():
                    delay = self.backoff.delay(attempt)
                    if not healthy:
                        attempt += 1
                    await asyncio.sleep(delay)

    def _build_provider(self) -> MarketDataProvider:
        return build_provider(
            self.cfg.provider, self.cfg.symbol_list, token=self.cfg.provider_token
        )

    async def _rehydrate(self) -> None:
        """This provider's own past only: its parked state and its own history."""
        switched = await self.pipeline.adopt_provider(self.cfg.provider)
        if switched is not None and switched != ("unverified", 0):
            log.error(
                "provider.switched",
                previous=switched[0],
                current=self.cfg.provider,
                parked_keys=switched[1],
            )
        provider = self._build_provider()
        await self.pipeline.seed_history(provider)
        await self.pipeline.refresh_daily_history(provider)
        await self.pipeline.restore_detectors()

    async def _refresh_daily_history(self) -> None:
        """Runs beside the feed, so a slow history request never delays a tick."""
        while True:
            await asyncio.sleep(DAILY_HISTORY_REFRESH_S)
            try:
                await self.pipeline.refresh_daily_history(self._build_provider())
            except RedisError as exc:
                log.warning("history.refresh_failed", error=str(exc))

    async def _recover_history(self) -> None:
        """Fill missed minutes from the provider's history while leading.

        Beside the feed, like the daily refresh: it never delays a tick and never
        writes the live path (see fx_ingestor.recovery). An unreachable database or
        Redis only postpones the next run.
        """
        if self.cfg.recovery_mode == "off":
            return
        await asyncio.sleep(self.cfg.recovery_first_delay_s)
        pool: asyncpg.Pool | None = None
        recovery: HistoryRecovery | None = None
        trigger = "leadership"
        try:
            while True:
                try:
                    if pool is None:
                        pool = await asyncpg.create_pool(
                            self.cfg.database_url, min_size=1, max_size=2, command_timeout=120
                        )
                    if recovery is None:
                        recovery = HistoryRecovery(
                            store=RecoveryStore(pool, self.redis),
                            provider=self._build_provider,
                            provider_name=self.cfg.provider,
                            symbols=self.cfg.symbol_list,
                            instance=self.lease.instance_id,
                            config=RecoveryConfig(
                                lookback=timedelta(hours=self.cfg.recovery_lookback_h),
                                # Until the tick stream drops a minute's ticks, the worker
                                # may still persist it: recover only minutes older than that.
                                horizon=timedelta(seconds=self.cfg.stream_retention_s + 120),
                                max_requests=self.cfg.recovery_max_requests,
                                request_spacing_s=self.cfg.recovery_request_spacing_s,
                                days_per_request=self.cfg.recovery_days_per_request,
                            ),
                            stop=self.lease.lost,
                            quota=self.quota,
                        )
                    await recovery.run_once(trigger, apply=self.cfg.recovery_mode == "apply")
                    trigger = "periodic"
                except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, RedisError) as exc:
                    log.warning("recovery.unavailable", error=str(exc))
                await asyncio.sleep(self.cfg.recovery_interval_s)
        finally:
            if pool is not None:
                await pool.close()

    def _since(self) -> str:
        return self._last_status.get("since", self._last_status.get("ts", ""))

    async def _heartbeat_loop(self) -> None:
        """Refresh metrics, and re-stamp the feed status.

        Only the leader heartbeats: a standby has no feed to report on, and two
        replicas writing conflicting status would make the UI flicker between
        healthy and degraded.

        The re-stamp is the point. This loop IS the liveness signal, so writing
        the previous payload back verbatim - original timestamp and all - meant
        the age consumers computed was "time since the last state change", which
        on a healthy feed grows forever. It is also PUBLISHED, not only stored:
        a browser that connected an hour ago has no other way to learn that we
        are still here, and its banner would go stale on a feed that never
        missed a tick.
        """
        while not self._shutdown.is_set():
            with contextlib.suppress(Exception):
                await self.pipeline.refresh_depth_metric()
                if self.lease.is_leader:
                    self._last_status = {
                        **self._last_status,
                        "ts": datetime.now(UTC).isoformat(),
                    }
                    body = json.dumps(self._last_status)
                    pipe = self.redis.pipeline(transaction=True)
                    pipe.set(keys.FEED_STATUS, body, ex=keys.TTL_STATUS_S)
                    pipe.publish(keys.channel_status(), body)
                    await pipe.execute()
            await asyncio.sleep(HEARTBEAT_S)

    async def run(self) -> None:
        serve_metrics(self.cfg.metrics_port)
        depth = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        try:
            while not self._shutdown.is_set():
                # Standby replicas block here until the leader dies. This is the
                # whole of the N-replica story: only the lease holder connects.
                async with self.lease.hold():
                    refresher: asyncio.Task[None] | None = None
                    recovery: asyncio.Task[None] | None = None
                    try:
                        # Rehydrate BEFORE the first tick. A standby that starts
                        # cold re-baselines on whatever the market is doing right
                        # now - so if it is promoted mid-crisis it learns the crisis
                        # as "normal", reports NORMAL, and never emits the clear the
                        # stored 'stressed' row is waiting for.
                        await self._rehydrate()
                        refresher = asyncio.create_task(self._refresh_daily_history())
                        recovery = asyncio.create_task(
                            self._recover_history(), name="history-recovery"
                        )
                        recovery.add_done_callback(_report_recovery_failure)
                        await self._run_as_leader()
                    finally:
                        if recovery is not None:
                            recovery.cancel()
                            # A failure was logged when it happened; it must not end the feed.
                            await asyncio.gather(recovery, return_exceptions=True)
                        if refresher is not None:
                            refresher.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await refresher
                        # Said while the lease is still ours. From a standby, or once
                        # the lease has passed on, "stopped" would contradict the
                        # replica that now owns the feed.
                        if not self.lease.lost.is_set():
                            await self._publish_status("stopped")
                if self.lease.lost.is_set() and not self._shutdown.is_set():
                    log.warning("leadership.lost_reentering_standby")
        finally:
            depth.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await depth
            await close_redis(self.redis)

    def request_shutdown(self) -> None:
        log.info("shutdown.requested")
        self._shutdown.set()


def _report_recovery_failure(task: asyncio.Task[None]) -> None:
    """A defect in recovery stops recovery, loudly, and leaves the live feed running."""
    if not task.cancelled() and (exc := task.exception()) is not None:
        log.error("recovery.crashed", error=repr(exc), exc_info=exc)


async def main() -> None:
    cfg = IngestorSettings()
    configure_logging("ingestor", cfg.log_level)
    log.info(
        "ingestor.starting",
        provider=cfg.provider,
        symbols=cfg.symbol_list,
        bucket_seconds=cfg.bucket_seconds,
    )

    ingestor = Ingestor(cfg)
    work = asyncio.create_task(ingestor.run(), name="ingestor")
    requested = False

    def stop() -> None:
        nonlocal requested
        requested = True
        ingestor.request_shutdown()
        # The flag alone is not enough. A standby polling for the lease, a backoff
        # sleep and a socket with nothing to say - all weekend, while the market is
        # closed - never look at it, and Docker kills the process ten seconds after
        # SIGTERM. Cancelling unwinds each of them through the same finally blocks:
        # status published, lease released, Redis closed.
        work.cancel()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop)

    try:
        await work
    except asyncio.CancelledError:
        # Our own cancel, on request, is the clean exit. Any other is not ours to swallow.
        if not requested:
            raise


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except ValidationError as exc:
        print(f"ingestor not started: {exc}", file=sys.stderr)
        sys.exit(78)  # EX_CONFIG
    except ProviderAuthError:
        sys.exit(78)  # EX_CONFIG - a config problem, not a crash


if __name__ == "__main__":
    run()
