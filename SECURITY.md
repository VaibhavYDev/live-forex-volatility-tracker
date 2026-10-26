# Security Policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub Security Advisories](https://github.com/vaibhavyadav/live-forex-volatility-tracker/security/advisories/new)
rather than opening a public issue.

Expect an acknowledgement within 72 hours. If you have not heard back in a week,
assume the message went astray and open a public issue saying only that you are
waiting on a security response — no details.

## What this project is, and what that means for its threat model

This is a portfolio and demonstration system for streaming FX volatility. It is
designed to be run locally or on a single host, and it carries no user accounts,
no personal data and no funds. The deployment posture below is stated explicitly
because "it is only a demo" is how demos end up on the public internet with their
defaults intact.

### Defaults

| Control | Default | Notes |
|---|---|---|
| WebSocket ticket auth | **required** | `FX_REQUIRE_WS_TICKET=0` disables it for local development only |
| Ticket lifetime | 30 s, single use | Redeemed with `GETDEL`, so a replay finds nothing |
| REST rate limit | 30 req/min per client on the estimator endpoint | The only endpoint that does real work per request |
| CORS | `http://localhost:5173` | Must be set explicitly for any other origin |
| Redis / PostgreSQL | no external ports in production compose | The local compose file publishes them for inspection; do not copy it to a server |
| Market-data token | server-side only | Read from `.env` by the ingestor; sent to Tiingo in a header, never in a URL, a log line or the browser bundle |
| Cloud deployment access | password (Caddy basic auth over TLS) | Everything but `/api/health` requires it, because the provider's individual plans license the data for internal use only. See docs/deploy-cloud.md |
| Per-visitor limits (cloud and local web image) | 20 API requests/s per address, burst 60; 8 sockets per address | nginx `limit_req` / `limit_conn`, keyed on the address Caddy forwards |
| WebSocket clients per API replica | 200 | One more is refused with close code 1013 (try again later) |
| Redis / PostgreSQL in the cloud deployment | password required, not published | The provider token is given to the ingestors only, never to the API or worker |

### Known limitations — deliberate, and listed rather than hidden

* **Ticket issuance is unauthenticated.** `POST /ws/ticket` returns a ticket for
  subject `anonymous`. The mechanism exists so that real authentication can be
  dropped in at exactly one place — the `subject` argument — but this repository
  ships no user model, so anyone who can reach the endpoint can obtain a ticket.
  The rate limit bounds abuse; it does not make the feed private.
* **There is no authorisation model.** Every client that can connect sees every
  symbol the ingestor tracks.
* **The default credentials in `.env.example` are `fx`/`fx`.** They exist to make
  `docker compose up` work on a laptop. Change them before running anywhere else.
* **Metrics endpoints are unauthenticated** on ports 9100/9101/8000. They expose
  operational data only, but they should not be internet-facing.
* **The cloud password gate is one shared credential.** HTTP basic auth over TLS
  keeps the dashboard private; it is not per-user access control. Rotate it by
  setting a new hash in `.env.cloud` and redeploying.

If you find something that is *not* on this list, it is a bug and we want to hear
about it.

## Supported versions

The project is pre-1.0. Only `main` receives fixes.

## Dependencies

Dependencies are pinned by `uv.lock` and `package-lock.json`, and Dependabot
raises pull requests for security advisories weekly across pip, npm, Docker and
GitHub Actions. See [`.github/dependabot.yml`](.github/dependabot.yml).
