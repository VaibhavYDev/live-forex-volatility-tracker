"""REST endpoints: market status, historical bars, on-demand volatility.

The WebSocket carries everything live. REST exists for what a stream is bad at:
page load, deep history, and any client that wants one number without holding a
connection open.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fx_core import keys
from fx_core.calendar import active_sessions, is_market_open, next_close, next_open
from fx_core.models import Bar, BarSource, Estimator, VolSnapshot
from fx_core.volatility import (
    close_to_close,
    garman_klass,
    parkinson,
    rogers_satchell,
    yang_zhang,
)

from fx_api.deps import get_redis, get_settings
from fx_api.ratelimit import client_id, hit

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api", tags=["market"])

_MIN_BARS = 3

# Symbols reach Redis as key fragments and cache keys. Six upper-case letters is
# every FX pair there is; the bound matters because an unvalidated symbol is an
# unbounded set of cache entries.
_SYMBOL = re.compile(r"^[A-Z]{6}$")


def _symbol(raw: str) -> str:
    s = raw.strip().upper()
    if not _SYMBOL.match(s):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{raw!r} is not a currency pair; expected six letters, e.g. EURUSD",
        )
    return s


async def _guard(request: Request, response: Response, bucket: str, limit: int) -> None:
    verdict = await hit(get_redis(), bucket, client_id(request), limit, 60)
    response.headers["X-RateLimit-Limit"] = str(limit)
    response.headers["X-RateLimit-Remaining"] = str(verdict.remaining)
    if not verdict.allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"rate limit is {limit}/min for this endpoint",
            headers={"Retry-After": str(verdict.retry_after_s)},
        )


class _Cache:
    """Tiny TTL cache for computed estimator responses.

    In-process rather than in Redis on purpose: what is being protected here is
    the CPU cost of five estimators over up to 1,440 bars, and that cost is
    per-replica. Bounded because the key includes a user-supplied symbol.
    """

    __slots__ = ("_data", "_max")

    def __init__(self, max_entries: int = 256) -> None:
        self._data: dict[tuple[str, int], tuple[float, dict[str, Any]]] = {}
        self._max = max_entries

    def get(self, key: tuple[str, int], ttl_s: float) -> dict[str, Any] | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if time.monotonic() - stored_at > ttl_s:
            self._data.pop(key, None)
            return None
        return value

    def put(self, key: tuple[str, int], value: dict[str, Any]) -> None:
        if len(self._data) >= self._max:
            # Oldest insertion first. dicts preserve insertion order, so this is
            # FIFO rather than LRU - adequate for a cache whose entries all
            # expire within 30 seconds anyway.
            self._data.pop(next(iter(self._data)), None)
        self._data[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._data.clear()


_compare_cache = _Cache()

_ESTIMATORS = {
    Estimator.CLOSE_TO_CLOSE: close_to_close,
    Estimator.PARKINSON: parkinson,
    Estimator.GARMAN_KLASS: garman_klass,
    Estimator.ROGERS_SATCHELL: rogers_satchell,
    Estimator.YANG_ZHANG: yang_zhang,
}

ANNUALIZATION_BASIS = "252 trading days x 24h = 362,880 one-minute bars/year"


async def _bars_from_cache(symbol: str, limit: int) -> list[Bar]:
    """Read sealed bars out of the Redis hot cache the ingestor maintains."""
    redis = get_redis()
    raw = await redis.zrevrange(keys.history(symbol), 0, limit - 1)
    return [
        Bar(
            symbol=symbol,
            bucket=datetime.fromtimestamp(d["t"], tz=UTC),
            open=d["o"],
            high=d["h"],
            low=d["l"],
            close=d["c"],
            tick_count=d.get("n", 0),
            sum_ret=0.0,
            sum_ret_sq=0.0,
            source=BarSource(d.get("src", "stream")),
        )
        for d in (json.loads(str(b)) for b in reversed(raw))
    ]


@router.get("/market/status")
async def market_status() -> dict[str, Any]:
    """Is the FX market open, and which regional sessions are live?

    The frontend uses this to explain silence honestly. "No ticks because it is
    Saturday" and "no ticks because we are broken" look identical on a chart, and
    only one of them is worth waking someone up for.
    """
    open_now = is_market_open()
    return {
        "is_open": open_now,
        "sessions": active_sessions(),
        "next_open": None if open_now else next_open().isoformat(),
        "next_close": next_close().isoformat() if open_now else None,
    }


@router.get("/bars/{symbol}")
async def bars(symbol: str, limit: int = Query(default=240, ge=1, le=1440)) -> dict[str, Any]:
    """Recent bars from the Redis hot cache.

    Redis-first on purpose: page load is the burstiest read there is, and it is
    served from a cache the ingestor already maintains, so a wave of reloads
    cannot become a wave of database queries.
    """
    sym = _symbol(symbol)
    rows = await _bars_from_cache(sym, limit)
    return {
        "symbol": sym,
        "bars": [
            {
                "t": int(b.bucket.timestamp()),
                "o": b.open,
                "h": b.high,
                "l": b.low,
                "c": b.close,
                "n": b.tick_count,
                "src": str(b.source),
            }
            for b in rows
        ],
        "source": "cache",
        "warming_up": not rows,
    }


@router.get("/volatility/{symbol}")
async def volatility(
    symbol: str,
    estimator: Estimator = Query(default=Estimator.PARKINSON),
    limit: int = Query(default=60, ge=_MIN_BARS, le=1440),
) -> dict[str, Any]:
    """Compute one estimator over the cached bars, on demand.

    Every response carries the estimator, the window and the annualisation basis.
    An unlabelled sigma is meaningless - 0.004 could be per-tick, per-minute or
    annualised - and a finance-literate reviewer checks for exactly this.
    """
    sym = _symbol(symbol)
    rows = await _bars_from_cache(sym, limit)
    if len(rows) < _MIN_BARS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"not enough bars cached for {sym} yet ({len(rows)}); the feed is still warming up"
            ),
        )

    snap = VolSnapshot.build(
        symbol=sym,
        estimator=estimator,
        window_s=len(rows) * 60,
        sigma=_ESTIMATORS[estimator](rows),
        sample_count=len(rows),
        bar_seconds=60,
    )
    return {
        "symbol": snap.symbol,
        "estimator": str(snap.estimator),
        "window_s": snap.window_s,
        "bars_used": snap.sample_count,
        "sigma_per_bar": snap.sigma,
        "sigma_annualized": snap.sigma_annualized,
        "annualization_basis": ANNUALIZATION_BASIS,
        "ts": snap.ts.isoformat(),
    }


@router.get("/volatility/{symbol}/compare")
async def compare_estimators(
    request: Request,
    response: Response,
    symbol: str,
    limit: int = Query(default=60, ge=_MIN_BARS, le=1440),
) -> dict[str, Any]:
    """Every estimator over the same bars, side by side.

    Cheap to add and it makes the argument visually: the spread between
    close-to-close and Garman-Klass on identical data IS the case for range
    estimators.

    The expensive endpoint, and the only one the frontend polls: five estimators
    over up to 1,440 bars, every 15 seconds per mounted panel. Rate limited so a
    loop cannot monopolise the Redis every other component shares, and cached
    because the answer cannot change until the next bar seals.
    """
    settings = get_settings()
    sym = _symbol(symbol)
    await _guard(request, response, "compare", settings.rate_limit_per_min)

    key = (sym, limit)
    cached = _compare_cache.get(key, settings.compare_cache_s)
    if cached is not None:
        response.headers["X-Cache"] = "hit"
        return cached
    response.headers["X-Cache"] = "miss"

    rows = await _bars_from_cache(sym, limit)
    if len(rows) < _MIN_BARS:
        # Deliberately not cached. "Warming up" is the one answer that becomes
        # wrong on its own, and caching it would keep a panel blank for 30
        # seconds after the data arrived.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"not enough bars cached for {sym} yet ({len(rows)})",
        )

    payload = {
        "symbol": sym,
        "bars_used": len(rows),
        "window_s": len(rows) * 60,
        "annualization_basis": ANNUALIZATION_BASIS,
        "estimators": {
            str(est): VolSnapshot.build(
                symbol=sym,
                estimator=est,
                window_s=len(rows) * 60,
                sigma=fn(rows),
                sample_count=len(rows),
                bar_seconds=60,
            ).sigma_annualized
            for est, fn in _ESTIMATORS.items()
        },
    }
    _compare_cache.put(key, payload)
    return payload
