"""Real market data only: no simulator to select, no start without credentials,
and the provider's own record is the only past ever loaded."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fx_ingestor.config import IngestorSettings
from fx_ingestor.main import DAILY_HISTORY_REFRESH_S, Ingestor
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


async def test_the_daily_series_is_refreshed_while_the_feed_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loaded once at start, it expired mid-run and left 1d/1w/1M empty until a restart."""
    monkeypatch.setattr("fx_ingestor.main.make_redis", lambda _url: None)
    ing = Ingestor(IngestorSettings(FX_PROVIDER_TOKEN="t", FX_SYMBOLS="EURUSD", _env_file=None))  # type: ignore[call-arg]
    refreshed: list[object] = []

    async def refresh(provider: object) -> int:
        refreshed.append(provider)
        return 0

    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(ing.pipeline, "refresh_daily_history", refresh)
    monkeypatch.setattr("fx_ingestor.main.asyncio.sleep", sleep)
    ing._build_provider = lambda: "tiingo"  # type: ignore[assignment,method-assign,return-value]

    with pytest.raises(asyncio.CancelledError):
        await ing._refresh_daily_history()

    assert refreshed == ["tiingo", "tiingo"]
    assert slept == [DAILY_HISTORY_REFRESH_S] * 3


async def test_the_leader_refreshes_beside_the_feed_and_stops_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("fx_ingestor.main.make_redis", lambda _url: None)
    monkeypatch.setattr("fx_ingestor.main.serve_metrics", lambda _port: None)
    monkeypatch.setattr("fx_ingestor.main.close_redis", _nothing)
    ing = Ingestor(IngestorSettings(FX_PROVIDER_TOKEN="t", FX_SYMBOLS="EURUSD", _env_file=None))  # type: ignore[call-arg]
    refreshing, stopped = asyncio.Event(), asyncio.Event()

    async def refresh() -> None:
        refreshing.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    async def lead() -> None:
        await asyncio.wait_for(refreshing.wait(), 1)
        ing._shutdown.set()

    @contextlib.asynccontextmanager
    async def hold() -> AsyncIterator[None]:
        yield

    for name, fake in (
        ("_refresh_daily_history", refresh),
        ("_run_as_leader", lead),
        ("_rehydrate", _nothing),
        ("_heartbeat_loop", _nothing),
        ("_publish_status", _nothing),
    ):
        monkeypatch.setattr(ing, name, fake)
    monkeypatch.setattr(ing.lease, "hold", hold)

    started = asyncio.get_running_loop().time()
    await asyncio.wait_for(ing.run(), 2)

    assert refreshing.is_set() and stopped.is_set()
    # Leadership ending must stop the refresher, not leave run() waiting on it.
    assert asyncio.get_running_loop().time() - started < 1


async def _nothing(*_args: object) -> None:
    return None
