"""Tiingo's FX quote is positional: [Q, ticker, date, bidSize, bid, mid, askSize, ask].

Reading index 6 (askSize) as the ask stored every pair at (bid + 1_000_000) / 2:
USDJPY at 158.344 became 500079.172 in Redis, in Postgres and on the chart.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fx_core.models import Tick
from fx_ingestor.providers.tiingo import TiingoProvider

TS = "2026-10-09T15:53:00.123Z"


def frame(*data: object) -> str:
    return json.dumps({"service": "fx", "messageType": "A", "data": ["Q", "usdjpy", TS, *data]})


def test_the_ask_is_read_from_the_ask_price_field() -> None:
    tick = TiingoProvider(["USDJPY"], token="t")._parse(
        frame(1_000_000.0, 158.344, 158.3445, 1_000_000.0, 158.345)
    )

    assert tick is not None
    assert (tick.bid, tick.ask) == (158.344, 158.345)
    assert tick.mid == pytest.approx(158.3445)


def test_the_size_is_never_stored_as_the_ask() -> None:
    """The frame as the old parser read it: index 6 is a size of 1_000_000."""
    provider = TiingoProvider(["USDJPY"], token="t")

    assert provider._parse(frame(1_000_000.0, 158.344, 158.3445, 158.345, 1_000_000.0)) is None
    assert provider.dropped_frames == 1


def test_a_frame_that_disagrees_with_its_own_mid_is_dropped() -> None:
    """Plausible prices from the wrong fields still contradict the frame's mid."""
    provider = TiingoProvider(["USDJPY"], token="t")

    assert provider._parse(frame(1_000_000.0, 158.344, 158.3445, 1_000_000.0, 158.9)) is None
    assert provider.dropped_frames == 1


def test_the_stored_anomaly_fails_validation() -> None:
    now = datetime.now(UTC)
    tick = Tick("USDJPY", bid=158.344, ask=1_000_000.0, ts_event=now, ts_ingest=now)

    assert tick.mid == pytest.approx(500079.172)
    with pytest.raises(ValueError, match="implausible spread"):
        tick.validate()
