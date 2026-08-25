"""The switch that stops CI passing by testing less than it thinks it is.

Locally an unreachable Redis skips its tests so the pure-maths core can be worked
on without Docker. In CI that same behaviour is a trap: a service container that
fails its health check silently reduces 162 tests to 109 and the build still goes
green. This is the guard, and it gets its own tests because a safety mechanism
nobody exercises is decoration.

It has already earned its keep once: during the sweep that added it, PostgreSQL
died mid-run and the suite failed loudly instead of quietly reporting a pass over
53 tests that never executed.
"""

from __future__ import annotations

import pytest

from tests.conftest import _services_required


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_any_truthy_value_arms_the_guard(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("FX_REQUIRE_SERVICES", value)
    assert _services_required() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "  "])
def test_falsey_and_empty_values_leave_it_disarmed(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # An empty string is what you get from `FX_REQUIRE_SERVICES=` in a shell or a
    # compose file, and it must mean "off" rather than "on".
    monkeypatch.setenv("FX_REQUIRE_SERVICES", value)
    assert _services_required() is False


def test_absent_means_local_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FX_REQUIRE_SERVICES", raising=False)
    assert _services_required() is False
