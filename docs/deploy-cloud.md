# Deploying to a cloud server

The target is one Linux server running `docker-compose.prod.yml`: the complete
stack behind Caddy, on a hostname with a real certificate, surviving reboots and
redeploys without losing data, with daily backups and an external health check.

Time: about an hour, most of it waiting for the cloud provider.

---

## 0. Read this first: the data licence

Tiingo's individual plans (Starter, free; Power, $30/month) are licensed for
**internal use only**. Its pricing page defines that as using the data for your
own personal use: *"you may not display or share the data with another person or
organization."* Redistribution is a separate, paid licence.

So this deployment is **private by default**: Caddy asks for a username and
password before serving the dashboard or its data. Only `/api/health` answers
without credentials, and it reports health, never a price. Do not remove the
password gate, publish a link, or post screenshots of the live dashboard until you
hold a licence that permits displaying the data to others (contact Tiingo sales,
or use a provider whose terms allow it). Section 9 lists what changes then.

---

## 1. What runs

```
internet ─443─▶ caddy ──▶ web (nginx) ──┬── static SPA
               TLS +       per-visitor   └── /api, /ws ──▶ api ──▶ redis ◀── ingestor ×2 ◀── Tiingo
               password    limits                                    │
                                                                     └──▶ worker ──▶ timescale
```

| Service | Role |
|---|---|
| caddy | TLS (Let's Encrypt), HTTP→HTTPS, password gate, security headers. The only published ports: 80 and 443 |
| web | nginx: the built dashboard, `/api` and `/ws` proxying, per-visitor limits (20 API requests/s with a burst of 60; 8 sockets per address) |
| api | FastAPI. Serves the dashboard from Redis only; refuses sockets past 200 per replica |
| ingestor ×2 | One holds the lease and the Tiingo connection; the other takes over within 10 s. Only these hold the token |
| worker | Drains the tick stream into TimescaleDB (minute bars, 5m/1h rollups, alert log) |
| redis | Hot cache and write-ahead log. Password, append-only file on a volume, no published port |
| timescale | Durable history. Password, volume, no published port. Migrations 001, 003 and 004 run on first boot |

Measured on 2026-10-26 the whole stack used about 350 MB of RAM; each service has
a memory ceiling so one fault cannot starve the database.

---

## 2. Choose a server

Any x86-64 or ARM64 Linux VM with **2 GB RAM or more**, 20 GB of disk and a public
IPv4 works; every image is published for both architectures. Checked on
2026-10-26:

| Option | Size | Price | Notes |
|---|---|---|---|
| **Oracle Cloud Always Free, Ampere A1** (recommended) | up to 2 OCPU / 12 GB in total (the allowance was halved in 2026) | $0 within the allowance | 200 GB block storage, 10 TB/month egress. Capacity in popular regions is often exhausted. **Idle reclamation**: Oracle may reclaim an Always Free instance whose CPU (95th percentile), network and memory use all stay under 20% for 7 days; this stack is light, so size the VM small (1 OCPU / 4 GB, the stack then uses well over 20% of its memory) or upgrade the account to Pay As You Go, which Oracle stated in 2023 keeps idle instances from being reclaimed (its current documentation does not say) and costs nothing within the allowance |
| DigitalOcean Basic Droplet | 1 vCPU / 2 GB / 50 GB | $12/month (+20% for weekly backups) | Predictable, no capacity lottery, snapshots and backups built in |
| Hetzner Cloud CX23 | 2 vCPU / 4 GB / 40 GB | €5.99/month | Cost-optimized plans are listed as currently unavailable; the regular CPX12 (1 vCPU / 2 GB) is €11.99 |

Prices change; check the provider's page before creating anything.

### Oracle: create the instance

Console → **Compute → Instances → Create instance**: Canonical Ubuntu 24.04, shape
`VM.Standard.A1.Flex` with 1 OCPU and 4 GB (see the reclamation note above), boot
volume 50 GB, your SSH public key. Note the public IPv4 address.

> **"Out of host capacity."** A1 is oversubscribed in popular regions. Try each
> availability domain in turn and retry over a few hours. Do not change your home
> region to chase capacity; that is irreversible.

### Oracle: open the two firewalls

**The virtual one (always required).** Networking → your VCN → the public subnet →
its **Security List** → *Add Ingress Rules*: TCP 80 and TCP 443 from `0.0.0.0/0`.

**The one on the instance (check before changing).** Oracle's Ubuntu image ships an
iptables ruleset ending in a `REJECT`. Docker usually makes it irrelevant: published
ports are DNAT'd and traverse `FORWARD`, not `INPUT`. After step 6, from your own
machine:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://<PUBLIC_IP>/
```

`308` means you are done. A hang means `INPUT` is in the path: add these **above**
the final `REJECT` line of `/etc/iptables/rules.v4`, then
`sudo iptables-restore < /etc/iptables/rules.v4`:

```
-A INPUT -p tcp -m state --state NEW -m tcp --dport 80 -j ACCEPT
-A INPUT -p tcp -m state --state NEW -m tcp --dport 443 -j ACCEPT
```

Other providers: allow inbound TCP 22, 80 and 443 and nothing else.

---

## 3. A hostname

Caddy needs one; Let's Encrypt does not issue certificates for bare IP addresses.
Point an `A` record at the server, or claim a free `yourname.duckdns.org` at
[DuckDNS](https://duckdns.org). Confirm it resolves before continuing:

```bash
dig +short yourname.duckdns.org
```

---

## 4. Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
sudo systemctl enable --now docker
newgrp docker
```

`enable` brings the stack back after a reboot, together with
`restart: unless-stopped` on every service.

---

## 5. Settings

```bash
git clone https://github.com/VaibhavYDev/live-forex-volatility-tracker.git
cd live-forex-volatility-tracker
cp -n .env.cloud.example .env.cloud && chmod 600 .env.cloud
openssl rand -hex 24   # run twice: POSTGRES_PASSWORD and REDIS_PASSWORD
docker run --rm caddy:2.11.7-alpine caddy hash-password --plaintext '<dashboard password>'
${EDITOR:-nano} .env.cloud
```

Fill in `FX_DOMAIN`, `FX_ACCESS_USER`, `FX_ACCESS_HASH` (inside the single quotes),
`FX_PROVIDER_TOKEN`, `POSTGRES_PASSWORD` and `REDIS_PASSWORD`. The stack refuses to
start while any is missing. The token stays on the server: only the ingestors see
it, and the browser talks to this API, never to Tiingo.

The Tiingo token's request allowance (Starter: 50 an hour, 1,000 a day) is shared
by every copy of the stack using it. Run one stack per token: a laptop stack and a
cloud stack on the same token each load history hourly and on every leader change.

---

## 6. Deploy

```bash
scripts/cloud/deploy.sh origin/main
```

It builds the commit's images tagged with the commit, starts them, and waits up to
three minutes for all eight containers to report healthy
(`scripts/cloud/verify.sh`). If they do not, it rolls back to the previous release.
The first build takes about five minutes on a small VM.

Volumes are never touched by a deploy or rollback: the database, Redis and the
certificates survive both.

---

## 7. Verify

```bash
docker compose -f docker-compose.prod.yml --env-file .env.cloud ps
curl -s https://yourname.duckdns.org/api/health
```

`/api/health` returns 200 and `"ok": true` when the ingestor's heartbeat is fresh,
the feed is healthy (or the market is closed, or it is the quiet rollover hour), and
the worker holds its lease and keeps up with the tick stream; otherwise 503 with
the problems listed. Then open `https://yourname.duckdns.org`, sign in, and check:

- the banner reads **Live** while the market is open, **Market closed** at the
  weekend, and **Stale** if no quote has arrived for a while;
- all five pairs and all seven timeframes draw, with candles from provider history
  labelled as such.

---

## 8. Operate it

**Monitoring.** Point a free external uptime monitor (UptimeRobot, Healthchecks.io,
Better Stack) at `https://yourname.duckdns.org/api/health`, every 5 minutes,
alerting on anything but 200. That covers a dead server, a dead feed, a dead worker
and an expired certificate. A closed market is not an alarm.

**Backups.** Daily, verified, with an optional off-server copy:

```bash
sudo cp infra/systemd/fx-backup.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now fx-backup.timer
scripts/cloud/backup.sh   # run once by hand to see it work
```

Each backup holds a `pg_dump` of the database and a Redis RDB snapshot, checked
before it counts, with checksums. Copies older than 30 days are removed locally.
For an off-server copy install [rclone](https://rclone.org), configure a remote
(Oracle Object Storage, S3, Backblaze B2, Google Drive...) and set
`FX_BACKUP_REMOTE` in `infra/systemd/fx-backup.service`. Remote copies are never
deleted by the script.

**Restore.** Into a new database first, then swap it in, so the current data stays
until the restored copy is known to be good (rehearsed 2026-10-26 on a copy of
this stack: 0 errors, the worker resumed writing within a minute):

```bash
C="docker compose -f docker-compose.prod.yml --env-file .env.cloud"
$C exec -T timescale psql -U fx -d postgres -c "CREATE DATABASE fx_restore"
$C exec -T timescale psql -U fx -d fx_restore -c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()"
$C exec -T timescale pg_restore -U fx -d fx_restore --no-owner < /path/to/backup/fx.dump
$C exec -T timescale psql -U fx -d fx_restore -c "SELECT timescaledb_post_restore()" -c "SELECT count(*), max(bucket) FROM bars_1m"
$C stop api worker ingestor
$C exec -T timescale psql -U fx -d postgres -c "ALTER DATABASE fx RENAME TO fx_before_restore" -c "ALTER DATABASE fx_restore RENAME TO fx"
$C restart timescale        # restarts TimescaleDB's background jobs after the rename
$C up -d
```

Drop `fx_before_restore` only once the restored data has been checked.

Redis rebuilds its hot cache from the feed within a day; to restore it as well,
copy `redis.rdb` into the `redis-data` volume as `dump.rdb` before starting Redis.

**Update.** `scripts/cloud/deploy.sh origin/main` again. **Roll back** to the
release before: `scripts/cloud/rollback.sh`.

**Leader failover.** `docker compose -f docker-compose.prod.yml --env-file .env.cloud
restart ingestor` shows the standby promote within the lease TTL.

**Gaps.** If the server was down while the market was open, minutes are missing.
History recovery fills them from the provider's one-minute history but is off for
unattended use (`FX_RECOVERY_MODE=off`): it spends the shared request allowance.
Run it by hand, dry run first:

```bash
docker compose -f docker-compose.prod.yml --env-file .env.cloud exec ingestor \
  python -m fx_ingestor.recovery --since 2026-10-26T00:00Z
```

**The daily rollover.** Every day at 17:00 New York the market goes quiet and
spreads widen for about an hour. The ingestor allows up to 5 minutes of silence
there before reconnecting (`FX_STALENESS_TIMEOUT_ROLLOVER_S`), and the regime
detector does not score that hour (`FX_ROLLOVER_GATE_MIN`). Quotes the provider
sends outside the New York trading week (it quotes from about 20:00 UTC on Sunday
and after the Friday close) are stored as received, never scored by the detector,
and counted separately by the volatility estimators (`bars_outside_market_hours`).

---

## 9. Going public

Only when a licence permits displaying the data to others:

1. Remove the `basic_auth` block from `infra/caddy/Caddyfile` (keep the per-visitor
   limits in nginx and the client cap in the API).
2. Redeploy, and check from a browser on another network with no local setup that
   the dashboard loads, streams and shows correct market-state banners.
3. Add the URL to the README's **Live dashboard** section.

---

## Costs

Oracle Always Free: nothing, while the instance stays within the allowance (Oracle
does not bill a free-tier account without an explicit upgrade). DigitalOcean or
Hetzner: the plan price above, plus backups if enabled. Off-server backup storage is
a few megabytes a day. A Tiingo redistribution licence, if you go public, is priced
by Tiingo.
