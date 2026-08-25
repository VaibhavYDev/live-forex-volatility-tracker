"""Shared fixtures.

Integration and chaos tests run against a REAL Redis rather than a mock. Mocking
Redis would mean mocking the exact semantics under test - consumer group pending
lists, XAUTOCLAIM idle timing, MINID trimming - which is to say it would prove
nothing.

SKIPPING IS A LOCAL CONVENIENCE, NOT A CI BEHAVIOUR
---------------------------------------------------
Locally, an unreachable service skips its tests so you can work on the pure-maths
core without Docker running. In CI that is a trap: a service container that fails
its health check, a renamed port or a mistyped environment variable would reduce
the suite to the unit tier and the build would still go green - 54 tests quietly
not running, with nothing in the output that a human would notice.

So ``FX_REQUIRE_SERVICES=1`` (set by the workflow) turns every one of those skips
into a hard failure. The rule is that CI must never be able to pass by testing
less than it thinks it is.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import asyncpg
import pytest
import redis as redis_sync_mod
import redis.asyncio as aioredis
from fx_worker.db import Database

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://fx:fx@localhost:5432/fxtest")


def _services_required() -> bool:
    return os.environ.get("FX_REQUIRE_SERVICES", "").strip() not in ("", "0", "false", "no")


def _unavailable(what: str, detail: str) -> None:
    """Skip locally, fail in CI. Never returns."""
    msg = (
        f"{what} unavailable: {detail}. "
        "FX_REQUIRE_SERVICES=1 is set, so this is a failure rather than a skip - "
        "the service container did not come up."
    )
    if _services_required():
        pytest.fail(msg, pytrace=False)
    pytest.skip(f"{what} unavailable: {detail}")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def redis() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except (aioredis.RedisError, OSError) as exc:
        await client.aclose()
        _unavailable(f"Redis at {REDIS_URL}", str(exc))

    # DB 15 is the test scratch database. Flushing it is safe and makes every
    # test start from a known state - essential when the thing under test is
    # recovery from leftover pending entries.
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest.fixture
def redis_sync() -> Iterator[Any]:
    """Synchronous Redis, for the FastAPI ``TestClient`` tests.

    TestClient drives the app on its own event loop; seeding fixtures from an
    async client in a different loop is how you get "attached to a different
    loop" errors that look like flakiness. A sync client sidesteps the question
    entirely, and the app under test still uses the async one.
    """
    client = redis_sync_mod.Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        client.ping()
    except (redis_sync_mod.RedisError, OSError) as exc:
        client.close()
        _unavailable(f"Redis at {REDIS_URL}", str(exc))

    client.flushdb()
    try:
        yield client
    finally:
        client.flushdb()
        client.close()


@pytest.fixture
async def db() -> AsyncIterator[Database]:
    """A real PostgreSQL with the migrations applied.

    A suite that quietly stops exercising the idempotency guarantee is worse than
    one that fails, because it keeps reporting green - hence ``_unavailable``.
    """
    database = Database(DATABASE_URL)
    try:
        await database.connect(min_size=1, max_size=4)
    except (OSError, asyncpg.PostgresError) as exc:
        _unavailable(f"PostgreSQL at {DATABASE_URL}", str(exc))

    async with database.pool.acquire() as conn:
        missing = not await conn.fetchval("SELECT to_regclass('public.alert_events')")
    if missing:
        await database.close()
        _unavailable("alert_events table", "apply infra/migrations/003_alert_events.sql")

    async with database.pool.acquire() as conn:
        await conn.execute("DELETE FROM alert_events")
    try:
        yield database
    finally:
        async with database.pool.acquire() as conn:
            await conn.execute("DELETE FROM alert_events")
        await database.close()
