"""Single-writer election via a Redis lease.

The problem this solves
-----------------------
The moment you run more than one ingestor replica, each opens its own upstream
WebSocket. You burn N times your rate limit, ingest N copies of every tick, and
produce N slightly different volatility numbers. Horizontal scaling *breaks the
application*. Almost every tracker on GitHub has this bug and never notices,
because they only ever run one container.

So: exactly one replica holds the lease and connects. The rest sit in standby and
promote within ``ttl_ms`` if the leader dies.

What this is NOT
----------------
This is a **lease, not consensus**. A stop-the-world GC pause longer than the TTL
can produce two leaders for a moment, and no amount of Lua fixes that - it is the
scenario Kleppmann uses to critique Redlock ("How to do distributed locking",
2016). Reaching for a full Redlock implementation here would be worse: more code,
more failure modes, same fundamental limitation.

Instead we make the overlap **harmless**:

    bars_1m has PRIMARY KEY (symbol, bucket), and the persister upserts.

Two leaders briefly writing the same ticks produces duplicate stream entries that
collapse to the same row. That is the fencing story - achieved with a unique index
rather than a distributed lock, which is the trade a senior engineer actually makes.
Stating that limitation out loud is worth more than pretending it does not exist.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator

import redis.asyncio as aioredis
import structlog
from fx_core.keys import LEADER_LEASE

log = structlog.get_logger(__name__)

# Compare-and-set renewal. Doing this as GET-then-PEXPIRE would be a race: the
# lease could expire and be taken by another replica between the two commands,
# and we would then extend *someone else's* lease. Lua runs atomically.
_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

# Same reasoning for release: never DEL a lease you no longer own.
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


class LeaderLease:
    def __init__(
        self,
        redis: aioredis.Redis,
        ttl_ms: int = 10_000,
        renew_ms: int = 3_000,
        key: str = LEADER_LEASE,
    ) -> None:
        if renew_ms >= ttl_ms / 2:
            # Renewing at >= TTL/2 gives you one shot before expiry. One dropped
            # packet and you lose leadership while perfectly healthy.
            raise ValueError(
                f"renew_ms ({renew_ms}) must be well below ttl_ms/2 ({ttl_ms / 2}); "
                "at least two renewal attempts must fit inside one TTL"
            )
        self._redis = redis
        self._ttl_ms = ttl_ms
        self._renew_ms = renew_ms
        self._key = key
        self.instance_id = str(uuid.uuid4())
        self._is_leader = False
        self._lost = asyncio.Event()

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    @property
    def lost(self) -> asyncio.Event:
        """Set the instant renewal fails. The supervisor races its work against this."""
        return self._lost

    async def _acquire(self) -> bool:
        ok = await self._redis.set(self._key, self.instance_id, nx=True, px=self._ttl_ms)
        return bool(ok)

    async def _renew(self) -> bool:
        result = await self._redis.eval(
            _RENEW_LUA, 1, self._key, self.instance_id, str(self._ttl_ms)
        )
        return bool(result)

    async def _release(self) -> None:
        with contextlib.suppress(aioredis.RedisError):
            await self._redis.eval(_RELEASE_LUA, 1, self._key, self.instance_id)

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(self._renew_ms / 1000.0)
            try:
                renewed = await self._renew()
            except aioredis.RedisError as exc:
                log.warning("lease.renew_error", error=str(exc))
                renewed = False
            if not renewed:
                # We may have lost it to a network blip or a slow event loop.
                # Either way we must assume another replica now owns the feed.
                log.warning("lease.lost", instance=self.instance_id)
                self._is_leader = False
                self._lost.set()
                return

    @contextlib.asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        """Block until leadership is acquired, then hold it for the block's duration.

        On exit - normal or exceptional - the lease is released immediately rather
        than left to expire, so failover is sub-second on a clean shutdown instead
        of waiting out the full TTL.
        """
        while not await self._acquire():
            log.info("lease.standby", instance=self.instance_id, retry_in_s=1)
            await asyncio.sleep(1.0)

        self._is_leader = True
        self._lost.clear()
        log.info("lease.acquired", instance=self.instance_id, ttl_ms=self._ttl_ms)

        renewer = asyncio.create_task(self._renew_loop(), name="lease-renew")
        try:
            yield
        finally:
            renewer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewer
            self._is_leader = False
            await self._release()
            log.info("lease.released", instance=self.instance_id)
