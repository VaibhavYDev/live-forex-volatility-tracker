"""FastAPI application.

Stateless by construction: every piece of shared state lives in Redis, so you can
run N replicas behind a load balancer and any of them can serve any request or
any WebSocket. The one process-level resource is the single Redis pub/sub
subscription that fans out to this replica's clients - see ``ws/manager.py``.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fx_platform import close_redis, configure_logging, make_redis
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from fx_api.config import ApiSettings
from fx_api.deps import set_runtime
from fx_api.routers import health, market, stream
from fx_api.ws.manager import ConnectionManager
from fx_api.ws.tickets import TicketStore

log = structlog.get_logger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = ApiSettings()
    configure_logging("api", settings.log_level)

    redis = make_redis(settings.redis_url)
    manager = ConnectionManager(redis)
    tickets = TicketStore(redis, settings.ws_ticket_ttl_s)
    set_runtime(settings, redis, manager, tickets)

    await manager.start()
    log.info("api.started", cors=settings.cors_list, ticket_required=settings.require_ws_ticket)
    try:
        yield
    finally:
        await manager.stop()
        await close_redis(redis)
        log.info("api.stopped")


def create_app() -> FastAPI:
    settings = ApiSettings()
    app = FastAPI(
        title="Live Forex Volatility Tracker",
        version="0.1.0",
        description=(
            "Fault-tolerant FX volatility streaming. "
            "See /docs for REST, and ws://<host>/ws/stream for the live feed."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(market.router)
    app.include_router(stream.router)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
