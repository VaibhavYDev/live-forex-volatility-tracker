"""Provider registry.

Adding a data source is: write the adapter, add one line to ``_REGISTRY``, done.
Nothing else in the codebase needs to know it exists. That is the concrete answer
to "how would a new contributor extend this?" - and it is the question GSoC
mentors actually weight. Every entry is a real market feed: nothing here may
generate prices.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from fx_ingestor.providers.base import (
    MarketDataProvider,
    ProviderAuthError,
    ProviderError,
)
from fx_ingestor.providers.tiingo import TiingoProvider

__all__ = [
    "PROVIDERS",
    "MarketDataProvider",
    "ProviderAuthError",
    "ProviderError",
    "TiingoProvider",
    "build_provider",
]

_REGISTRY: dict[str, Callable[..., MarketDataProvider]] = {
    "tiingo": TiingoProvider,
}

PROVIDERS = frozenset(_REGISTRY)


def build_provider(name: str, symbols: Sequence[str], **kwargs: Any) -> MarketDataProvider:
    try:
        factory = _REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown provider {name!r}; available: {sorted(_REGISTRY)}") from None
    return factory(symbols, **kwargs)
