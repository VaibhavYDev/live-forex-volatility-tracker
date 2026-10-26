#!/usr/bin/env bash
# Wait for every container of the cloud stack to report healthy, then check the
# API from inside the network. Read-only. Exit 0 when healthy within the deadline.
#
#     scripts/cloud/verify.sh [env file, default .env.cloud] [seconds, default 180]
#
# FX_VERIFY_MIN_CONTAINERS: containers expected (default 8: caddy, web, api, worker,
# redis, timescale and two ingestors).
set -euo pipefail
cd "$(dirname "$0")/../.."

ENV_FILE=${1:-${FX_CLOUD_ENV:-.env.cloud}}
DEADLINE=$(( $(date +%s) + ${2:-180} ))
COMPOSE=(docker compose -f docker-compose.prod.yml --env-file "$ENV_FILE")

while :; do
  # -a: a container that exited or is restarting must fail the check, not vanish from it.
  ids=$("${COMPOSE[@]}" ps -a -q)
  total=$(echo "$ids" | grep -c . || true)
  # Containers without a healthcheck (caddy) count once they are running.
  healthy=$(echo "$ids" | xargs -r docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
    | grep -cE '^(healthy|running)$' || true)
  if [ "$total" -ge "${FX_VERIFY_MIN_CONTAINERS:-8}" ] && [ "$healthy" -eq "$total" ]; then
    break
  fi
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "not healthy: $healthy of $total containers" >&2
    "${COMPOSE[@]}" ps >&2
    exit 1
  fi
  sleep 5
done

# /api/health may legitimately report a problem (a closed market is fine, a dead
# feed is not), so print it rather than fail the deploy on it: the containers
# being healthy is the release check, the monitor is the operational one.
"${COMPOSE[@]}" exec -T api python -c "
import json, urllib.error, urllib.request
try:
    body = urllib.request.urlopen('http://localhost:8000/api/health', timeout=5).read()
except urllib.error.HTTPError as e:
    body = e.read()
print('api/health:', json.dumps(json.loads(body)))
"
echo "healthy: $healthy of $total containers"
