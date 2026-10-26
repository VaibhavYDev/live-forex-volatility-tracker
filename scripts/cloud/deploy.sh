#!/usr/bin/env bash
# Deploy a commit of this repository on the cloud server, and roll back to the
# previous release if it does not come up healthy.
#
#     scripts/cloud/deploy.sh [git ref, default origin/main]
#
# Images are tagged with the commit (FX_IMAGE_TAG), so the previous release's
# images stay on the server and scripts/cloud/rollback.sh is a restart, not a
# rebuild. Volumes are never touched: the database, Redis and certificates
# survive every deploy and rollback.
set -euo pipefail
cd "$(dirname "$0")/../.."

REF=${1:-origin/main}
ENV_FILE=${FX_CLOUD_ENV:-.env.cloud}
COMPOSE=(docker compose -f docker-compose.prod.yml --env-file "$ENV_FILE")
STATE=.deploy
mkdir -p "$STATE"

[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE (see docs/deploy-cloud.md)" >&2; exit 1; }
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "the checkout has local changes; commit or discard them first" >&2
  exit 1
fi

previous=$(cat "$STATE/current" 2>/dev/null || true)
git fetch --quiet origin
git checkout --quiet --detach "$REF"
commit=$(git rev-parse HEAD)
tag=${commit:0:12}
echo "deploying $tag (previous: ${previous:-none})"

FX_IMAGE_TAG=$tag "${COMPOSE[@]}" build
FX_IMAGE_TAG=$tag "${COMPOSE[@]}" up -d --remove-orphans

if scripts/cloud/verify.sh "$ENV_FILE"; then
  echo "$commit" > "$STATE/current"
  echo "$(date -u +%FT%TZ) $commit" >> "$STATE/history"
  echo "deployed $tag"
  exit 0
fi

echo "release $tag did not become healthy" >&2
if [ -n "$previous" ]; then
  echo "rolling back to ${previous:0:12}" >&2
  git checkout --quiet --detach "$previous"
  FX_IMAGE_TAG=${previous:0:12} "${COMPOSE[@]}" up -d --remove-orphans
  scripts/cloud/verify.sh "$ENV_FILE" || echo "the previous release is not healthy either" >&2
fi
exit 1
