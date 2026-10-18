"""The provider extension point.

This is the file a new contributor touches to add a data source, and the concrete
answer to a reviewer asking "how would I plug in a different feed?"

It is an anti-corruption layer in the Evans sense: provider wire formats are
grotesque and all different - Tiingo sends *positional arrays*, Twelve Data sends
objects, another might send protobuf - and every one of them will change without
warning. Converting to ``fx_core.Tick`` at this boundary means **nothing
downstream ever sees a provider-shaped payload**, so a provider change touches
exactly one file and cannot reach the volatility maths, the WAL, or the schema.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Sequence
from datetime import datetime

from fx_core.models import Bar, Tick

__all__ = [
    "MarketDataProvider",
    "ProviderAuthError",
    "ProviderEntitlementError",
    "ProviderError",
    "ProviderRateLimitError",
]


class ProviderError(RuntimeError):
    """Recoverable upstream failure. The supervisor will back off and retry."""


class ProviderAuthError(ProviderError):
    """Bad or missing credentials.

    Deliberately distinct: retrying a 401 forever is pointless and will get the
    key banned. The supervisor treats this as fatal and exits loudly instead of
    entering an infinite backoff loop that looks healthy in a dashboard.
    """


class ProviderEntitlementError(ProviderError):
    """The credentials work, but this account may not read what was asked for (HTTP 403).

    Recoverable for the feed - the live stream does not depend on it - so it is
    a plain ProviderError, not an auth failure. History recovery stops asking for
    that symbol for a while rather than repeating a request that cannot succeed.
    """


class ProviderRateLimitError(ProviderError):
    """The provider asked us to slow down (HTTP 429).

    Still a ProviderError, so anything that already retries those keeps doing
    so; history recovery stops its run instead and waits ``retry_after_s`` (the
    provider's hint, when it gives one) before asking again.
    """

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class MarketDataProvider(abc.ABC):
    """One upstream feed.

    Contract:
    * ``connect`` may raise ``ProviderError``; the supervisor owns all retry policy,
      so implementations must NOT retry internally. One responsibility each.
    * ``stream`` yields validated ``Tick`` objects until the caller stops reading.
      A connection that drops or is closed, cleanly or not, raises
      ``ProviderError`` so the supervisor's retry path owns it. A transport
      library's own exception escaping ``stream`` ends the process instead.
    * Implementations count their own malformed frames in ``dropped_frames`` rather
      than raising, because one corrupt message must not kill the feed.
    """

    name: str = "abstract"
    supports_backfill: bool = False

    def __init__(self, symbols: Sequence[str]) -> None:
        self.symbols = [s.upper() for s in symbols]
        self.dropped_frames = 0
        self.received_frames = 0

    @abc.abstractmethod
    async def connect(self) -> None:
        """Open the connection and subscribe. Raise ProviderError on failure."""

    @abc.abstractmethod
    def stream(self) -> AsyncIterator[Tick]:
        """Yield normalised ticks; raise ProviderError when the connection closes."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Idempotent. Must be safe to call on an already-dead connection."""

    async def backfill(
        self, symbol: str, start: datetime, end: datetime, interval: str = "1m"
    ) -> list[Bar]:
        """Fetch the provider's own bars of ``interval`` ("1m" or "1d").

        Default is "no backfill available" rather than an exception, so a provider
        without a REST history endpoint still works - the gap is simply recorded
        as missing instead of silently interpolated. Never invent data to fill a
        hole; ``BarSource.BACKFILL`` exists so the UI can show the difference.
        """
        _ = (symbol, start, end, interval)
        return []

    async def __aenter__(self) -> MarketDataProvider:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
