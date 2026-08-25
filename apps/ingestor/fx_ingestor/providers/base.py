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

__all__ = ["MarketDataProvider", "ProviderAuthError", "ProviderError"]


class ProviderError(RuntimeError):
    """Recoverable upstream failure. The supervisor will back off and retry."""


class ProviderAuthError(ProviderError):
    """Bad or missing credentials.

    Deliberately distinct: retrying a 401 forever is pointless and will get the
    key banned. The supervisor treats this as fatal and exits loudly instead of
    entering an infinite backoff loop that looks healthy in a dashboard.
    """


class MarketDataProvider(abc.ABC):
    """One upstream feed.

    Contract:
    * ``connect`` may raise ``ProviderError``; the supervisor owns all retry policy,
      so implementations must NOT retry internally. One responsibility each.
    * ``stream`` yields validated ``Tick`` objects and terminates cleanly when the
      connection drops - it never raises for an ordinary disconnect.
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
        """Yield normalised ticks until the connection closes."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Idempotent. Must be safe to call on an already-dead connection."""

    async def backfill(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        """Fetch bars for a detected gap.

        Default is "no backfill available" rather than an exception, so a provider
        without a REST history endpoint still works - the gap is simply recorded
        as missing instead of silently interpolated. Never invent data to fill a
        hole; ``BarSource.BACKFILL`` exists so the UI can show the difference.
        """
        _ = (symbol, start, end)
        return []

    async def __aenter__(self) -> MarketDataProvider:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()
