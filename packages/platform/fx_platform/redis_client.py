"""Redis connection factory.

``decode_responses=True`` throughout: every value we store is UTF-8 JSON or a
number, and threading ``bytes`` through the whole codebase to save one decode on
a 200-byte payload is a false economy that shows up as a bug in the parts of the
code you touch least.

``health_check_interval`` matters more than it looks. Cloud load balancers and
NAT gateways silently drop idle TCP connections after a few minutes, and a pooled
Redis connection can sit idle exactly that long. Without the periodic PING you
get a mystifying ``ConnectionResetError`` on the first command after a quiet
period - the kind of bug that only reproduces in production at 3am.
"""

from __future__ import annotations

import redis.asyncio as aioredis

_DEFAULT_TIMEOUT_S = 5.0


def make_redis(url: str, *, decode: bool = True) -> aioredis.Redis:
    return aioredis.Redis.from_url(
        url,
        decode_responses=decode,
        socket_timeout=_DEFAULT_TIMEOUT_S,
        socket_connect_timeout=_DEFAULT_TIMEOUT_S,
        socket_keepalive=True,
        health_check_interval=30,
        retry_on_timeout=True,
    )


async def close_redis(client: aioredis.Redis) -> None:
    await client.aclose()
