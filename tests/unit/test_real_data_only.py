"""Real market data only: no simulator to select, no start without credentials,
and the provider's own record is the only past ever loaded."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fx_ingestor.config import IngestorSettings
from fx_ingestor.providers import ProviderError, TiingoProvider, tiingo
from pydantic import ValidationError

END = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def test_the_ingestor_refuses_to_start_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FX_PROVIDER_TOKEN", raising=False)
    with pytest.raises(ValidationError, match="FX_PROVIDER_TOKEN is required"):
        IngestorSettings(_env_file=None)  # type: ignore[call-arg]


def test_the_retired_simulator_cannot_be_selected() -> None:
    with pytest.raises(ValidationError, match="FX_PROVIDER must be one of"):
        IngestorSettings(FX_PROVIDER="replay", FX_PROVIDER_TOKEN="t", _env_file=None)  # type: ignore[call-arg]


def serve(
    monkeypatch: pytest.MonkeyPatch, answer: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []
    real = httpx.AsyncClient

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer(request)

    monkeypatch.setattr(
        tiingo.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(record), **kw),
    )
    return seen


async def test_history_keeps_the_token_out_of_the_url_and_drops_inconsistent_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "date": "2026-10-10T11:58:00Z",
            "open": 158.30,
            "high": 158.36,
            "low": 158.29,
            "close": 158.35,
        },
        {
            "date": "2026-10-10T11:59:00Z",
            "open": 158.35,
            "high": 158.30,
            "low": 158.40,
            "close": 158.33,
        },
        {"date": "2026-10-10T11:59:00Z", "open": None, "high": 158.4, "low": 158.3, "close": 158.3},
    ]
    seen = serve(monkeypatch, lambda _r: httpx.Response(200, json=rows))

    bars = await TiingoProvider(["USDJPY"], token="secret").backfill(
        "USDJPY", END - timedelta(days=1), END, "1m"
    )

    assert [(b.close, str(b.source)) for b in bars] == [(158.35, "backfill")]
    assert seen[0].headers["Authorization"] == "Token secret"
    assert "secret" not in str(seen[0].url)
    assert seen[0].url.params["resampleFreq"] == "1min"


async def test_history_the_account_may_not_read_is_a_recoverable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve(monkeypatch, lambda _r: httpx.Response(403, json={"detail": "not permitted"}))

    with pytest.raises(ProviderError, match="history unavailable"):
        await TiingoProvider(["USDJPY"], token="t").backfill("USDJPY", END - timedelta(days=1), END)
