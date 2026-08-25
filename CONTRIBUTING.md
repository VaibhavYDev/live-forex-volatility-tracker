# Contributing

Thanks for looking. This document covers getting the stack running, the shape of
the test suite, and the two conventions that matter more here than they do in
most projects.

## The short version

```bash
cp .env.example .env
docker compose up -d          # Redis, TimescaleDB, ingestor, worker, API, web
open http://localhost:5173
```

That is the whole demo path. The default provider is `replay`, a deterministic
synthetic feed, so nothing above needs an API key or a network connection to a
market data vendor.

## Working on the code

Python is managed with [uv](https://docs.astral.sh/uv/); the frontend with npm.

```bash
uv sync --all-packages
cd apps/web && npm ci && cd ../..

# Services the integration tier needs
docker compose up -d redis timescale
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f infra/migrations/001_init.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f infra/migrations/003_alert_events.sql
```

Everything below is also wired into `make` targets if you prefer those.

## The test suite has four tiers

| Tier | Command | Needs | Runs in CI |
|---|---|---|---|
| Unit | `uv run pytest tests/unit` | nothing | yes |
| Integration | `uv run pytest tests/integration` | Redis, PostgreSQL | yes |
| Chaos | `uv run pytest tests/chaos` | Redis | yes |
| Load | `uv run pytest tests/load -m load -s` | Redis | nightly |
| Frontend | `cd apps/web && npm test` | nothing | yes |

Load tests are excluded from the default run (`-m "not load"`) because they take
minutes rather than seconds. They still run on a schedule, because the O(1)
property they assert is the kind that degrades silently.

**Integration and chaos tests skip when Redis or PostgreSQL are unreachable, so
you can work on the pure-maths core without Docker.** In CI that skip is turned
into a failure by `FX_REQUIRE_SERVICES=1` — otherwise a broken service container
would quietly reduce the suite to unit tests and the build would still pass.

### Before you open a pull request

```bash
uv run ruff format . && uv run ruff check . --fix
uv run mypy packages apps          # strict, and it must stay clean
uv run pytest tests
cd apps/web && npx tsc --noEmit && npm test && npm run build
```

## Two conventions worth reading

### 1. Claims come with numbers

Any statement about performance, correctness or behaviour needs something
executable behind it. `docs/benchmarks.md` exists so that every performance
adjective in the README traces to a measurement, and it has a *"what is not
benchmarked yet"* section because listing the gaps is part of the benchmark.

If you add a claim, add the test or the benchmark that supports it. If you cannot
measure it yet, say so explicitly rather than rounding up.

### 2. Comments explain *why*, never *what*

The code says what it does. Comments are for the decision behind it — the
constraint you hit, the alternative you rejected, the bug that will come back if
someone "simplifies" this later. Some of the most useful comments in this
codebase describe failures we *measured* and that turned out to be different from
what we assumed:

```python
# Without it a promoted standby starts cold, re-baselines on the elevated data,
# and silently reports NORMAL through the rest of an ongoing event - never
# emitting the clear that the stored 'stressed' row is waiting for. Measured in
# tests/chaos/test_regime_pipeline.py; we had assumed it would merely re-fire
# the escalation, which would have been the kinder bug.
```

If a comment would still be true after you deleted the code under it, it is
probably restating the obvious.

## Architecture decisions

Non-obvious choices live in [`docs/adr/`](docs/adr/) — nine of them so far,
covering the leader lease, Redis eviction policy, conflation, idempotency and
why this is not built on Kafka. If you are changing something an ADR covers,
update the ADR in the same pull request. If you are making a decision future
readers will question, write a new one.

## Reporting a bug

Include what you expected, what happened, and the smallest reproduction you can
manage. If it involves the regime detector, the seed and the tick sequence matter
— `tests/chaos/sine_feed.py` produces deterministic feeds and is usually the
fastest way to hand over a reproducible case.

Security issues go to [SECURITY.md](SECURITY.md), not the issue tracker.

## Code of conduct

Participation is governed by [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
