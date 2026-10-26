"""Liveness vs readiness - two different questions.

``/healthz`` (liveness): is this process wedged? Only a restart fixes that, so it
checks nothing external. A liveness probe that pings Redis will restart every
healthy API pod during a Redis blip - turning a partial outage into a total one.

``/readyz`` (readiness): should this replica receive traffic *right now*? This one
does check dependencies, plus feed freshness: a replica whose feed has gone stale
can still answer, but it would answer with old prices, so it takes itself out of
the load balancer instead.

Distinguishing these is a small thing that reviewers consistently notice, because
conflating them is one of the most common production mistakes there is.

``/api/health`` (monitoring): should a person be woken up? It is what an external
uptime monitor polls, so it sits under ``/api/`` where the web proxy routes it, and it
judges by the calendar: a closed market and the silent minutes of the 17:00 New York
rollover are healthy, a missing ingestor heartbeat or a worker that has stopped
draining the tick stream is not - weekday or weekend.
"""

from __future__ import annotations

from datetime import UTC, datetime

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Response, status
from fx_core import keys
from fx_core.calendar import in_rollover_window, is_market_open

from fx_api.deps import get_manager, get_redis, get_settings

log = structlog.get_logger(__name__)
router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/readyz")
async def readyz(response: Response) -> dict[str, object]:
    redis = get_redis()
    manager = get_manager()
    settings = get_settings()

    checks: dict[str, object] = {}

    try:
        await redis.ping()
        checks["redis"] = "ok"
    except (aioredis.RedisError, OSError) as exc:
        checks["redis"] = f"error: {exc}"

    # Read through to Redis rather than trusting the cached value: this replica
    # may have started after the last status change, and the key's TTL is the
    # authoritative liveness signal for the ingestor.
    try:
        feed = await manager.refresh_feed_status()
    except (aioredis.RedisError, OSError):
        feed = manager.feed_status
    checks["feed_state"] = feed.get("state", "unknown")
    feed_ts = feed.get("ts")
    age: float | None = None
    if isinstance(feed_ts, str):
        with_tz = datetime.fromisoformat(feed_ts)
        age = (datetime.now(UTC) - with_tz).total_seconds()
        checks["feed_age_s"] = round(age, 1)

    # "unknown" means the status key expired: no ingestor heartbeat for its TTL,
    # which is staler than the threshold below, not a reason to take traffic.
    ready = checks["redis"] == "ok" and feed.get("state") == "healthy"
    if age is not None and age > settings.feed_stale_after_s:
        ready = False
        checks["feed_state"] = "stale"

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    checks["clients"] = manager.client_count
    checks["ready"] = ready
    return checks


@router.get("/api/health")
async def monitor(response: Response) -> dict[str, object]:
    redis = get_redis()
    manager = get_manager()
    settings = get_settings()
    now = datetime.now(UTC)
    market_open = is_market_open(now)
    rollover = in_rollover_window(now)
    problems: list[str] = []

    try:
        await redis.ping()
    except (aioredis.RedisError, OSError):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"ok": False, "problems": ["redis unreachable"], "market_open": market_open}

    try:
        feed = await manager.refresh_feed_status()
    except (aioredis.RedisError, OSError):
        feed = manager.feed_status
    state = feed.get("state", "unknown")
    feed_ts = feed.get("ts")
    age = (
        (now - datetime.fromisoformat(feed_ts)).total_seconds()
        if isinstance(feed_ts, str)
        else None
    )
    # The heartbeat proves the ingestor is alive whether or not quotes arrive;
    # "connecting" is the normal state until the first quote of a session.
    if age is None or age > settings.feed_stale_after_s:
        problems.append("no ingestor heartbeat")
    elif market_open and state != "healthy" and not (state == "connecting" and rollover):
        problems.append(f"feed {state} while the market is open")

    worker: dict[str, int] | str
    try:
        groups = await redis.xinfo_groups(keys.STREAM_TICKS)
    except aioredis.ResponseError:
        groups = []
    group = next((g for g in groups if g.get("name") == keys.CONSUMER_GROUP), None)
    if group is None:
        worker = "absent"
        if settings.monitor_require_worker:
            problems.append("no tick persister group")
    else:
        lag = int(group.get("lag") or 0)
        worker = {"lag": lag, "pending": int(group.get("pending") or 0)}
        if lag > settings.monitor_max_worker_lag:
            problems.append(f"worker {lag} ticks behind")
    # Lag grows only as fast as ticks arrive, so a quiet market can hide a dead
    # worker for a long time; its lease expires within seconds.
    if settings.monitor_require_worker and not await redis.exists(keys.PERSISTER_LEASE):
        problems.append("no tick persister holds the lease")

    if problems:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "ok": not problems,
        "problems": problems,
        "market_open": market_open,
        "rollover": rollover,
        "feed": {"state": state, "age_s": None if age is None else round(age, 1)},
        "worker": worker,
        "clients": manager.client_count,
    }
