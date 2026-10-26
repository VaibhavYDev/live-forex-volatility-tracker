#!/usr/bin/env bash
# Return to the release deployed before the current one, using the images that are
# still on the server. Volumes are untouched.
#
#     scripts/cloud/rollback.sh
set -euo pipefail
cd "$(dirname "$0")/../.."

ENV_FILE=${FX_CLOUD_ENV:-.env.cloud}
COMPOSE=(docker compose -f docker-compose.prod.yml --env-file "$ENV_FILE")
STATE=.deploy

current=$(cat "$STATE/current" 2>/dev/null || true)
previous=$(awk '{print $2}' "$STATE/history" 2>/dev/null | grep -v "^${current:-x}$" | tail -1 || true)
[ -n "$previous" ] || { echo "no earlier release recorded in $STATE/history" >&2; exit 1; }

tag=${previous:0:12}
docker image inspect "fx-volatility-python:$tag" "fx-volatility-web:$tag" >/dev/null \
  || { echo "images for $tag are no longer on this server; deploy it instead" >&2; exit 1; }

echo "rolling back from ${current:0:12} to $tag"
git checkout --quiet --detach "$previous"
FX_IMAGE_TAG=$tag "${COMPOSE[@]}" up -d --remove-orphans
scripts/cloud/verify.sh "$ENV_FILE"
echo "$previous" > "$STATE/current"
echo "$(date -u +%FT%TZ) $previous rollback" >> "$STATE/history"
