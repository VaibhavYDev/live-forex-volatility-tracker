"""The web container's proxy must find the API again after the API is recreated.

WHY THIS EXISTS
---------------
`proxy_pass http://api:8000` resolves `api` once, when nginx starts, and keeps
that address. A `docker compose up` that recreated only the Python services gave
the API a new address, and every /api and /ws request through the dashboard then
failed with 502 until the web container was restarted as well. The fix resolves
the upstream per request through Docker's DNS; these checks keep it that way.
"""

from __future__ import annotations

import re
from pathlib import Path

CONF = Path(__file__).resolve().parents[2] / "infra" / "docker" / "nginx.conf"


def _location(text: str, path: str) -> str:
    match = re.search(r"location\s+" + re.escape(path) + r"\s*\{(.*?)\n    \}", text, re.S)
    assert match, f"no `location {path}` block in {CONF.name}"
    return match.group(1)


def test_the_api_is_resolved_at_request_time() -> None:
    text = CONF.read_text()
    assert re.search(r"^\s*resolver\s+127\.0\.0\.11\b", text, re.M), "no Docker DNS resolver"
    for path in ("/api/", "/ws/"):
        upstream = re.search(r"proxy_pass\s+([^;]+);", _location(text, path))
        assert upstream, f"{path} proxies nowhere"
        assert upstream.group(1).startswith("$"), (
            f"{path} proxies to the literal {upstream.group(1)}, which nginx resolves once "
            "and keeps after the api container moves"
        )


def test_the_websocket_upgrade_and_timeouts_are_kept() -> None:
    ws = _location(CONF.read_text(), "/ws/")
    for directive in (
        "proxy_http_version 1.1;",
        "proxy_set_header Upgrade $http_upgrade;",
        'proxy_set_header Connection "upgrade";',
        "proxy_read_timeout 3600s;",
        "proxy_send_timeout 3600s;",
        "proxy_buffering off;",
    ):
        assert directive in ws, f"/ws/ lost `{directive}`"
