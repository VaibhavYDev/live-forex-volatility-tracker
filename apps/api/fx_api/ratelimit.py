"""Fixed-window rate limiting, in Redis.

WHY REDIS AND NOT A PROCESS-LOCAL DICT
--------------------------------------
The API layer is stateless by construction - that is the entire reason tickets
live in Redis and the fan-out is one subscription per replica. A process-local
limiter would silently multiply the effective limit by the replica count and
would reset every deploy, which makes the number in SECURITY.md a fiction. Two
pipelined commands is a price worth paying to keep the claim true.

WHY FIXED WINDOW AND NOT A TOKEN BUCKET
---------------------------------------
A fixed window allows up to 2x the limit across a window boundary. For an
endpoint whose purpose is to stop one client monopolising Redis and CPU, that
factor of two does not change the outcome, and the alternative costs a Lua script
and a sorted set per client. Stated here rather than discovered later.
"""

from __future__ import annotations

from dataclasses import dataclass

import redis.asyncio as aioredis
from prometheus_client import Counter
from starlette.requests import Request

RATE_LIMITED = Counter("fx_api_rate_limited_total", "Requests rejected by the limiter", ["route"])

_PREFIX = "rl:"


def client_id(request: Request) -> str:
    """Best-effort client identity for the limiter.

    X-Forwarded-For is trusted only for its first hop, and only because this sits
    behind a reverse proxy in every deployment that matters. It is spoofable by
    anyone talking to the app directly, which is acceptable for a limiter whose
    job is protecting Redis from an accidental loop rather than defeating a
    determined attacker. Anything stronger needs real authentication, which is
    the same gap SECURITY.md records against ticket issuance.
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    remaining: int
    retry_after_s: int


async def hit(
    redis: aioredis.Redis, bucket: str, client: str, limit: int, window_s: int
) -> Verdict:
    """Count one request against ``client``'s budget for ``bucket``."""
    key = f"{_PREFIX}{bucket}:{client}"
    pipe = redis.pipeline(transaction=True)
    pipe.incr(key)
    pipe.ttl(key)
    count, ttl = await pipe.execute()

    # INCR is atomic, so exactly one caller ever observes 1 and there is no race
    # to set the expiry. A key with no TTL means that EXPIRE was lost to a
    # crash between the two commands; re-arming it is cheap and prevents a
    # client being locked out permanently.
    if count == 1 or ttl is None or ttl < 0:
        await redis.expire(key, window_s)
        ttl = window_s

    if count > limit:
        RATE_LIMITED.labels(route=bucket).inc()
        return Verdict(False, 0, max(int(ttl), 1))
    return Verdict(True, limit - int(count), max(int(ttl), 1))
