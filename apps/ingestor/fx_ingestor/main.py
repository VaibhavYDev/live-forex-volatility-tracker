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
from datetime import UTC, datetime

import structlog
from fx_core import keys
from fx_platform import close_redis, configure_logging, make_redis, serve_metrics

from fx_ingestor.config import IngestorSettings
from fx_ingestor.leader import LeaderLease
from fx_ingestor.pipeline import IngestPipeline
from fx_ingestor.providers import ProviderAuthError, ProviderError, build_provider
from fx_ingestor.supervisor import BackoffPolicy, CircuitBreaker, StalenessWatchdog

log = structlog.get_logger(__name__)

# A session shorter than this did not work, whatever the socket said. Below it we
# treat a clean disconnect as a failed attempt: it escalates the backoff ladder
# and counts against the circuit breaker. See tests/unit/test_reconnect_ladder.py
# for what this costs a provider when it is missing.
MIN_HEALTHY_SESSION_S = 30.0


def _provider_kwargs(cfg: IngestorSettings) -> dict[str, object]:
    if cfg.provider == "replay":
        return {
            "ticks_per_sec": cfg.replay_ticks_per_sec,
            "seed": cfg.replay_seed,
            "burst_every_s": cfg.replay_burst_every_s,
        }
    return {"token": cfg.provider_token}


class Ingestor:
    def __init__(self, cfg: IngestorSettings) -> None:
        self.cfg = cfg
        self.redis = make_redis(cfg.redis_url)
        self.pipeline = IngestPipeline(
            self.redis,
            cfg.symbol_list,
            bucket_seconds=cfg.bucket_seconds,
            ewma_lambda=cfg.ewma_lambda,
            retention_s=cfg.stream_retention_s,
        )
        self.lease = LeaderLease(self.redis, cfg.lease_ttl_ms, cfg.lease_renew_ms)
        self.backoff = BackoffPolicy(cfg.backoff_base_s, cfg.backoff_cap_s)
        self.breaker = CircuitBreaker(cfg.breaker_fail_threshold, cfg.breaker_reset_s)
        self._shutdown = asyncio.Event()
        self._force_reconnect = asyncio.Event()
        self._last_status: dict[str, str] = {"state": "starting"}
        # Injected so the ladder can be tested without spending real minutes.
        self._monotonic: Callable[[], float] = time.monotonic

    async def _publish_status(self, state: str, detail: str = "") -> None:
        """Tell the UI the truth about the feed.

        A market dashboard that keeps rendering the last price with no indication
        the feed died is worse than one that shows nothing: it looks authoritative
        while being wrong. This is what drives the 'Data delayed' banner.
        """
        payload = {
            "state": state,
            "detail": detail,
            "provider": self.cfg.provider,
            "ts": datetime.now(UTC).isoformat(),
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
        provider = build_provider(
            self.cfg.provider, self.cfg.symbol_list, **_provider_kwargs(self.cfg)
        )

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
            await self._publish_status("healthy")
            self.pipeline.set_feed_stale(False)
            self._force_reconnect.clear()

            async with StalenessWatchdog(self.cfg.staleness_timeout_s, on_stale) as watchdog:
                async for tick in provider.stream():
                    watchdog.pet()
                    await self.pipeline.handle(tick)
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

    async def _heartbeat_loop(self) -> None:
        """Refresh metrics and the feed-status TTL.

        Only the leader heartbeats: a standby has no feed to report on, and two
        replicas writing conflicting status would make the UI flicker between
        healthy and degraded.
        """
        while not self._shutdown.is_set():
            with contextlib.suppress(Exception):
                await self.pipeline.refresh_depth_metric()
                if self.lease.is_leader:
                    await self.redis.set(
                        keys.FEED_STATUS,
                        json.dumps(self._last_status),
                        ex=keys.TTL_STATUS_S,
                    )
            await asyncio.sleep(5.0)

    async def run(self) -> None:
        serve_metrics(self.cfg.metrics_port)
        depth = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        try:
            while not self._shutdown.is_set():
                # Standby replicas block here until the leader dies. This is the
                # whole of the N-replica story: only the lease holder connects.
                async with self.lease.hold():
                    # Rehydrate BEFORE the first tick. A standby that starts
                    # cold re-baselines on whatever the market is doing right
                    # now - so if it is promoted mid-crisis it learns the crisis
                    # as "normal", reports NORMAL, and never emits the clear the
                    # stored 'stressed' row is waiting for.
                    await self.pipeline.restore_detectors()
                    await self._run_as_leader()
                if self.lease.lost.is_set() and not self._shutdown.is_set():
                    log.warning("leadership.lost_reentering_standby")
        finally:
            depth.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await depth
            await self._publish_status("stopped")
            await close_redis(self.redis)

    def request_shutdown(self) -> None:
        log.info("shutdown.requested")
        self._shutdown.set()


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
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, ingestor.request_shutdown)

    await ingestor.run()


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except ProviderAuthError:
        sys.exit(78)  # EX_CONFIG - a config problem, not a crash


if __name__ == "__main__":
    run()
