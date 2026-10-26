"""The compose file is part of the product, so it gets tested like the rest of it.

WHY THIS EXISTS
---------------
The README's first instruction is ``docker compose up -d``. It was broken: the
ingestor declared ``ports: ["9100:9100"]`` alongside ``deploy.replicas: 2``, and
two containers cannot bind one fixed host port. The second replica died with
"port is already allocated", ``compose up`` aborted, and the web UI - the last
service in the graph - never started at all. The failure surfaced as
"localhost:5173 does not load", which points nowhere near the cause.

``docker compose config`` does NOT catch this. It validates schema, and a fixed
host port on a scaled service is schema-valid; the collision only exists at
runtime, on the second container. So the check has to be written by hand, and it
runs in the unit tier because it needs no services - only the file.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
import yaml

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(COMPOSE.read_text())
    return doc


def _published(port: Any) -> tuple[str, str] | None:
    """Return (host_ip, host_port) for a published port, or None if not published.

    Compose accepts several shapes: "8000:8000", "127.0.0.1:8000:8000", "8000"
    and the long dict form. Only the ones that actually claim a host port matter.
    """
    if isinstance(port, dict):
        pub = port.get("published")
        return (str(port.get("host_ip", "0.0.0.0")), str(pub)) if pub else None

    parts = str(port).split(":")
    if len(parts) == 1:  # "9100" - container port only, Docker picks the host one
        return None
    if len(parts) == 2:  # "9100:9100"
        return ("0.0.0.0", parts[0])
    return (parts[0], parts[1])  # "127.0.0.1:9100:9100"


def _replicas(svc: dict[str, Any]) -> int:
    return int((svc.get("deploy") or {}).get("replicas", 1))


def test_no_scaled_service_publishes_a_fixed_host_port(compose: dict[str, Any]) -> None:
    """The exact regression.

    A service with N>1 replicas and a single fixed host port is not a
    misconfiguration that degrades - it is one that takes the whole stack down,
    because `compose up` aborts on the first container that cannot start.
    """
    offenders = [
        (name, _published(p))
        for name, svc in compose["services"].items()
        if _replicas(svc) > 1
        for p in svc.get("ports", [])
        if _published(p) is not None
    ]
    assert not offenders, (
        f"{offenders} publish a fixed host port while scaled; the second replica "
        "will fail with 'port is already allocated' and abort `compose up`"
    )


def test_no_two_services_claim_the_same_host_port(compose: dict[str, Any]) -> None:
    claims: dict[tuple[str, str], list[str]] = defaultdict(list)
    for name, svc in compose["services"].items():
        for p in svc.get("ports", []):
            pub = _published(p)
            if pub:
                claims[pub].append(name)

    clashes = {port: users for port, users in claims.items() if len(users) > 1}
    assert not clashes, f"host port collision: {clashes}"


def test_the_ingestor_still_runs_more_than_one_replica(compose: dict[str, Any]) -> None:
    # The fix for the port clash must not be "drop to one replica". Two replicas
    # ARE the leader-election demo, and losing them to make a port free would be
    # deleting the feature to fix the symptom.
    assert _replicas(compose["services"]["ingestor"]) >= 2


def test_datastores_are_not_published_to_the_lan(compose: dict[str, Any]) -> None:
    """Redis and Postgres stay on loopback.

    They are published at all so a developer can point `psql` or `redis-cli` at
    them. Bound to 0.0.0.0 with the default credentials in .env.example, that is
    an open database on every network the laptop joins.
    """
    for name in ("redis", "timescale", "worker"):
        for p in compose["services"][name].get("ports", []):
            pub = _published(p)
            if pub:
                assert pub[0] == "127.0.0.1", f"{name} publishes {pub[1]} on {pub[0]}"


def test_every_service_the_demo_needs_is_in_the_default_profile(
    compose: dict[str, Any],
) -> None:
    # `docker compose up -d` starts only the default profile. If the API or the
    # web UI ever landed behind one, the README's one-command path would come up
    # missing exactly the part the reader was told to open.
    for name in ("redis", "timescale", "ingestor", "worker", "api", "web"):
        assert not compose["services"][name].get("profiles"), (
            f"{name} is profile-gated but the README tells people to expect it "
            "after a plain `docker compose up -d`"
        )


def test_every_default_service_comes_back_after_a_reboot(compose: dict[str, Any]) -> None:
    """Docker restarts only the containers that carry a restart policy.

    The Python services had one through the anchor and ``web`` set its own, but
    Redis and Timescale had none. After a reboot Docker brought the API, worker
    and ingestors back and left the datastores exited, so the API failed on a
    Redis connection timeout and the rest crash-looped until someone ran
    ``docker start`` on the two of them by hand.
    """
    default = {name: svc for name, svc in compose["services"].items() if not svc.get("profiles")}
    assert {"redis", "timescale"} <= set(default)
    stays_down = [name for name, svc in default.items() if svc.get("restart") != "unless-stopped"]
    assert not stays_down, f"{stays_down} would stay down after a reboot"


def test_prometheus_scrapes_every_ingestor_replica(compose: dict[str, Any]) -> None:
    """A static target resolves to ONE replica at random on each scrape.

    Docker's embedded DNS returns an A record per container, so
    `static_configs: [ingestor:9100]` interleaves leader and standby metrics
    under a single series — worst when the thing being watched is which replica
    holds the lease. DNS SD enumerates them instead.
    """
    prom = yaml.safe_load((COMPOSE.parent / "infra/grafana/prometheus.yml").read_text())
    jobs = {j["job_name"]: j for j in prom["scrape_configs"]}

    assert "dns_sd_configs" in jobs["ingestor"], (
        "the ingestor is scaled, so it needs DNS service discovery rather than a static target"
    )
    assert "static_configs" not in jobs["ingestor"]


def test_no_setup_instruction_replaces_an_existing_env() -> None:
    """A quickstart pasted into an existing checkout replaced the .env there.

    `cp .env.example .env` overwrites without asking, and in PowerShell `cp` is
    Copy-Item, which does the same. The next `docker compose up -d` recreated the
    services from the copy, without a token. `cp -n` never overwrites (and fails
    outright in PowerShell), so pasting the quickstart again is harmless.
    """
    for name in ("README.md", "CONTRIBUTING.md", "docker-compose.yml"):
        for n, line in enumerate((COMPOSE.parent / name).read_text().splitlines(), 1):
            assert not re.search(r"\bcp\s+\.env\.example\s+\.env\b", line), (
                f"{name}:{n} would overwrite an existing .env: {line.strip()}"
            )


def test_the_services_read_their_settings_through_fx_env_file(
    compose: dict[str, Any],
) -> None:
    # The pointer in a checkout's .env is what keeps the token outside the tree
    # and survives a plain `docker compose up` from a fresh shell.
    for name in ("ingestor", "worker", "api"):
        assert compose["services"][name]["env_file"] == ["${FX_ENV_FILE:-.env}"]


# ---------------------------------------------------------------- prod profile

PROD = Path(__file__).resolve().parents[2] / "docker-compose.prod.yml"


@pytest.fixture(scope="module")
def prod() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(PROD.read_text())
    return doc


CADDYFILE = Path(__file__).resolve().parents[2] / "infra" / "caddy" / "Caddyfile"


def _env(svc: dict[str, Any]) -> dict[str, Any]:
    env: dict[str, Any] = svc.get("environment") or {}
    return env


class TestCloudDeployment:
    """The prod file runs the whole stack on a server nobody watches.

    Every invariant here is one that fails silently rather than loudly: a server
    that is up but wrong looks identical to one that is fine until someone relies
    on it.
    """

    def test_only_the_tls_terminator_is_reachable(self, prod: dict[str, Any]) -> None:
        # Publishing anything else re-opens the hole the whole design closes: the
        # API on a raw port is a plaintext path around the certificate and the
        # access gate; Redis or PostgreSQL on one is the data itself.
        exposed = {name: svc["ports"] for name, svc in prod["services"].items() if svc.get("ports")}
        assert set(exposed) == {"caddy"}, f"unexpected public ports: {exposed}"

    def test_the_terminator_serves_both_http_and_https(self, prod: dict[str, Any]) -> None:
        # 80 is not optional: it is how the ACME HTTP-01 challenge completes and how
        # a visitor typing the bare hostname gets redirected to TLS.
        ports = prod["services"]["caddy"]["ports"]
        assert {p.rsplit(":", 1)[1] for p in ports} == {"80", "443"}
        assert {re.search(r":-(\d+)}", p).group(1) for p in ports} == {"80", "443"}  # type: ignore[union-attr]

    def test_the_scaled_ingestor_still_claims_no_host_port(self, prod: dict[str, Any]) -> None:
        ing = prod["services"]["ingestor"]
        assert ing["deploy"]["replicas"] >= 2
        assert not ing.get("ports")

    def test_every_service_restarts_itself(self, prod: dict[str, Any]) -> None:
        # The server reboots for kernel patches. Nothing on it is started by hand.
        for name, svc in prod["services"].items():
            assert svc.get("restart") == "unless-stopped", f"{name} would stay down"

    def test_every_service_caps_its_logs(self, prod: dict[str, Any]) -> None:
        for name, svc in prod["services"].items():
            opts = (svc.get("logging") or {}).get("options", {})
            assert opts.get("max-size"), f"{name} has uncapped logs"
            assert opts.get("max-file"), f"{name} never rotates logs"

    def test_every_service_has_a_memory_ceiling(self, prod: dict[str, Any]) -> None:
        # One runaway process must not take the database down with it on a small VM.
        for name, svc in prod["services"].items():
            limit = (((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}).get(
                "memory"
            )
            assert limit, f"{name} can use all of the server's memory"

    def test_certificates_and_data_survive_a_restart(self, prod: dict[str, Any]) -> None:
        # Without a persistent /data Caddy re-issues on every restart and burns the
        # Let's Encrypt rate limit; without named volumes a redeploy loses history.
        def mounts(name: str) -> list[str]:
            return [str(m) for m in prod["services"][name]["volumes"]]

        assert any(m.startswith("caddy-data:") and m.endswith(":/data") for m in mounts("caddy"))
        assert any(m.startswith("timescale-data:") for m in mounts("timescale"))
        assert any(m.startswith("redis-data:") for m in mounts("redis"))
        assert {"caddy-data", "timescale-data", "redis-data"} <= set(prod["volumes"])

    def test_the_full_stack_runs(self, prod: dict[str, Any]) -> None:
        # Durable history, the alert log and gap recovery need the database and the
        # worker; the cache-only subset this file used to be served the dashboard
        # but kept nothing older than a day.
        assert {"caddy", "web", "api", "ingestor", "worker", "redis", "timescale"} <= set(
            prod["services"]
        )
        initdb = [
            m
            for m in prod["services"]["timescale"]["volumes"]
            if "docker-entrypoint-initdb.d" in str(m)
        ]
        for migration in ("001_init.sql", "003_alert_events.sql", "004_history_recovery.sql"):
            assert any(migration in str(m) for m in initdb), (
                f"{migration} is not applied on first boot"
            )

    def test_the_datastores_require_passwords(self, prod: dict[str, Any]) -> None:
        redis = prod["services"]["redis"]
        assert "--requirepass" in redis["command"]
        assert "${REDIS_PASSWORD}" in redis["command"]
        assert _env(prod["services"]["timescale"])["POSTGRES_PASSWORD"] == "${POSTGRES_PASSWORD}"
        for name in ("api", "ingestor", "worker"):
            env = _env(prod["services"][name])
            assert "${REDIS_PASSWORD:?" in env["REDIS_URL"], (
                f"{name} reaches Redis without a password"
            )
            assert "${POSTGRES_PASSWORD:?" in env["DATABASE_URL"]

    def test_only_the_ingestor_holds_the_provider_token(self, prod: dict[str, Any]) -> None:
        holders = {
            name for name, svc in prod["services"].items() if "FX_PROVIDER_TOKEN" in _env(svc)
        }
        assert holders == {"ingestor"}
        assert _env(prod["services"]["ingestor"])["FX_PROVIDER_TOKEN"].startswith(
            "${FX_PROVIDER_TOKEN:?"
        )

    def test_unattended_recovery_stays_off(self, prod: dict[str, Any]) -> None:
        # Recovery spends the shared provider allowance and writes history; it is not
        # approved to run unattended.
        assert _env(prod["services"]["ingestor"])["FX_RECOVERY_MODE"] == "off"

    def test_the_dashboard_is_behind_the_access_gate(self) -> None:
        # The provider's individual plans license the data for internal use only.
        # Everything but the health monitor needs credentials; the monitor reports
        # health, never a price.
        text = CADDYFILE.read_text()
        assert re.search(r"@monitor\s+path\s+/api/health\n", text), (
            "the monitor path is not exactly /api/health"
        )
        gated = text[text.index("\thandle {") :]
        assert "basic_auth" in gated and "{$FX_ACCESS_USER} {$FX_ACCESS_HASH}" in gated
        assert text.count("reverse_proxy web:80") == 2, "a route bypasses the gate"

    def test_the_api_really_does_not_open_a_database(self) -> None:
        """The dashboard is served from Redis alone; the database is the durable store.

        Keeping the API free of a database driver keeps page loads off PostgreSQL and
        keeps the dashboard up while the database restarts.
        """
        api = Path(__file__).resolve().parents[2] / "apps" / "api" / "fx_api"
        drivers = ("asyncpg", "psycopg", "sqlalchemy", "databases")
        offenders = [
            f"{path.relative_to(api)}:{n}"
            for path in api.rglob("*.py")
            for n, line in enumerate(path.read_text().splitlines(), 1)
            if line.startswith(("import ", "from ")) and any(d in line for d in drivers)
        ]
        assert not offenders, f"API imports a database driver: {offenders}"
