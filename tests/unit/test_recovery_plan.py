"""Planning a history recovery: which minutes, in which requests. No services needed.

The integration tests in tests/integration/test_history_recovery.py run the same
plans against PostgreSQL and Redis.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import structlog.testing
from fx_ingestor.config import IngestorSettings
from fx_ingestor.main import Ingestor
from fx_ingestor.providers import (
    ProviderAuthError,
    ProviderEntitlementError,
    ProviderError,
    ProviderRateLimitError,
    tiingo,
)
from fx_ingestor.providers.tiingo import TiingoProvider
from fx_ingestor.recovery import (
    MINUTE,
    Span,
    Window,
    missing_spans,
    open_minutes,
    plan_requests,
    recovery_window,
    refresh_range,
)
from pydantic import ValidationError


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def minutes(start: datetime, end: datetime) -> list[datetime]:
    out, m = [], start
    while m <= end:
        out.append(m)
        m += MINUTE
    return out


# ------------------------------------------------------------------ window --
def test_minutes_the_worker_may_still_persist_are_never_eligible() -> None:
    """The horizon is the tick stream's retention plus two minutes."""
    now = utc(2030, 10, 14, 12, 0, 30)
    window = recovery_window(now, timedelta(days=7), timedelta(seconds=900 + 120))

    assert window is not None
    # 12:00:30 - 17 min = 11:43:30 -> the 11:42 bar is the newest one that has ended in time.
    assert window.end == utc(2030, 10, 14, 11, 42)
    assert window.start == utc(2030, 10, 7, 12, 0)


def test_a_lookback_shorter_than_the_horizon_is_an_empty_window() -> None:
    assert (
        recovery_window(utc(2030, 10, 14, 12), timedelta(minutes=5), timedelta(minutes=17)) is None
    )


# ---------------------------------------------------------------- calendar --
@pytest.mark.parametrize(
    ("friday_close", "sunday_open"),
    [
        # US daylight time: New York 17:00 is 21:00 UTC.
        (utc(2030, 10, 25, 21, 0), utc(2030, 10, 27, 21, 0)),
        # Standard time, from Sunday 3 November 2030: 17:00 is 22:00 UTC.
        (utc(2030, 11, 8, 22, 0), utc(2030, 11, 10, 22, 0)),
    ],
)
def test_the_weekly_close_and_reopen_follow_new_york_daylight_saving(
    friday_close: datetime, sunday_open: datetime
) -> None:
    expected = open_minutes(
        Window(friday_close - timedelta(hours=1), sunday_open + timedelta(hours=1) - MINUTE)
    )

    assert len(expected) == 120
    assert expected[59] == friday_close - MINUTE  # the last open minute of the week
    assert expected[60] == sunday_open  # the next is Sunday 17:00 New York


def test_the_reopen_on_the_day_the_clocks_change_uses_the_new_offset() -> None:
    # Sunday 2030-11-03 02:00 New York the clocks go back; the market opens at 17:00 EST.
    expected = open_minutes(Window(utc(2030, 11, 3, 20, 0), utc(2030, 11, 3, 22, 30)))
    assert expected[0] == utc(2030, 11, 3, 22, 0)


# ------------------------------------------------------------------- spans --
def test_the_october_12_outage_becomes_three_spans_per_symbol() -> None:
    """Live bars 10:57-11:42 and 14:36-16:46 UTC; standby and sleep took the rest."""
    day = Window(utc(2030, 10, 14, 0, 0), utc(2030, 10, 14, 23, 59))
    present = set(minutes(utc(2030, 10, 14, 10, 57), utc(2030, 10, 14, 11, 42))) | set(
        minutes(utc(2030, 10, 14, 14, 36), utc(2030, 10, 14, 16, 46))
    )

    spans = missing_spans("EURUSD", open_minutes(day), present)

    assert [(s.start, s.end, s.minutes) for s in spans] == [
        (utc(2030, 10, 14, 0, 0), utc(2030, 10, 14, 10, 56), 657),
        (utc(2030, 10, 14, 11, 43), utc(2030, 10, 14, 14, 35), 173),
        (utc(2030, 10, 14, 16, 47), utc(2030, 10, 14, 23, 59), 433),
    ]


def test_a_closure_splits_a_gap_into_two_spans() -> None:
    weekend = Window(utc(2030, 10, 11, 20, 50), utc(2030, 10, 13, 21, 9))
    spans = missing_spans("EURUSD", open_minutes(weekend), set())

    assert [(s.start, s.end) for s in spans] == [
        (utc(2030, 10, 11, 20, 50), utc(2030, 10, 11, 20, 59)),
        (utc(2030, 10, 13, 21, 0), utc(2030, 10, 13, 21, 9)),
    ]


def test_minutes_the_provider_was_already_found_silent_on_are_not_asked_for_again() -> None:
    day = open_minutes(Window(utc(2030, 12, 25, 0, 0), utc(2030, 12, 25, 0, 9)))
    settled = [(utc(2030, 12, 25, 0, 0), utc(2030, 12, 25, 0, 4))]

    spans = missing_spans("EURUSD", day, set(), settled)

    assert [(s.start, s.end) for s in spans] == [(utc(2030, 12, 25, 0, 5), utc(2030, 12, 25, 0, 9))]


# ---------------------------------------------------------------- requests --
def test_requests_cover_whole_utc_days_up_to_the_limit() -> None:
    long_gap = Span("EURUSD", utc(2030, 10, 14, 6, 0), utc(2030, 10, 18, 20, 59))
    other = Span("GBPUSD", utc(2030, 10, 14, 6, 0), utc(2030, 10, 14, 6, 9))

    requests = plan_requests([long_gap, other], days_per_request=3)

    assert [(r.symbol, r.start, r.end) for r in requests] == [
        ("EURUSD", utc(2030, 10, 14, 6, 0), utc(2030, 10, 16, 23, 59)),
        ("EURUSD", utc(2030, 10, 17, 0, 0), utc(2030, 10, 18, 20, 59)),
        ("GBPUSD", utc(2030, 10, 14, 6, 0), utc(2030, 10, 14, 6, 9)),
    ]
    assert sum(r.minutes for r in requests if r.symbol == "EURUSD") == long_gap.minutes


def test_spans_in_the_same_days_share_one_request() -> None:
    spans = [
        Span("EURUSD", utc(2030, 10, 14, 0, 0), utc(2030, 10, 14, 10, 56)),
        Span("EURUSD", utc(2030, 10, 14, 11, 43), utc(2030, 10, 14, 14, 35)),
        Span("EURUSD", utc(2030, 10, 14, 16, 47), utc(2030, 10, 14, 23, 59)),
    ]
    requests = plan_requests(spans, days_per_request=3)

    assert len(requests) == 1 and requests[0].spans == tuple(spans)


# ----------------------------------------------------------------- rollups --
def test_a_rollup_bucket_that_has_not_closed_is_left_to_the_refresh_policy() -> None:
    now = utc(2030, 10, 14, 11, 0, 30)
    five = refresh_range(
        utc(2030, 10, 14, 10, 0),
        utc(2030, 10, 14, 10, 58),
        timedelta(minutes=5),
        timedelta(minutes=1),
        now,
    )
    hour = refresh_range(
        utc(2030, 10, 14, 10, 0),
        utc(2030, 10, 14, 10, 58),
        timedelta(hours=1),
        timedelta(minutes=5),
        now,
    )

    assert five == (utc(2030, 10, 14, 10, 0), utc(2030, 10, 14, 10, 55))
    assert hour is None  # 10:00-11:00 is not past the 5-minute offset yet


# --------------------------------------------------------------- provider --
def _answer(
    monkeypatch: pytest.MonkeyPatch, answer: Callable[[httpx.Request], httpx.Response]
) -> None:
    real = httpx.AsyncClient
    monkeypatch.setattr(
        tiingo.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(answer), **kw)
    )


@pytest.mark.parametrize(
    ("status", "expected"), [(401, ProviderAuthError), (403, ProviderEntitlementError)]
)
async def test_refused_history_requests_say_why(
    monkeypatch: pytest.MonkeyPatch, status: int, expected: type[ProviderError]
) -> None:
    """401 is the token; 403 is an account that may not read this history - recoverable."""
    _answer(monkeypatch, lambda _r: httpx.Response(status, json={"detail": "no"}))
    with pytest.raises(expected) as caught:
        await TiingoProvider(["EURUSD"], token="t").backfill(
            "EURUSD", utc(2030, 10, 14), utc(2030, 10, 14, 23, 59)
        )
    assert isinstance(caught.value, ProviderAuthError) == (status == 401)


async def test_a_rate_limit_carries_the_providers_retry_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _answer(monkeypatch, lambda _r: httpx.Response(429, headers={"Retry-After": "120"}))
    with pytest.raises(ProviderRateLimitError) as caught:
        await TiingoProvider(["EURUSD"], token="t").backfill(
            "EURUSD", utc(2030, 10, 14), utc(2030, 10, 14, 23, 59)
        )
    assert caught.value.retry_after_s == 120


# ---------------------------------------------------------------- settings --
def test_recovery_is_off_unless_asked_for() -> None:
    cfg = IngestorSettings(FX_PROVIDER_TOKEN="t", _env_file=None)  # type: ignore[call-arg]
    assert cfg.recovery_mode == "off"


def test_applying_recovery_without_a_database_is_refused() -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        IngestorSettings(
            FX_PROVIDER_TOKEN="t", FX_RECOVERY_MODE="apply", DATABASE_URL="", _env_file=None
        )  # type: ignore[call-arg]


# ------------------------------------------------------------ in the leader --
def _ingestor(monkeypatch: pytest.MonkeyPatch, **settings: object) -> Ingestor:
    monkeypatch.setattr("fx_ingestor.main.make_redis", lambda _url: None)
    return Ingestor(IngestorSettings(FX_PROVIDER_TOKEN="t", _env_file=None, **settings))  # type: ignore[call-arg, arg-type]


async def test_recovery_is_not_started_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    ing = _ingestor(monkeypatch)

    async def never(_s: float) -> None:
        raise AssertionError("an idle recovery task must not even wait")

    monkeypatch.setattr("fx_ingestor.main.asyncio.sleep", never)
    await ing._recover_history()


async def test_an_unreachable_database_only_postpones_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ing = _ingestor(
        monkeypatch,
        FX_RECOVERY_MODE="dry_run",
        DATABASE_URL="postgresql://fx@127.0.0.1:1/fx",
        FX_RECOVERY_FIRST_DELAY_S=0,
    )
    attempts: list[str] = []

    async def refused(*_a: object, **_k: object) -> None:
        attempts.append("connect")
        raise ConnectionRefusedError("connection refused")

    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 3:
            raise asyncio.CancelledError  # the leader ending the task

    monkeypatch.setattr("fx_ingestor.main.asyncpg.create_pool", refused)
    monkeypatch.setattr("fx_ingestor.main.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await ing._recover_history()

    assert attempts == ["connect", "connect"] and slept == [0, 900, 900]


async def test_a_defect_in_recovery_is_reported_and_never_ends_the_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ing = _ingestor(monkeypatch)
    monkeypatch.setattr("fx_ingestor.main.serve_metrics", lambda _port: None)

    async def nothing(*_a: object) -> None:
        return None

    async def broken() -> None:
        raise RuntimeError("a bug in recovery")

    async def lead() -> None:
        await asyncio.sleep(0.05)
        ing._shutdown.set()

    @contextlib.asynccontextmanager
    async def hold() -> AsyncIterator[None]:
        yield

    for name, fake in (
        ("_recover_history", broken),
        ("_run_as_leader", lead),
        ("_rehydrate", nothing),
        ("_refresh_daily_history", nothing),
        ("_heartbeat_loop", nothing),
        ("_publish_status", nothing),
    ):
        monkeypatch.setattr(ing, name, fake)
    monkeypatch.setattr(ing.lease, "hold", hold)
    monkeypatch.setattr("fx_ingestor.main.close_redis", nothing)

    with structlog.testing.capture_logs() as logs:
        await asyncio.wait_for(ing.run(), 2)  # returns: the feed's own lifecycle is untouched

    assert any(e["event"] == "recovery.crashed" and "a bug in recovery" in e["error"] for e in logs)
