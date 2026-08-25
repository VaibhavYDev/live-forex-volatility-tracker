"""Process-wide singletons, wired once in the lifespan handler.

Plain module-level state rather than FastAPI ``Depends`` for these three: the
Redis connection, the pub/sub router and the ticket store are per-*process*
resources with a lifetime equal to the app's, not per-request objects. Modelling
them as request dependencies would imply they can vary per request, and the
``ConnectionManager`` in particular must not - there is exactly one Redis
subscription per replica, and that is the entire point of the fan-out design.

Tests override these by calling ``set_runtime`` with fakes.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from fx_api.config import ApiSettings
from fx_api.ws.manager import ConnectionManager
from fx_api.ws.tickets import TicketStore

_settings: ApiSettings | None = None
_redis: aioredis.Redis | None = None
_manager: ConnectionManager | None = None
_tickets: TicketStore | None = None


def set_runtime(
    settings: ApiSettings,
    redis: aioredis.Redis,
    manager: ConnectionManager,
    tickets: TicketStore,
) -> None:
    global _settings, _redis, _manager, _tickets
    _settings, _redis, _manager, _tickets = settings, redis, manager, tickets


def _require(value: object, name: str) -> None:
    if value is None:
        raise RuntimeError(f"{name} not initialised - is the app lifespan running?")


def get_settings() -> ApiSettings:
    _require(_settings, "settings")
    assert _settings is not None
    return _settings


def get_redis() -> aioredis.Redis:
    _require(_redis, "redis")
    assert _redis is not None
    return _redis


def get_manager() -> ConnectionManager:
    _require(_manager, "manager")
    assert _manager is not None
    return _manager


def get_tickets() -> TicketStore:
    _require(_tickets, "tickets")
    assert _tickets is not None
    return _tickets
