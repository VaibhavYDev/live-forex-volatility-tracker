"""Worker entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
import signal

import structlog
from fx_platform import close_redis, configure_logging, make_redis, serve_metrics

from fx_worker.alerts import AlertPersister
from fx_worker.alerts import run_forever as run_alerts_forever
from fx_worker.config import WorkerSettings
from fx_worker.db import Database
from fx_worker.persister import Persister, run_forever

log = structlog.get_logger(__name__)


async def main() -> None:
    cfg = WorkerSettings()
    configure_logging("worker", cfg.log_level)
    serve_metrics(cfg.metrics_port)

    redis = make_redis(cfg.redis_url)
    db = Database(cfg.database_url)

    # Postgres may still be running its init scripts when the worker starts.
    # Retry rather than crash-looping: an orchestrator restart storm during
    # startup is noise that hides real failures.
    for attempt in range(30):
        try:
            await db.connect()
            break
        except OSError as exc:
            log.warning("db.waiting", attempt=attempt, error=str(exc))
            await asyncio.sleep(2.0)
    else:
        raise RuntimeError("could not reach PostgreSQL after 60s")

    persister = Persister(
        redis,
        db,
        consumer_name=cfg.consumer_name,
        bucket_seconds=cfg.bucket_seconds,
        flush_max_rows=cfg.flush_max_rows,
        flush_max_seconds=cfg.flush_max_seconds,
        claim_idle_ms=cfg.claim_idle_ms,
    )
    # Separate stream, separate consumer group, separate blast radius. A poison
    # tick that stalls bar persistence must not also stop alerts reaching the
    # database - those are the records a human gets paged about.
    alert_persister = AlertPersister(
        redis, db, consumer_name=cfg.consumer_name, claim_idle_ms=cfg.claim_idle_ms
    )

    def stop_all() -> None:
        persister.stop()
        alert_persister.stop()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_all)

    log.info("worker.starting", consumer=cfg.consumer_name)
    try:
        # A TaskGroup, not gather(): if one persister dies with a programming
        # error the other is cancelled and the process exits, so the orchestrator
        # restarts a whole worker. gather() would leave a half-dead worker that
        # looks healthy in a dashboard while silently persisting only half the
        # data - the failure mode that takes a week to notice.
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(run_forever(persister), name="bar-persister")
            tasks.create_task(run_alerts_forever(alert_persister), name="alert-persister")
    finally:
        # Flush before dying so a graceful restart loses nothing at all - the
        # PEL would have covered us, but only after the idle timeout.
        with contextlib.suppress(Exception):
            await persister.flush()
        await db.close()
        await close_redis(redis)
        log.info("worker.stopped")


def run() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())


if __name__ == "__main__":
    run()
