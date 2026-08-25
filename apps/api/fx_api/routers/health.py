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
"""

from __future__ import annotations

from datetime import UTC, datetime

import redis.asyncio as aioredis
import structlog
from fastapi import APIRouter, Response, status

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

    ready = checks["redis"] == "ok" and feed.get("state") in ("healthy", "unknown")
    if age is not None and age > settings.feed_stale_after_s:
        ready = False
        checks["feed_state"] = "stale"

    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    checks["clients"] = manager.client_count
    checks["ready"] = ready
    return checks
