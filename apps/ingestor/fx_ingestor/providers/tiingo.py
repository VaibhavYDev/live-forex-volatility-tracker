"""Tiingo FX WebSocket adapter.

Endpoint: ``wss://api.tiingo.com/fx``  (free plan includes the firehose)

The wire format is a **positional array**, which is exactly why this class exists:

    {"service": "fx", "messageType": "A",
     "data": ["Q", "eurusd", "2026-08-22T09:00:00.123Z", bidSize, bid, mid, ask, askSize]}
              0     1         2                          3        4    5    6    7

Positional payloads are brittle by construction - the provider can insert a field
and every naive consumer silently misreads every price after it. So we validate
length and types on every frame, drop and count anything that fails, and convert
to ``fx_core.Tick`` immediately. One bad frame must never kill the feed, and must
never reach the variance state, where a NaN would be permanent.

``messageType`` values: "A" = data, "I" = subscription info, "E" = error, "H" =
heartbeat. Heartbeats matter more than they look: they are how we distinguish a
quiet market from a dead socket. See ``fx_ingestor.supervisor``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import httpx
import structlog
import websockets
from fx_core.models import Bar, BarSource, Tick

from fx_ingestor.providers.base import (
    MarketDataProvider,
    ProviderAuthError,
    ProviderError,
)

log = structlog.get_logger(__name__)

WS_URL = "wss://api.tiingo.com/fx"
REST_URL = "https://api.tiingo.com/tiingo/fx/{symbol}/prices"

_QUOTE = "Q"
_EXPECTED_FIELDS = 8


class TiingoProvider(MarketDataProvider):
    name = "tiingo"
    supports_backfill = True

    def __init__(
        self,
        symbols: Sequence[str],
        token: str,
        threshold_level: int = 5,  # 5 = all top-of-book updates
    ) -> None:
        super().__init__(symbols)
        if not token:
            raise ProviderAuthError("FX_PROVIDER_TOKEN is required for the tiingo provider")
        self._token = token
        self._threshold = threshold_level
        self._ws: websockets.ClientConnection | None = None

    async def connect(self) -> None:
        try:
            self._ws = await websockets.connect(
                WS_URL,
                # Belt and braces: protocol-level ping AND the application-level
                # staleness watchdog in the supervisor. TCP will happily hold a
                # black-holed connection open for minutes; neither alone is enough.
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_queue=1024,
            )
        except OSError as exc:
            raise ProviderError(f"tiingo connect failed: {exc}") from exc

        await self._ws.send(
            json.dumps(
                {
                    "eventName": "subscribe",
                    "authorization": self._token,
                    "eventData": {
                        "thresholdLevel": self._threshold,
                        "tickers": [s.lower() for s in self.symbols],
                    },
                }
            )
        )
        log.info("tiingo.subscribed", symbols=self.symbols, threshold=self._threshold)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def stream(self) -> AsyncIterator[Tick]:
        if self._ws is None:
            raise ProviderError("stream() called before connect()")

        async for raw in self._ws:
            self.received_frames += 1
            tick = self._parse(raw)
            if tick is not None:
                yield tick

    def _parse(self, raw: str | bytes) -> Tick | None:
        """Positional array -> domain Tick. Returns None (and counts) on anything odd."""
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.dropped_frames += 1
            return None

        if not isinstance(msg, dict):
            self.dropped_frames += 1
            return None

        mtype = msg.get("messageType")
        if mtype == "E":
            detail = str(msg.get("response", msg))
            if "auth" in detail.lower() or "token" in detail.lower():
                raise ProviderAuthError(f"tiingo rejected credentials: {detail}")
            raise ProviderError(f"tiingo error frame: {detail}")
        if mtype != "A":
            return None  # "I" subscription info, "H" heartbeat

        data = msg.get("data")
        if not isinstance(data, list) or len(data) < _EXPECTED_FIELDS or data[0] != _QUOTE:
            self.dropped_frames += 1
            return None

        try:
            ts_event = datetime.fromisoformat(str(data[2]).replace("Z", "+00:00"))
            if ts_event.tzinfo is None:
                ts_event = ts_event.replace(tzinfo=UTC)
            tick = Tick(
                symbol=str(data[1]).upper(),
                bid=float(data[4]),
                ask=float(data[6]),
                ts_event=ts_event,
                ts_ingest=datetime.now(UTC),
            )
            tick.validate()
        except (TypeError, ValueError) as exc:
            self.dropped_frames += 1
            log.debug("tiingo.malformed_frame", error=str(exc))
            return None

        return tick

    async def backfill(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        """Fill a detected gap from the REST history endpoint.

        Everything returned is tagged ``BarSource.BACKFILL`` so the chart can
        render it distinctly. Provenance is the whole point: a number you cannot
        trace is a number you cannot trust.
        """
        params = {
            "startDate": start.date().isoformat(),
            "endDate": end.date().isoformat(),
            "resampleFreq": "1min",
            "token": self._token,
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(REST_URL.format(symbol=symbol.lower()), params=params)
            if resp.status_code == httpx.codes.UNAUTHORIZED:
                raise ProviderAuthError("tiingo rejected the token on the REST endpoint")
            resp.raise_for_status()
            rows = resp.json()

        bars: list[Bar] = []
        for row in rows:
            try:
                bucket = datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if not start <= bucket <= end:
                continue
            bars.append(
                Bar(
                    symbol=symbol.upper(),
                    bucket=bucket,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    tick_count=0,  # unknown from OHLC-only history; honest zero
                    sum_ret=0.0,
                    sum_ret_sq=0.0,
                    source=BarSource.BACKFILL,
                )
            )
        log.info("tiingo.backfilled", symbol=symbol, bars=len(bars), start=start, end=end)
        return bars
