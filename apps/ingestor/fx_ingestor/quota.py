"""One count of the provider REST requests made with the shared token.

The account allows a fixed number of history requests per rolling hour and per
day (Tiingo's Starter plan: 50 and 1,000), and all of them come from one token:
the leader's history load when it takes the lease, its hourly daily-bar refresh,
history recovery and the recovery CLI. A per-run cap cannot see the others, and a
429 arrives only once the request the live feed needed has already been refused.

So every request is entered in one Redis ledger, whichever replica or process
makes it. Live collection is never refused: its requests are recorded and made.
Recovery takes a request only while the rolling hour and day leave the reserve
untouched, and otherwise waits for a later run.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import redis.asyncio as aioredis
import structlog
from fx_core import keys
from prometheus_client import Counter
from redis.exceptions import RedisError

log = structlog.get_logger(__name__)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
_UNCAPPED = 10**12

REQUESTS = Counter(
    "fx_provider_requests_total",
    "Provider REST requests by caller, and background ones deferred to keep the reserve",
    ["caller", "outcome"],
)

# KEYS[1] = ledger; ARGV = now (ms), hour cap, day cap, member. Forgets requests
# older than a day, then enters this one if both windows are under their caps.
# One script, so concurrent callers in any process cannot both take the last slot.
_TAKE_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - 86400000)
local hour = redis.call('ZCOUNT', key, now - 3600000, '+inf')
local day = redis.call('ZCARD', key)
if hour >= tonumber(ARGV[2]) or day >= tonumber(ARGV[3]) then
    return {0, hour, day}
end
redis.call('ZADD', key, now, ARGV[4])
return {1, hour + 1, day + 1}
"""


@dataclass(frozen=True, slots=True)
class QuotaLimits:
    """The account's allowance, and the part of it background work must leave alone."""

    per_hour: int
    per_day: int
    reserve_per_hour: int
    reserve_per_day: int


class RequestQuota:
    def __init__(
        self,
        redis: aioredis.Redis,
        provider: str,
        limits: QuotaLimits,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._key = keys.provider_requests(provider)
        self.limits = limits
        self._clock = clock

    async def record(self, caller: str) -> None:
        """A request the live feed makes: counted, never refused, never held up by Redis."""
        try:
            _, hour, day = await self._enter(caller, _UNCAPPED, _UNCAPPED)
        except RedisError as exc:
            log.warning("quota.unrecorded", caller=caller, error=str(exc))
            return
        REQUESTS.labels(caller=caller, outcome="made").inc()
        if hour > self.limits.per_hour or day > self.limits.per_day:
            log.warning("quota.over_limit", caller=caller, hour=hour, day=day)

    async def take(self, caller: str) -> bool:
        """One request for background work, granted only if the reserve stays untouched."""
        hour_cap = self.limits.per_hour - self.limits.reserve_per_hour
        day_cap = self.limits.per_day - self.limits.reserve_per_day
        try:
            granted, hour, day = await self._enter(caller, hour_cap, day_cap)
        except RedisError as exc:
            # Without the ledger nothing proves the request is spare.
            log.warning("quota.unavailable", caller=caller, error=str(exc))
            return False
        if not granted:
            REQUESTS.labels(caller=caller, outcome="deferred").inc()
            log.info("quota.deferred", caller=caller, hour=hour, day=day)
            return False
        REQUESTS.labels(caller=caller, outcome="made").inc()
        return True

    async def used(self) -> tuple[int, int]:
        """Requests in the rolling hour and the rolling day."""
        now = int(self._clock() * 1000)
        hour = await self._redis.zcount(self._key, now - HOUR_MS, "+inf")
        day = await self._redis.zcount(self._key, now - DAY_MS, "+inf")
        return int(hour), int(day)

    async def _enter(self, caller: str, hour_cap: int, day_cap: int) -> tuple[bool, int, int]:
        now = int(self._clock() * 1000)
        reply = cast(
            list[int],
            await self._redis.eval(
                _TAKE_LUA, 1, self._key, now, hour_cap, day_cap, f"{caller}:{uuid.uuid4().hex}"
            ),
        )
        granted, hour, day = (int(x) for x in reply)
        return bool(granted), hour, day
