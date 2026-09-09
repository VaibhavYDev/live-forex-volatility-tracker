# Deploying the public demo on Oracle Cloud Always Free

The target is one Ampere A1 (ARM64) instance running `docker-compose.prod.yml`
behind Caddy, on a hostname with a real certificate, surviving reboots without
anyone logging in.

Budget: **zero**, permanently. Time: about 40 minutes, most of it waiting for
Oracle.

---

## 0. What you are building

```
internet ──443──▶ caddy ──▶ web (nginx) ──┬── static SPA
                   TLS                    └── /api, /ws ──▶ api ──▶ redis ◀── ingestor ×2
```

Only Caddy is reachable from outside. Everything else talks over the compose
network. There is no database — see the header of `docker-compose.prod.yml` for
why, and `tests/unit/test_compose.py::TestPublicDemo` for the test that keeps it
true.

---

## 1. Create the instance

Console → **Compute → Instances → Create instance**.

| Field | Value |
|---|---|
| Image | Canonical Ubuntu 24.04 |
| Shape | **Ampere A1 Compute**, `VM.Standard.A1.Flex` |
| OCPUs / memory | 2 OCPU / 12 GB (half the free allowance — leaves room to rebuild) |
| Boot volume | 50 GB |
| SSH key | upload your public key |

Save the **public IPv4 address** it assigns.

> **"Out of host capacity."** This is the single most common wall, not a mistake
> on your part — A1 is oversubscribed in popular regions. Try each availability
> domain in turn, then retry over a few hours; capacity frees up constantly. Do
> not switch your home region to chase it, that is irreversible.
>
> If A1 stays unavailable, the Always Free `VM.Standard.E2.1.Micro` (x86, 1 GB
> RAM) will run this stack if you drop the ingestor to `replicas: 1`. It is
> tight but it works, and the demo looks identical.

---

## 2. Open the two firewalls

Oracle has **two**, and missing either produces the same symptom: the page
simply never loads, with no error to read.

**2a. The virtual one (always required).**

Networking → your VCN → the public subnet → its **Security List** → *Add Ingress
Rules*:

| Source CIDR | Protocol | Destination port |
|---|---|---|
| `0.0.0.0/0` | TCP | 80 |
| `0.0.0.0/0` | TCP | 443 |

**2b. The one on the instance (check before changing).**

Oracle's Ubuntu image ships an iptables ruleset ending in

```
REJECT all -- anywhere anywhere reject-with icmp-host-prohibited
```

Most guides tell you to punch through it immediately. **Test first**, because
Docker frequently makes it irrelevant: published ports are DNAT'd in
`nat/PREROUTING` and then traverse the `FORWARD` chain, not `INPUT`, so the
`INPUT` REJECT above never sees that traffic. Adding rules you do not need is
how people end up with a firewall they no longer understand.

After step 5, from your own machine:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://<PUBLIC_IP>/
```

`200` or `308` means you are done — skip the rest of this section. A hang or
`Connection refused` means `INPUT` is in the path. Then, and only then:

```bash
sudo nano /etc/iptables/rules.v4
```

Add these **above** the final `REJECT` line — order is the whole trick, a rule
below it is dead:

```
-A INPUT -p tcp -m state --state NEW -m tcp --dport 80 -j ACCEPT
-A INPUT -p tcp -m state --state NEW -m tcp --dport 443 -j ACCEPT
```

```bash
sudo iptables-restore < /etc/iptables/rules.v4
```

The file is read at boot, so this persists. Do not install `ufw` on top; two
firewall managers fighting over one ruleset is a bad afternoon.

---

## 3. A hostname

Caddy needs one to obtain a certificate — Let's Encrypt does not issue for bare
IP addresses.

- **Own a domain?** Add an `A` record pointing at the public IP.
- **Don't?** [DuckDNS](https://duckdns.org) is free: sign in, claim
  `yourname.duckdns.org`, set it to the IP.

Confirm it resolves before continuing, or Caddy will fail the ACME challenge and
back off:

```bash
dig +short yourname.duckdns.org
```

---

## 4. Docker

SSH in (`ssh ubuntu@<PUBLIC_IP>`), then:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
sudo systemctl enable --now docker
newgrp docker
```

`enable` is what brings the stack back after a reboot; combined with
`restart: unless-stopped` on every service, the box recovers from a kernel
patch unattended.

---

## 5. Bring it up

```bash
git clone https://github.com/VaibhavYDev/live-forex-volatility-tracker.git
cd live-forex-volatility-tracker
echo "FX_DOMAIN=yourname.duckdns.org" > .env
docker compose -f docker-compose.prod.yml up -d --build
```

First build takes ~5 minutes on 2 OCPUs — it compiles Python wheels for ARM and
runs a Vite production build.

---

## 6. Verify

```bash
docker compose -f docker-compose.prod.yml ps          # all Up, redis healthy
curl -s https://yourname.duckdns.org/readyz           # ready:true, feed_age_s < 5
```

`feed_age_s` is the number that matters. It is the ingestor heartbeat; anything
under 5 means the feed is live. If it climbs past 60 the dashboard will say
`Stale` — see `tests/unit/test_feed_heartbeat.py` for what that meant last time.

Then open `https://yourname.duckdns.org` and confirm the banner reads **Live**.

Certificate issuance takes 10–30 seconds on first request. If you get a browser
warning, wait and reload once before debugging: `docker compose -f
docker-compose.prod.yml logs caddy` states the ACME failure plainly.

---

## 7. Operating it

**Show off the leader election.** This is the demo's best trick, and it works on
the live URL:

```bash
docker compose -f docker-compose.prod.yml logs -f ingestor   # note the leader
docker compose -f docker-compose.prod.yml restart ingestor
```

The standby promotes within the lease TTL and the dashboard keeps streaming.

**Reset the chart.** Bars accumulate in Redis forever, so after months the
window fills with synthetic history and one enormous restart gap. Before
screenshotting or linking it anywhere:

```bash
docker compose -f docker-compose.prod.yml down -v
docker compose -f docker-compose.prod.yml up -d
```

**Update after a push:**

```bash
git pull && docker compose -f docker-compose.prod.yml up -d --build
```

Nothing is bind-mounted, so `--build` is not optional — `up -d` alone silently
restarts the old image.

---

## Costs

Nothing, as long as you stay inside Always Free: A1 capacity (4 OCPU / 24 GB
total), 200 GB of block volume, and 10 TB/month egress. This stack uses roughly
2 GB of RAM and a rounding error of bandwidth. Oracle will not bill you without
an explicit upgrade to a paid account.

Idle Always Free **x86** instances can be reclaimed after 7 days; A1 instances
are exempt, and this one is never idle anyway — the ingestor streams
continuously.
