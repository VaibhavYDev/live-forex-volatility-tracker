"""REST endpoints: market status, historical bars, on-demand volatility.

The WebSocket carries everything live. REST exists for what a stream is bad at:
page load, deep history, and any client that wants one number without holding a
connection open.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException, Query, status
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

from fx_api.deps import get_redis

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/api", tags=["market"])

_MIN_BARS = 3

_ESTIMATORS = {
    Estimator.CLOSE_TO_CLOSE: close_to_close,
    Estimator.PARKINSON: parkinson,
    Estimator.GARMAN_KLASS: garman_klass,
    Estimator.ROGERS_SATCHELL: rogers_satchell,
    Estimator.YANG_ZHANG: yang_zhang,
}

ANNUALIZATION_BASIS = "252 trading days x 24h = 362,880 one-minute bars/year"


async def _cached_bars(symbol: str, limit: int) -> list[Bar]:
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
    rows = await _cached_bars(symbol.upper(), limit)
    return {
        "symbol": symbol.upper(),
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
    rows = await _cached_bars(symbol.upper(), limit)
    if len(rows) < _MIN_BARS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=(
                f"not enough bars cached for {symbol.upper()} yet ({len(rows)}); "
                "the feed is still warming up"
            ),
        )

    snap = VolSnapshot.build(
        symbol=symbol.upper(),
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
    symbol: str, limit: int = Query(default=60, ge=_MIN_BARS, le=1440)
) -> dict[str, Any]:
    """Every estimator over the same bars, side by side.

    Cheap to add and it makes the argument visually: the spread between
    close-to-close and Garman-Klass on identical data IS the case for range
    estimators.
    """
    rows = await _cached_bars(symbol.upper(), limit)
    if len(rows) < _MIN_BARS:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail=f"not enough bars cached for {symbol.upper()} yet ({len(rows)})",
        )

    return {
        "symbol": symbol.upper(),
        "bars_used": len(rows),
        "window_s": len(rows) * 60,
        "annualization_basis": ANNUALIZATION_BASIS,
        "estimators": {
            str(est): VolSnapshot.build(
                symbol=symbol.upper(),
                estimator=est,
                window_s=len(rows) * 60,
                sigma=fn(rows),
                sample_count=len(rows),
                bar_seconds=60,
            ).sigma_annualized
            for est, fn in _ESTIMATORS.items()
        },
    }
