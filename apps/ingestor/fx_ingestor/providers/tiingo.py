"""Tiingo FX WebSocket adapter.

Endpoint: ``wss://api.tiingo.com/fx``  (free plan includes the firehose)

The wire format is a **positional array**, which is exactly why this class exists:

    {"service": "fx", "messageType": "A",
     "data": ["Q", "eurusd", "2026-08-22T09:00:00.123Z", bidSize, bid, mid, askSize, ask]}
              0     1         2                          3        4    5    6        7

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
import math
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime

import httpx
import structlog
import websockets
from fx_core.models import Bar, BarSource, Tick

from fx_ingestor.providers.base import (
    MarketDataProvider,
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderError,
    ProviderRateLimitError,
)

log = structlog.get_logger(__name__)

WS_URL = "wss://api.tiingo.com/fx"
REST_URL = "https://api.tiingo.com/tiingo/fx/{symbol}/prices"

_QUOTE = "Q"
_EXPECTED_FIELDS = 8
_RESAMPLE = {"1m": "1min", "1d": "1day"}
# Tiingo serves FX history from this date and rejects an earlier startDate with
# HTTP 400, which would lose the whole request rather than just the older part.
_HISTORY_START = datetime(2020, 1, 1, tzinfo=UTC)


def _retry_after(header: str | None) -> float | None:
    """Seconds from a Retry-After header, when it is the delta-seconds form."""
    try:
        seconds = float(header) if header is not None else None
    except ValueError:
        return None  # the HTTP-date form: no hint rather than a wrong one
    return seconds if seconds is not None and seconds >= 0 else None


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
        except (OSError, websockets.InvalidHandshake) as exc:
            # A timeout during the HTTP upgrade is already an OSError; a socket
            # that dies mid-upgrade, or a 5xx from Tiingo, is InvalidHandshake.
            raise ProviderError(f"tiingo connect failed: {exc}") from exc

        try:
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
        except websockets.ConnectionClosed as exc:
            raise ProviderError(self._closed_detail(exc)) from exc
        log.info("tiingo.subscribed", symbols=self.symbols, threshold=self._threshold)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def stream(self) -> AsyncIterator[Tick]:
        ws = self._ws
        if ws is None:
            raise ProviderError("stream() called before connect()")

        # recv(), not ``async for``: the iterator ends silently on a clean close,
        # which the supervisor would take for a session that went well. And a
        # websockets exception is not one it retries: a keepalive timeout on a
        # flaky network used to escape it and take the whole process down.
        while True:
            try:
                raw = await ws.recv()
            except websockets.ConnectionClosed as exc:
                raise ProviderError(self._closed_detail(exc)) from exc
            self.received_frames += 1
            tick = self._parse(raw)
            if tick is not None:
                yield tick

    def _closed_detail(self, exc: websockets.ConnectionClosed) -> str:
        # The close reason is free text chosen by the other end; nothing promises
        # it does not echo the subscribe frame, and this string reaches the logs
        # and the status banner.
        return f"tiingo connection closed: {exc}".replace(self._token, "<redacted>")

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
                ask=float(data[7]),
                ts_event=ts_event,
                ts_ingest=datetime.now(UTC),
            )
            tick.validate()
            # The frame carries its own mid: a field read from the wrong position
            # disagrees with it, where reading askSize as the ask once passed every
            # other check and stored USDJPY at 500079.
            if not math.isclose(tick.mid, float(data[5]), rel_tol=1e-5):
                raise ValueError(f"mid {data[5]} disagrees with bid {tick.bid} / ask {tick.ask}")
        except (TypeError, ValueError) as exc:
            self.dropped_frames += 1
            log.debug("tiingo.malformed_frame", error=str(exc))
            return None

        return tick

    async def backfill(
        self, symbol: str, start: datetime, end: datetime, interval: str = "1m"
    ) -> list[Bar]:
        """Fill a detected gap from the REST history endpoint.

        Everything returned is tagged ``BarSource.BACKFILL`` so the chart can
        render it distinctly. Provenance is the whole point: a number you cannot
        trace is a number you cannot trust.
        """
        start = max(start, _HISTORY_START)
        params = {
            "startDate": start.date().isoformat(),
            "endDate": end.date().isoformat(),
            "resampleFreq": _RESAMPLE[interval],
        }
        # A header, not a query parameter: request URLs end up in HTTP client logs.
        headers = {"Authorization": f"Token {self._token}"}
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(
                    REST_URL.format(symbol=symbol.lower()), params=params, headers=headers
                )
            if resp.status_code == httpx.codes.UNAUTHORIZED:
                raise ProviderAuthError("tiingo rejected the token on the REST endpoint")
            if resp.status_code == httpx.codes.FORBIDDEN:
                # A valid token without the entitlement. Asking again will not help.
                raise ProviderEntitlementError(
                    "tiingo history unavailable: HTTP 403, this account may not read it"
                )
            if resp.status_code == httpx.codes.TOO_MANY_REQUESTS:
                raise ProviderRateLimitError(
                    "tiingo rate-limited the history request (HTTP 429)",
                    retry_after_s=_retry_after(resp.headers.get("Retry-After")),
                )
            resp.raise_for_status()
            rows = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"tiingo history unavailable: {exc}") from exc

        bars: list[Bar] = []
        for row in rows:
            try:
                bucket = datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00"))
                o, h, low, c = (float(row[k]) for k in ("open", "high", "low", "close"))
            except (KeyError, TypeError, ValueError):
                self.dropped_frames += 1
                continue
            if not (math.isfinite(h) and 0.0 < low <= min(o, c) <= max(o, c) <= h):
                self.dropped_frames += 1
                continue
            if not start <= bucket <= end:
                continue
            bars.append(
                Bar(
                    symbol=symbol.upper(),
                    bucket=bucket,
                    open=o,
                    high=h,
                    low=low,
                    close=c,
                    tick_count=0,  # unknown from OHLC-only history; honest zero
                    sum_ret=0.0,
                    sum_ret_sq=0.0,
                    source=BarSource.BACKFILL,
                )
            )
        log.info("tiingo.backfilled", symbol=symbol, interval=interval, bars=len(bars))
        return bars
