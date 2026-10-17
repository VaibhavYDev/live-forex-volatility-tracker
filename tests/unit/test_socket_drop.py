"""A dropped Tiingo socket is a provider failure, not a crash.

THE DEFECT
----------
``TiingoProvider.stream()`` read the socket with ``async for``. On a flaky
network the keepalive ping times out and websockets raises

    ConnectionClosedError: sent 1011 (internal error) keepalive ping timeout;
    no close frame received

out of that loop. ``_run_as_leader`` retries ``ProviderError`` and ``OSError``
only, so the exception escaped ``run()``, the process exited and Docker
restarted the container, two or three times in half an hour on 2026-10-17,
each restart giving up the lease and rehydrating from scratch. The README's
failure-mode matrix promised backoff, a breaker failure and a ``degraded``
status instead.

A clean close went wrong more quietly: the iterator swallows
``ConnectionClosedOK`` and just stops, so the supervisor took it for a session
that ended well and went on reporting ``healthy`` while it waited to reconnect.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable

import pytest
from fx_core.models import Tick
from fx_ingestor.config import IngestorSettings
from fx_ingestor.main import Ingestor
from fx_ingestor.providers import ProviderAuthError, ProviderError, TiingoProvider
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidMessage,
)
from websockets.frames import Close, CloseCode

TOKEN = "tiingo-token-under-test"
TS = "2026-10-17T15:46:00.123Z"
CONNECT = "fx_ingestor.providers.tiingo.websockets.connect"


def quote() -> str:
    return json.dumps(
        {
            "service": "fx",
            "messageType": "A",
            "data": ["Q", "eurusd", TS, 1_000_000.0, 1.0841, 1.0842, 1_000_000.0, 1.0843],
        }
    )


# The two closes seen in the ingestor logs on 2026-10-17, and a clean one.
def keepalive_timeout() -> ConnectionClosed:
    return ConnectionClosedError(
        None, Close(CloseCode.INTERNAL_ERROR, "keepalive ping timeout"), None
    )


def vanished() -> ConnectionClosed:
    return ConnectionClosedError(None, None, None)


def closed_cleanly() -> ConnectionClosed:
    normal = Close(CloseCode.NORMAL_CLOSURE, "")
    return ConnectionClosedOK(normal, normal, True)


class ScriptedSocket:
    """Delivers its frames, then fails ``recv()`` the way websockets does."""

    def __init__(
        self,
        frames: list[str],
        then: ConnectionClosed,
        send_fails: ConnectionClosed | None = None,
    ) -> None:
        self._frames = list(frames)
        self._then = then
        self._send_fails = send_fails
        self.sent: list[str] = []
        self.closed = False

    async def send(self, message: str) -> None:
        if self._send_fails is not None:
            raise self._send_fails
        self.sent.append(message)

    async def recv(self) -> str:
        await asyncio.sleep(0)
        if self._frames:
            return self._frames.pop(0)
        raise self._then

    async def __aiter__(self) -> AsyncIterator[str]:
        # websockets' own iterator, so a stream() that goes back to ``async for``
        # fails these tests for the reason it is wrong.
        try:
            while True:
                yield await self.recv()
        except ConnectionClosedOK:
            return

    async def close(self) -> None:
        self.closed = True


def serve(
    monkeypatch: pytest.MonkeyPatch,
    sockets: list[ScriptedSocket],
    on_open: Callable[[int], None] = lambda _n: None,
) -> list[ScriptedSocket]:
    """Point the real adapter's connect() at the given sockets, in order."""
    opened: list[ScriptedSocket] = []

    async def connect(_url: str, **_kwargs: object) -> ScriptedSocket:
        sock = sockets[len(opened)]
        opened.append(sock)
        on_open(len(opened))
        return sock

    monkeypatch.setattr(CONNECT, connect)
    return opened


async def read_until_closed(provider: TiingoProvider) -> tuple[list[Tick], ProviderError]:
    ticks: list[Tick] = []
    with pytest.raises(ProviderError) as caught:
        async for tick in provider.stream():
            ticks.append(tick)
    return ticks, caught.value


class TestTheAdapter:
    @pytest.mark.parametrize(
        ("close", "detail"),
        [
            (keepalive_timeout, "sent 1011 (internal error) keepalive ping timeout"),
            (vanished, "no close frame received or sent"),
            (closed_cleanly, "received 1000 (OK); then sent 1000 (OK)"),
        ],
    )
    async def test_a_closed_socket_raises_provider_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        close: Callable[[], ConnectionClosed],
        detail: str,
    ) -> None:
        serve(monkeypatch, [ScriptedSocket([quote(), quote()], then=close())])
        provider = TiingoProvider(["EURUSD"], token=TOKEN)
        await provider.connect()

        ticks, error = await read_until_closed(provider)

        assert len(ticks) == 2, "quotes received before the close were lost"
        assert type(error) is ProviderError
        assert detail in str(error)
        assert isinstance(error.__cause__, ConnectionClosed)

    async def test_the_token_never_reaches_the_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The close reason is the server's text; it could echo the subscribe frame."""
        echoed = ConnectionClosedError(
            Close(CloseCode.POLICY_VIOLATION, f"bad subscribe {TOKEN}"), None, None
        )
        serve(monkeypatch, [ScriptedSocket([], then=echoed)])
        provider = TiingoProvider(["EURUSD"], token=TOKEN)
        await provider.connect()

        _, error = await read_until_closed(provider)

        assert TOKEN not in str(error)
        assert "<redacted>" in str(error)

    async def test_a_drop_during_subscribe_raises_provider_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        serve(monkeypatch, [ScriptedSocket([], then=vanished(), send_fails=keepalive_timeout())])

        with pytest.raises(ProviderError, match="keepalive ping timeout") as caught:
            await TiingoProvider(["EURUSD"], token=TOKEN).connect()
        assert TOKEN not in str(caught.value)

    async def test_a_handshake_cut_short_raises_provider_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def connect(_url: str, **_kwargs: object) -> ScriptedSocket:
            raise InvalidMessage("did not receive a valid HTTP response")

        monkeypatch.setattr(CONNECT, connect)

        with pytest.raises(ProviderError, match="did not receive a valid HTTP response"):
            await TiingoProvider(["EURUSD"], token=TOKEN).connect()

    async def test_a_rejected_token_is_still_an_auth_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rejected = json.dumps({"messageType": "E", "response": {"message": "invalid token"}})
        serve(monkeypatch, [ScriptedSocket([rejected], then=closed_cleanly())])
        provider = TiingoProvider(["EURUSD"], token=TOKEN)
        await provider.connect()

        with pytest.raises(ProviderAuthError):
            async for _tick in provider.stream():
                pass


Statuses = list[dict[str, object]]


def build(monkeypatch: pytest.MonkeyPatch) -> tuple[Ingestor, Statuses]:
    """An Ingestor on the real adapter, with Redis and the pipeline stubbed out.

    Each status is recorded with the breaker count and stale flag as they were
    when it was published, which is what the UI and the detector would see.
    """
    monkeypatch.setattr("fx_ingestor.main.make_redis", lambda _url: None)
    cfg = IngestorSettings(
        FX_PROVIDER="tiingo",
        FX_PROVIDER_TOKEN=TOKEN,
        FX_SYMBOLS="EURUSD",
        # Real sleeps, kept short: the watchdog shares asyncio.sleep, so it
        # cannot be patched out without making the watchdog spin.
        FX_BACKOFF_BASE_S=0.001,
        FX_BACKOFF_CAP_S=0.005,
        _env_file=None,  # type: ignore[call-arg]
    )
    ing = Ingestor(cfg)
    statuses: Statuses = []
    stale: list[bool] = []

    async def record(state: str, detail: str = "") -> None:
        statuses.append(
            {
                "state": state,
                "detail": detail,
                "failures": ing.breaker.failures,
                "stale": stale[-1] if stale else None,
            }
        )

    async def handle(_tick: Tick) -> None:
        return None

    monkeypatch.setattr(ing, "_publish_status", record)
    monkeypatch.setattr(ing.pipeline, "set_feed_stale", stale.append)
    monkeypatch.setattr(ing.pipeline, "handle", handle)
    return ing, statuses


class TestTheIngestor:
    async def test_it_reconnects_through_dropped_sockets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression: both closes are retried inside the same leader loop."""
        ing, statuses = build(monkeypatch)
        sockets = [
            ScriptedSocket([quote()], then=keepalive_timeout()),
            ScriptedSocket([quote()], then=closed_cleanly()),
            ScriptedSocket([quote()], then=closed_cleanly()),
        ]

        def stop_on_third(n: int) -> None:
            if n == len(sockets):
                ing._shutdown.set()  # the third session ends after its first quote

        opened = serve(monkeypatch, sockets, on_open=stop_on_third)

        # Returning at all is the point: the old code raised out of here, and
        # run() let it end the process. Returning only once shutdown was asked
        # for means the lease held by run() was never given up.
        await asyncio.wait_for(ing._run_as_leader(), timeout=5.0)

        assert len(opened) == 3, "the ingestor did not reconnect after the drops"
        assert all(sock.closed for sock in opened)
        assert [s["state"] for s in statuses] == [
            "connecting",
            "healthy",
            "degraded",
            "connecting",
            "healthy",
            "degraded",
            "connecting",
            "healthy",
        ]
        degraded = [s for s in statuses if s["state"] == "degraded"]
        assert "keepalive ping timeout" in str(degraded[0]["detail"])
        assert "1000 (OK)" in str(degraded[1]["detail"])
        assert [s["failures"] for s in degraded] == [1, 2]
        assert all(s["stale"] is True for s in degraded), (
            "the detector was still evaluating a dead feed"
        )
        assert not ing.lease.lost.is_set()
        assert all(TOKEN not in str(s["detail"]) for s in statuses)

    async def test_a_rejected_token_still_stops_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Retrying a 401 gets the key banned; the drop fix must not catch it."""
        ing, statuses = build(monkeypatch)
        rejected = json.dumps({"messageType": "E", "response": {"message": "invalid token"}})
        opened = serve(monkeypatch, [ScriptedSocket([rejected], then=closed_cleanly())])

        with pytest.raises(ProviderAuthError):
            await asyncio.wait_for(ing._run_as_leader(), timeout=5.0)

        assert len(opened) == 1
        assert statuses[-1]["state"] == "fatal"
        assert ing._shutdown.is_set()
