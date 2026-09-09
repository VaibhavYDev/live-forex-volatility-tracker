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


# ---------------------------------------------------------------- prod profile

PROD = Path(__file__).resolve().parents[2] / "docker-compose.prod.yml"


@pytest.fixture(scope="module")
def prod() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(PROD.read_text())
    return doc


class TestPublicDemo:
    """The prod file faces the internet unattended for months.

    Every invariant here is one that fails silently rather than loudly: a demo
    that is up but wrong looks identical to a demo that is fine, right up until
    the reviewer it was built for opens it.
    """

    def test_only_the_tls_terminator_is_reachable(self, prod: dict[str, Any]) -> None:
        # Publishing anything else re-opens the hole the whole design closes:
        # the API on a raw port means a second firewall rule on two firewalls,
        # and a plaintext path around the certificate.
        exposed = {name: svc["ports"] for name, svc in prod["services"].items() if svc.get("ports")}
        assert set(exposed) == {"caddy"}, f"unexpected public ports: {exposed}"

    def test_the_terminator_serves_both_http_and_https(self, prod: dict[str, Any]) -> None:
        # 80 is not optional: it is how the ACME HTTP-01 challenge completes and
        # how a visitor typing the bare hostname gets redirected to TLS.
        published = {p.split(":")[0] for p in prod["services"]["caddy"]["ports"]}
        assert published == {"80", "443"}

    def test_the_scaled_ingestor_still_claims_no_host_port(self, prod: dict[str, Any]) -> None:
        # The original outage, restated for the profile that nobody watches.
        ing = prod["services"]["ingestor"]
        assert ing["deploy"]["replicas"] >= 2
        assert not ing.get("ports")

    def test_every_service_restarts_itself(self, prod: dict[str, Any]) -> None:
        # The box reboots for kernel patches. Nothing on it is started by hand.
        for name, svc in prod["services"].items():
            assert svc.get("restart") == "unless-stopped", f"{name} would stay down"

    def test_every_service_caps_its_logs(self, prod: dict[str, Any]) -> None:
        # 25 ticks/sec for months on a fixed boot volume. Unbounded json-file
        # logs fill the disk, and a full disk takes down every container at once
        # for a reason that looks nothing like logging.
        for name, svc in prod["services"].items():
            opts = (svc.get("logging") or {}).get("options", {})
            assert opts.get("max-size"), f"{name} has uncapped logs"
            assert opts.get("max-file"), f"{name} never rotates logs"

    def test_certificates_survive_a_restart(self, prod: dict[str, Any]) -> None:
        # Without a persistent /data, Caddy re-issues on every restart and burns
        # through the Let's Encrypt rate limit, after which the demo serves an
        # untrusted certificate for a week.
        mounts = prod["services"]["caddy"]["volumes"]
        assert any(str(m).endswith(":/data") for m in mounts), "no persistent cert store"

    def test_the_database_is_absent_on_purpose(self, prod: dict[str, Any]) -> None:
        # Guards the claim the file makes: the dashboard reads Redis only. If
        # someone later couples the API to Postgres, this profile breaks in
        # production and this test is the cheapest place to find out.
        assert "timescale" not in prod["services"]
        assert "worker" not in prod["services"]

    def test_the_api_really_does_not_open_a_database(self) -> None:
        """The assumption the whole profile rests on, checked at the source.

        Dropping Timescale is only safe while the API serves the dashboard from
        Redis alone. The moment someone adds a query, the demo starts returning
        500s on a box nobody is watching — so assert the absence of a driver
        rather than trusting a comment.
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
