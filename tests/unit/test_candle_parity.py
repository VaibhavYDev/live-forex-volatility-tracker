"""The browser folds candles the way the server does.

Every intraday timeframe is folded in the browser from one-minute bars, so the
dashboard draws a candle `/api/bars` never served. These two folds must agree
exactly, and the market calendar the daily candle relies on must agree with the
one the ingestor runs. Each side is tested against `apps/web/src/test/
candle_parity.json`: this file checks that the fixture is what `fx_core` computes
today; `apps/web/src/lib/__tests__/candles.test.ts` and `market.test.ts` check
that the browser computes the same from the same input.

The minutes are generated, not recorded - the point is the arithmetic, not any
real price - and shaped like the cases that matter: provider history turning
live mid-bucket, the same minute from both sources, minutes nothing traded in,
the Friday close and the Sunday reopen.

Regenerate after a deliberate change to the fold or the calendar with
``python -m tests.unit.test_candle_parity``; the tests read the stored copy.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fx_core.calendar import is_market_open
from fx_core.intervals import fold

# Inside the web app: its image is built from apps/web alone, and the browser
# tests import this file.
FIXTURE = (
    Path(__file__).resolve().parents[2] / "apps" / "web" / "src" / "test" / "candle_parity.json"
)
TIMEFRAMES = ("1m", "3m", "5m", "10m", "15m", "30m")
SYMBOLS = {"EURUSD": (1.08, 5), "USDJPY": (150.0, 3)}

# Friday 16 October 2026: the market closes 21:00 UTC (17:00 New York, EDT).
# Sunday 18 October: it reopens 21:00 UTC.
FRIDAY = datetime(2026, 10, 16, 19, 58, tzinfo=UTC)
SUNDAY = datetime(2026, 10, 18, 21, 0, tzinfo=UTC)
SPAN_MIN = 62


def _row(bar: dict[str, Any]) -> list[Any]:
    return [bar["t"], bar["o"], bar["h"], bar["l"], bar["c"], bar["n"], bar["src"]]


def _minutes(symbol: str) -> list[dict[str, Any]]:
    """Raw cache members: unsorted, gapped, with one minute from both sources."""
    price, dp = SYMBOLS[symbol]
    rng = random.Random(symbol)
    out: list[dict[str, Any]] = []
    for start in (FRIDAY, SUNDAY):
        for i in range(SPAN_MIN):
            at = start + timedelta(minutes=i)
            if not is_market_open(at) or rng.random() < 0.12:
                continue  # closed, or a minute nothing arrived for
            moves = [rng.gauss(0, price * 2e-4) for _ in range(4)]
            o = price
            path = [o + sum(moves[: k + 1]) for k in range(4)]
            c = path[-1]
            h = max(o, *path) + abs(rng.gauss(0, price * 5e-5))
            low = min(o, *path) - abs(rng.gauss(0, price * 5e-5))
            price = c
            # The first stretch of each session is provider history; the
            # stream takes over part-way through a 15-minute bucket.
            src = "backfill" if i < 22 else "stream"
            bar = {
                "t": int(at.timestamp()),
                "o": round(o, dp),
                "h": round(h, dp),
                "l": round(low, dp),
                "c": round(c, dp),
                "n": 0 if src == "backfill" else rng.randint(1, 90),
                "src": src,
            }
            if i == 22:
                # The minute the stream joined: the provider has it from its
                # first second, our stream from the tick it connected on, so
                # each saw an extreme the other did not.
                early = round(o - price * 1e-4, dp)
                out.append(
                    {
                        "t": bar["t"],
                        "o": early,
                        "h": round(h + price * 3e-5, dp),
                        "l": min(round(low, dp), early),
                        "c": round(c - price * 2e-5, dp),
                        "n": 0,
                        "src": "backfill",
                    }
                )
                bar["l"] = round(low - price * 4e-5, dp)
            out.append(bar)
    rng.shuffle(out)
    return out


def _calendar() -> list[list[Any]]:
    """The minutes either side of every boundary the daily candle counts across."""
    edges = [
        datetime(2026, 10, 16, 20, 59, tzinfo=UTC),  # Friday close, EDT
        datetime(2026, 10, 18, 20, 59, tzinfo=UTC),  # Sunday reopen, EDT
        datetime(2026, 10, 30, 20, 59, tzinfo=UTC),  # last Friday close in EDT
        datetime(2026, 11, 1, 21, 59, tzinfo=UTC),  # reopen the day the clocks change
        datetime(2026, 11, 6, 21, 59, tzinfo=UTC),  # Friday close, EST
        datetime(2026, 3, 8, 20, 59, tzinfo=UTC),  # reopen the day the clocks go forward
    ]
    out: list[list[Any]] = []
    for edge in edges:
        for k in range(3):
            at = edge + timedelta(minutes=k)
            out.append([int(at.timestamp()), is_market_open(at)])
    # A Saturday, a Wednesday midnight, and Sunday morning before the open.
    for spec in ("2026-10-17T12:00", "2026-10-21T00:00", "2026-10-18T09:30"):
        at = datetime.fromisoformat(spec).replace(tzinfo=UTC)
        out.append([int(at.timestamp()), is_market_open(at)])
    return out


def build() -> dict[str, Any]:
    minutes = {symbol: _minutes(symbol) for symbol in SYMBOLS}
    return {
        "minutes": {s: [_row(b) for b in bars] for s, bars in minutes.items()},
        "folds": {
            s: {tf: [_row(b) for b in fold(bars, tf)] for tf in TIMEFRAMES}
            for s, bars in minutes.items()
        },
        "calendar": _calendar(),
    }


def _dump(data: dict[str, Any]) -> str:
    # One row per line: small enough to read in a diff, exact enough to compare.
    lines = ["{"]
    for i, (key, value) in enumerate(data.items()):
        tail = "," if i < len(data) - 1 else ""
        lines.append(f"  {json.dumps(key)}: {json.dumps(value, separators=(',', ':'))}{tail}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _stored() -> dict[str, Any]:
    return dict(json.loads(FIXTURE.read_text(encoding="utf-8")))


def _bars(rows: list[list[Any]]) -> list[dict[str, Any]]:
    return [dict(zip(("t", "o", "h", "l", "c", "n", "src"), row, strict=True)) for row in rows]


def test_the_stored_folds_are_what_the_server_computes() -> None:
    # From the stored minutes rather than regenerated ones, so nothing here
    # depends on the random generator staying the same between Pythons.
    data = _stored()
    for symbol, rows in data["minutes"].items():
        for tf in TIMEFRAMES:
            assert [_row(b) for b in fold(_bars(rows), tf)] == data["folds"][symbol][tf], (
                f"{symbol} {tf}: fx_core.intervals.fold no longer matches the fixture. If "
                "that was deliberate, regenerate it; the browser test then says whether "
                "the browser still agrees."
            )


def test_the_stored_calendar_is_what_the_server_computes() -> None:
    for epoch, open_ in _stored()["calendar"]:
        assert is_market_open(datetime.fromtimestamp(epoch, tz=UTC)) is open_, epoch


def test_the_fixture_exercises_what_it_claims() -> None:
    data = _stored()
    for symbol, folds in data["folds"].items():
        srcs = {row[6] for row in folds["15m"]}
        assert {"backfill", "mixed", "stream"} <= srcs, symbol
        stamps = [row[0] for row in data["minutes"][symbol]]
        assert len(stamps) != len(set(stamps)), "one minute must arrive from both sources"
        assert stamps != sorted(stamps), "the input must be unordered"
        closed = range(min(stamps), max(stamps), 60)
        assert any(not is_market_open(datetime.fromtimestamp(t, tz=UTC)) for t in closed)
    assert {open_ for _, open_ in data["calendar"]} == {True, False}


if __name__ == "__main__":
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(_dump(build()), encoding="utf-8", newline="\n")
    print(f"wrote {FIXTURE}")
