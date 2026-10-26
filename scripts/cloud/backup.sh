#!/usr/bin/env bash
# Back up the cloud stack's database and Redis, verify the copy, and optionally send
# it off the server. Run daily by infra/systemd/fx-backup.timer.
#
#     scripts/cloud/backup.sh
#
# FX_BACKUP_DIR        where copies are kept (default /var/backups/fx-volatility)
# FX_BACKUP_KEEP_DAYS  local copies older than this are removed (default 30)
# FX_BACKUP_REMOTE     rclone destination for the off-server copy, e.g. "oci:fx-backups"
#                      (unset: local only). Remote copies are never deleted here.
set -euo pipefail
cd "$(dirname "$0")/../.."

ENV_FILE=${FX_CLOUD_ENV:-.env.cloud}
COMPOSE=(docker compose -f docker-compose.prod.yml --env-file "$ENV_FILE")
DIR=${FX_BACKUP_DIR:-/var/backups/fx-volatility}
KEEP=${FX_BACKUP_KEEP_DAYS:-30}
stamp=$(date -u +%Y%m%dT%H%M%SZ)
out="$DIR/$stamp"
mkdir -p "$out"
chmod 700 "$DIR" "$out"

# One consistent snapshot of the database, in pg_dump's custom format.
"${COMPOSE[@]}" exec -T timescale pg_dump -U fx -d fx -Fc > "$out/fx.dump"
# Redis over its replication protocol: a point-in-time RDB, without pausing writes.
"${COMPOSE[@]}" exec -T redis sh -c \
  'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning --rdb /tmp/backup.rdb >/dev/null && cat /tmp/backup.rdb && rm -f /tmp/backup.rdb' \
  > "$out/redis.rdb"

# A backup that cannot be read is not a backup: list the dump's contents and check
# the RDB's magic header before calling it done.
entries=$("${COMPOSE[@]}" exec -T timescale pg_restore --list < "$out/fx.dump" | grep -c 'TABLE DATA' || true)
[ "$entries" -gt 0 ] || { echo "backup $stamp: the database dump lists no table data" >&2; exit 1; }
[ "$(head -c 5 "$out/redis.rdb")" = "REDIS" ] || { echo "backup $stamp: the Redis copy is not an RDB file" >&2; exit 1; }
(cd "$out" && sha256sum fx.dump redis.rdb > SHA256SUMS)
echo "backup $stamp: $(du -sh "$out" | cut -f1), $entries tables with data"

if [ -n "${FX_BACKUP_REMOTE:-}" ]; then
  rclone copy "$out" "$FX_BACKUP_REMOTE/$stamp"
  echo "backup $stamp: copied to $FX_BACKUP_REMOTE/$stamp"
fi

find "$DIR" -mindepth 1 -maxdepth 1 -type d -name '20*Z' -mtime +"$KEEP" -exec rm -rf {} +
