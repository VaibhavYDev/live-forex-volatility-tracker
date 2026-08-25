"""The HTTP and WebSocket surface, exercised end to end.

The audit's largest single gap. Every unit below this line was well covered - the
conflator at 98%, the pipeline at 98% - and the endpoint that assembles them had
never been invoked, so nothing verified that a subscribe returns a snapshot, that
the symbol cap closes the socket, or that an invalid ticket is refused. Correct
parts and unproven wiring is the exact shape of system that passes every test and
fails on first contact.

These run against the real app object with its real lifespan, and a real Redis.
Nothing here is mocked: the point is the wiring.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from fx_api.config import ApiSettings
from fx_api.main import create_app
from fx_api.routers.market import _Cache, _compare_cache
from fx_core import keys
from starlette.websockets import WebSocketDisconnect

pytestmark = pytest.mark.integration

SYMBOL = "EURUSD"
TICKET_REQUIRED_ENV = "FX_REQUIRE_WS_TICKET"


def _bar(t: int, o: float, h: float, low: float, c: float, src: str = "stream") -> str:
    return json.dumps({"t": t, "o": o, "h": h, "l": low, "c": c, "n": 12, "src": src})


def seed_bars(redis: Any, symbol: str = SYMBOL, count: int = 30) -> None:
    """Enough bars that every estimator has something to work with."""
    base = int(datetime(2026, 8, 25, 12, 0, tzinfo=UTC).timestamp())
    px = 1.0842
    members = {}
    for i in range(count):
        t = base + i * 60
        o = px
        c = px * (1.0 + (0.0004 if i % 2 else -0.0003))
        members[_bar(t, o, max(o, c) * 1.0002, min(o, c) * 0.9998, c)] = t
        px = c
    redis.zadd(keys.history(symbol), members)


def seed_feed(redis: Any, state: str = "healthy", age_s: float = 0.0) -> None:
    ts = datetime.now(UTC) - timedelta(seconds=age_s)
    redis.set(
        keys.FEED_STATUS,
        json.dumps({"state": state, "detail": "", "provider": "replay", "ts": ts.isoformat()}),
        ex=300,
    )


@pytest.fixture
def client(redis_sync: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The real app, real lifespan, real Redis, tickets off unless a test says so."""
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv(TICKET_REQUIRED_ENV, "0")
    monkeypatch.setenv("FX_RATE_LIMIT_PER_MIN", "5")
    monkeypatch.setenv("FX_TICKET_LIMIT_PER_MIN", "3")
    _compare_cache.clear()

    with TestClient(create_app()) as c:
        yield c
    _compare_cache.clear()


@pytest.fixture
def secured(redis_sync: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/15")
    monkeypatch.setenv(TICKET_REQUIRED_ENV, "1")
    monkeypatch.setenv("FX_TICKET_LIMIT_PER_MIN", "50")
    _compare_cache.clear()

    with TestClient(create_app()) as c:
        yield c


class TestHealth:
    def test_liveness_checks_nothing_external(self, client: TestClient) -> None:
        # A liveness probe that pings Redis restarts every healthy API pod during
        # a Redis blip, turning a partial outage into a total one.
        assert client.get("/healthz").json() == {"status": "alive"}

    def test_readiness_is_green_with_a_healthy_feed(
        self, client: TestClient, redis_sync: Any
    ) -> None:
        seed_feed(redis_sync, "healthy")
        r = client.get("/readyz")
        assert r.status_code == 200
        assert r.json()["ready"] is True
        assert r.json()["redis"] == "ok"

    def test_readiness_fails_on_a_stale_feed(self, client: TestClient, redis_sync: Any) -> None:
        # The replica can still answer - it would just answer with old prices, so
        # it takes itself out of the load balancer instead.
        seed_feed(redis_sync, "healthy", age_s=3600)
        r = client.get("/readyz")
        assert r.status_code == 503
        assert r.json()["feed_state"] == "stale"

    def test_readiness_fails_when_the_feed_is_fatal(
        self, client: TestClient, redis_sync: Any
    ) -> None:
        seed_feed(redis_sync, "fatal")
        assert client.get("/readyz").status_code == 503

    def test_no_heartbeat_at_all_is_not_reported_as_healthy(self, client: TestClient) -> None:
        # Absence of the key means the ingestor's TTL expired. That is an answer,
        # and it must not be optimistically rounded to "fine".
        assert client.get("/readyz").json()["feed_state"] == "unknown"

    def test_metrics_are_exported(self, client: TestClient) -> None:
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "fx_ws_clients" in r.text or "python_info" in r.text


class TestMarketRest:
    def test_status_reports_session_state(self, client: TestClient) -> None:
        body = client.get("/api/market/status").json()
        assert isinstance(body["is_open"], bool)
        assert ("next_open" in body) and ("next_close" in body)

    def test_bars_are_served_oldest_first(self, client: TestClient, redis_sync: Any) -> None:
        seed_bars(redis_sync)
        body = client.get(f"/api/bars/{SYMBOL}").json()
        stamps = [b["t"] for b in body["bars"]]
        assert stamps == sorted(stamps)
        assert body["warming_up"] is False

    def test_an_unknown_symbol_is_empty_rather_than_an_error(self, client: TestClient) -> None:
        body = client.get("/api/bars/GBPUSD").json()
        assert body["bars"] == []
        assert body["warming_up"] is True

    @pytest.mark.parametrize("bad", ["EUR", "EURUSDX", "../etc", "EUR USD", "1234567"])
    def test_a_malformed_symbol_is_rejected_before_it_reaches_redis(
        self, client: TestClient, bad: str
    ) -> None:
        # Symbols become Redis key fragments and cache keys. Unvalidated, they
        # are an unbounded set of cache entries driven by the caller.
        assert client.get(f"/api/bars/{bad}").status_code in (404, 422)

    def test_symbols_are_case_insensitive(self, client: TestClient, redis_sync: Any) -> None:
        seed_bars(redis_sync)
        assert client.get("/api/bars/eurusd").json()["symbol"] == SYMBOL

    def test_volatility_refuses_to_answer_before_it_can(self, client: TestClient) -> None:
        # Serving a sigma computed from two bars as though it were a measurement
        # is how dashboards lie.
        r = client.get(f"/api/volatility/{SYMBOL}")
        assert r.status_code == 409
        assert "warming up" in r.json()["detail"]

    def test_volatility_labels_its_basis(self, client: TestClient, redis_sync: Any) -> None:
        seed_bars(redis_sync)
        body = client.get(f"/api/volatility/{SYMBOL}?estimator=parkinson").json()
        assert body["estimator"] == "parkinson"
        assert body["window_s"] == body["bars_used"] * 60
        assert "362,880" in body["annualization_basis"]
        assert body["sigma_annualized"] > 0

    def test_compare_returns_every_estimator_over_identical_bars(
        self, client: TestClient, redis_sync: Any
    ) -> None:
        seed_bars(redis_sync)
        body = client.get(f"/api/volatility/{SYMBOL}/compare").json()
        assert set(body["estimators"]) == {
            "close_to_close",
            "parkinson",
            "garman_klass",
            "rogers_satchell",
            "yang_zhang",
        }
        assert all(v > 0 for v in body["estimators"].values())


class TestCompareCache:
    def test_the_second_call_is_served_from_cache(
        self, client: TestClient, redis_sync: Any
    ) -> None:
        seed_bars(redis_sync)
        first = client.get(f"/api/volatility/{SYMBOL}/compare")
        second = client.get(f"/api/volatility/{SYMBOL}/compare")

        assert first.headers["x-cache"] == "miss"
        assert second.headers["x-cache"] == "hit"
        assert first.json() == second.json()

    def test_the_cache_is_keyed_by_window(self, client: TestClient, redis_sync: Any) -> None:
        seed_bars(redis_sync)
        client.get(f"/api/volatility/{SYMBOL}/compare?limit=10")
        assert client.get(f"/api/volatility/{SYMBOL}/compare?limit=20").headers["x-cache"] == "miss"

    def test_warming_up_is_never_cached(self, client: TestClient, redis_sync: Any) -> None:
        # 409 is the one answer that becomes wrong on its own. Caching it would
        # keep the panel blank for 30 seconds after the data arrived.
        assert client.get(f"/api/volatility/{SYMBOL}/compare").status_code == 409
        seed_bars(redis_sync)
        assert client.get(f"/api/volatility/{SYMBOL}/compare").status_code == 200

    def test_the_cache_is_bounded(self) -> None:
        cache = _Cache(max_entries=4)
        for i in range(50):
            cache.put((f"SYM{i:03d}", 60), {"i": i})
        assert len(cache._data) == 4

    def test_entries_expire(self) -> None:
        cache = _Cache()
        cache.put((SYMBOL, 60), {"v": 1})
        assert cache.get((SYMBOL, 60), ttl_s=10) == {"v": 1}
        time.sleep(0.05)
        assert cache.get((SYMBOL, 60), ttl_s=0.01) is None


class TestRateLimit:
    def test_the_estimator_endpoint_is_limited(self, client: TestClient, redis_sync: Any) -> None:
        # Five estimators over up to 1,440 bars, polled every 15s per panel. A
        # loop here monopolises the Redis every other component shares.
        seed_bars(redis_sync)
        codes = [
            client.get(f"/api/volatility/{SYMBOL}/compare?limit={10 + i}").status_code
            for i in range(7)
        ]
        assert codes[:5] == [200] * 5
        assert codes[5:] == [429, 429]

    def test_a_rejected_request_says_when_to_come_back(
        self, client: TestClient, redis_sync: Any
    ) -> None:
        seed_bars(redis_sync)
        last = None
        for i in range(7):
            last = client.get(f"/api/volatility/{SYMBOL}/compare?limit={10 + i}")
        assert last is not None and last.status_code == 429
        assert 0 < int(last.headers["retry-after"]) <= 60

    def test_the_budget_is_per_client(self, client: TestClient, redis_sync: Any) -> None:
        seed_bars(redis_sync)
        for i in range(6):
            client.get(
                f"/api/volatility/{SYMBOL}/compare?limit={10 + i}",
                headers={"X-Forwarded-For": "10.0.0.1"},
            )
        # A different caller must not inherit the first one's exhausted budget.
        other = client.get(
            f"/api/volatility/{SYMBOL}/compare", headers={"X-Forwarded-For": "10.0.0.2"}
        )
        assert other.status_code == 200

    def test_ticket_issuance_is_limited(self, client: TestClient) -> None:
        codes = [client.post("/ws/ticket").status_code for _ in range(5)]
        assert codes[:3] == [200] * 3
        assert 429 in codes

    def test_the_window_carries_a_ttl(self, client: TestClient, redis_sync: Any) -> None:
        # A counter with no expiry locks a client out permanently.
        seed_bars(redis_sync)
        client.get(f"/api/volatility/{SYMBOL}/compare")
        key = next(iter(redis_sync.keys("rl:compare:*")))
        assert 0 < redis_sync.ttl(key) <= 60


class TestWebSocketProtocol:
    def test_hello_states_the_protocol_version(self, client: TestClient) -> None:
        with client.websocket_connect("/ws/stream") as ws:
            hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["protocol"] == 1
        assert hello["max_symbols"] == 25

    def test_subscribe_returns_a_snapshot_before_any_delta(
        self, client: TestClient, redis_sync: Any
    ) -> None:
        seed_bars(redis_sync)
        redis_sync.hset(
            keys.quote(SYMBOL), mapping={"bid": "1.084", "ask": "1.0841", "mid": "1.08405"}
        )
        with client.websocket_connect("/ws/stream") as ws:
            ws.receive_json()  # hello
            ws.send_json({"op": "subscribe", "symbols": [SYMBOL], "bars": 240})
            snap = ws.receive_json()

        assert snap["type"] == "snapshot"
        data = snap["data"][SYMBOL]
        assert data["bars"], "a client connecting mid-session would see an empty chart"
        assert data["quote"]["mid"] == pytest.approx(1.08405)
        # Present even with no history, so the pane renders its axis rather than
        # erroring.
        assert "zhist" in data
        assert data["regime"]["regime"] == "unknown"

    def test_symbols_are_normalised_server_side(self, client: TestClient) -> None:
        # ["eurusd", "EURUSD"] would otherwise occupy two subscription slots and
        # deliver every update twice.
        with client.websocket_connect("/ws/stream") as ws:
            ws.receive_json()
            ws.send_json({"op": "subscribe", "symbols": ["eurusd", "EURUSD"], "bars": 0})
            snap = ws.receive_json()
        assert list(snap["data"]) == [SYMBOL]

    def test_ping_is_answered(self, client: TestClient) -> None:
        with client.websocket_connect("/ws/stream") as ws:
            ws.receive_json()
            ws.send_json({"op": "ping"})
            assert ws.receive_json()["type"] == "pong"

    def test_an_unknown_op_is_reported_without_dropping_the_socket(
        self, client: TestClient
    ) -> None:
        with client.websocket_connect("/ws/stream") as ws:
            ws.receive_json()
            ws.send_json({"op": "teleport"})
            assert ws.receive_json()["type"] == "error"
            ws.send_json({"op": "ping"})
            assert ws.receive_json()["type"] == "pong"

    def test_a_malformed_frame_closes_with_a_protocol_error(self, client: TestClient) -> None:
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect("/ws/stream") as ws,
        ):
            ws.receive_json()
            ws.send_json({"op": "subscribe", "symbols": []})  # min_length=1
            ws.receive_json()  # error frame
            ws.receive_json()  # close
        assert exc.value.code == 4000

    def test_an_oversized_batch_is_a_protocol_error(self, client: TestClient) -> None:
        # 26 in one frame never reaches the handler: SubscribeOp caps the list at
        # max_length, so this is a malformed message (4000), not a quota breach.
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect("/ws/stream") as ws,
        ):
            ws.receive_json()
            ws.send_json({"op": "subscribe", "symbols": [f"AAA{i:03d}" for i in range(26)]})
            ws.receive_json()
            ws.receive_json()
        assert exc.value.code == 4000

    def test_the_cumulative_symbol_cap_is_enforced(self, client: TestClient) -> None:
        # The quota path, which only two valid batches can reach. Worth its own
        # close code: a client can retry after unsubscribing, which is not true
        # of a protocol violation.
        first = [f"AAA{i:03d}" for i in range(20)]
        second = [f"BBB{i:03d}" for i in range(10)]
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect("/ws/stream") as ws,
        ):
            ws.receive_json()
            ws.send_json({"op": "subscribe", "symbols": first, "bars": 0})
            ws.receive_json()
            ws.send_json({"op": "subscribe", "symbols": second, "bars": 0})
            ws.receive_json()
        assert exc.value.code == 4002

    def test_unsubscribe_is_accepted(self, client: TestClient) -> None:
        with client.websocket_connect("/ws/stream") as ws:
            ws.receive_json()
            ws.send_json({"op": "subscribe", "symbols": [SYMBOL], "bars": 0})
            ws.receive_json()
            ws.send_json({"op": "unsubscribe", "symbols": [SYMBOL]})
            ws.send_json({"op": "ping"})
            assert ws.receive_json()["type"] == "pong"


class TestWebSocketAuth:
    def test_the_feed_is_closed_without_a_ticket(self, secured: TestClient) -> None:
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            secured.websocket_connect("/ws/stream") as ws,
        ):
            ws.receive_json()
        assert exc.value.code == 4001

    def test_a_valid_ticket_is_accepted(self, secured: TestClient) -> None:
        ticket = secured.post("/ws/ticket").json()["ticket"]
        with secured.websocket_connect(f"/ws/stream?ticket={ticket}") as ws:
            assert ws.receive_json()["type"] == "hello"

    def test_a_ticket_is_single_use(self, secured: TestClient) -> None:
        # GETDEL rather than GET-then-DEL: two commands would let concurrent
        # handshakes redeem the same ticket twice.
        ticket = secured.post("/ws/ticket").json()["ticket"]
        with secured.websocket_connect(f"/ws/stream?ticket={ticket}") as ws:
            ws.receive_json()

        with (
            pytest.raises(WebSocketDisconnect) as exc,
            secured.websocket_connect(f"/ws/stream?ticket={ticket}") as ws,
        ):
            ws.receive_json()
        assert exc.value.code == 4001

    def test_a_forged_ticket_is_refused(self, secured: TestClient) -> None:
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            secured.websocket_connect("/ws/stream?ticket=not-a-real-ticket") as ws,
        ):
            ws.receive_json()
        assert exc.value.code == 4001

    def test_authentication_is_on_unless_explicitly_disabled(
        self, redis_sync: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The regression this guards: the demo posture used to be the default
        # posture, so the stream was open to anyone who could reach the port.
        monkeypatch.delenv(TICKET_REQUIRED_ENV, raising=False)
        assert ApiSettings().require_ws_ticket is True
