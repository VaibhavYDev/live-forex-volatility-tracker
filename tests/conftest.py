"""Shared fixtures.

Integration and chaos tests run against a REAL Redis rather than a mock. Mocking
Redis would mean mocking the exact semantics under test - consumer group pending
lists, XAUTOCLAIM idle timing, MINID trimming - which is to say it would prove
nothing. If Redis is not reachable, those tests skip loudly instead of passing
vacuously.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
import redis.asyncio as aioredis
from fx_worker.db import Database

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://fx:fx@localhost:5432/fxtest")


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
        pytest.skip(f"Redis not reachable at {REDIS_URL}: {exc}")

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
async def db() -> AsyncIterator[Database]:
    """A real PostgreSQL with the migrations applied.

    Skips loudly rather than passing vacuously when Postgres is unreachable: a
    test suite that quietly stops exercising the idempotency guarantee is worse
    than one that fails, because it keeps reporting green.
    """
    database = Database(DATABASE_URL)
    try:
        await database.connect(min_size=1, max_size=4)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL not reachable at {DATABASE_URL}: {exc}")

    async with database.pool.acquire() as conn:
        missing = not await conn.fetchval("SELECT to_regclass('public.alert_events')")
    if missing:
        await database.close()
        pytest.skip("alert_events is absent - apply infra/migrations/003_alert_events.sql")

    async with database.pool.acquire() as conn:
        await conn.execute("DELETE FROM alert_events")
    try:
        yield database
    finally:
        async with database.pool.acquire() as conn:
            await conn.execute("DELETE FROM alert_events")
        await database.close()
