"""FX market session calendar.

Why this file exists
--------------------
A dead WebSocket and a quiet market are byte-for-byte identical: both produce
silence. So the ingestor runs a staleness watchdog ("no message in 30s -> force
reconnect"). Without a calendar, that watchdog reconnect-loops from Friday evening
to Sunday evening, hammering the provider, filling the logs with false alarms, and
very possibly getting the API key rate-limited before Monday.

Almost no forex tracker on GitHub has one of these. It is a small file that answers
a question a reviewer will absolutely ask.

The convention
--------------
The FX week runs Sunday 17:00 to Friday 17:00 **New York time**, not UTC. Anchoring
in ``America/New_York`` means DST is handled by the stdlib rather than by us: the
UTC open shifts between 21:00 and 22:00 twice a year, and hardcoding either one is
wrong for half the year.

Sessions overlap; the London/New York overlap (12:00-16:00 UTC) is the highest-
liquidity window of the day and the one where volatility spikes are most meaningful.
The UI shades it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

__all__ = [
    "SESSIONS",
    "Session",
    "active_sessions",
    "is_market_open",
    "next_close",
    "next_open",
    "seconds_until_open",
]

NY = ZoneInfo("America/New_York")

_WEEK_OPEN_DAY = 6  # Sunday, per datetime.weekday()
_WEEK_CLOSE_DAY = 4  # Friday
_SATURDAY = 5
_WEEK_BOUNDARY = time(17, 0)  # 17:00 New York


@dataclass(frozen=True, slots=True)
class Session:
    """A regional trading session, in UTC hours (approximate, by convention)."""

    name: str
    open_utc: int
    close_utc: int

    def contains(self, ts: datetime) -> bool:
        h = ts.astimezone(UTC).hour
        if self.open_utc <= self.close_utc:
            return self.open_utc <= h < self.close_utc
        return h >= self.open_utc or h < self.close_utc  # wraps midnight


SESSIONS: tuple[Session, ...] = (
    Session("Sydney", 21, 6),
    Session("Tokyo", 0, 9),
    Session("London", 7, 16),
    Session("New York", 12, 21),
)


def is_market_open(ts: datetime | None = None) -> bool:
    """True if the interbank FX market is open at ``ts``.

    Holidays are deliberately not modelled: FX has no single holiday calendar
    (liquidity thins on Christmas and New Year but the market does not close), so
    a hardcoded holiday list would be wrong more often than it was right. The
    staleness watchdog handles thin-liquidity silence via its timeout instead.
    """
    ts = (ts or datetime.now(UTC)).astimezone(NY)
    dow = ts.weekday()
    local_t = ts.time()

    if dow == _WEEK_CLOSE_DAY:
        return local_t < _WEEK_BOUNDARY
    if dow == _SATURDAY:
        return False
    if dow == _WEEK_OPEN_DAY:
        return local_t >= _WEEK_BOUNDARY
    return True  # Mon-Thu


def next_close(ts: datetime | None = None) -> datetime:
    """Next Friday 17:00 New York, as UTC."""
    ts = (ts or datetime.now(UTC)).astimezone(NY)
    days = (_WEEK_CLOSE_DAY - ts.weekday()) % 7
    candidate = datetime.combine(ts.date() + timedelta(days=days), _WEEK_BOUNDARY, tzinfo=NY)
    if candidate <= ts:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


def next_open(ts: datetime | None = None) -> datetime:
    """Next Sunday 17:00 New York, as UTC. Returns ``ts`` if already open."""
    ts_utc = ts or datetime.now(UTC)
    if is_market_open(ts_utc):
        return ts_utc
    local = ts_utc.astimezone(NY)
    days = (_WEEK_OPEN_DAY - local.weekday()) % 7
    candidate = datetime.combine(local.date() + timedelta(days=days), _WEEK_BOUNDARY, tzinfo=NY)
    if candidate <= local:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


def seconds_until_open(ts: datetime | None = None) -> float:
    """0.0 when open. The supervisor sleeps on this instead of retry-looping."""
    ts = ts or datetime.now(UTC)
    if is_market_open(ts):
        return 0.0
    return (next_open(ts) - ts).total_seconds()


def active_sessions(ts: datetime | None = None) -> tuple[str, ...]:
    """Which regional sessions are live. Two of these overlapping is the signal."""
    ts = ts or datetime.now(UTC)
    if not is_market_open(ts):
        return ()
    return tuple(s.name for s in SESSIONS if s.contains(ts))
