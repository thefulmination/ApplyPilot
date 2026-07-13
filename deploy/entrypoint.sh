#!/usr/bin/env bash
# Railway canonical v3 apply worker entrypoint.
set -euo pipefail

export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

require_env() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo "[entrypoint] required environment variable is not set: $name" >&2
    exit 64
  fi
}

require_file() {
  local path="$1"
  if [ ! -s "$path" ]; then
    echo "[entrypoint] required mounted asset is missing or empty: $path" >&2
    exit 66
  fi
}

require_env DATABASE_URL
require_env DEEPSEEK_API_KEY
require_env APPLYPILOT_WORKER_ID
require_env APPLYPILOT_RELEASE_VERSION
require_file "${APPLYPILOT_DIR:-/data/applypilot}/profile.json"
require_file "${APPLYPILOT_DIR:-/data/applypilot}/resume.pdf"

export FLEET_PG_DSN="$DATABASE_URL"
export FLEET_MACHINE_OWNER="${FLEET_MACHINE_OWNER:-railway}"
export APPLYPILOT_FLEET_LABEL="${APPLYPILOT_FLEET_LABEL:-$FLEET_MACHINE_OWNER}"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-litellm-local-proxy}"

if [ "$APPLYPILOT_FLEET_LABEL" != "$FLEET_MACHINE_OWNER" ]; then
  echo "[entrypoint] APPLYPILOT_FLEET_LABEL must match FLEET_MACHINE_OWNER" >&2
  exit 64
fi

echo "[entrypoint] starting LiteLLM proxy (DeepSeek -> Anthropic /v1/messages) on :4000..."
# Start the proxy with DATABASE_URL UNSET: otherwise litellm treats the worker's Postgres
# URL as its own Prisma state DB and crashes ("No module named 'prisma'"). The worker (exec'd
# below) keeps DATABASE_URL in its own env. The proxy needs no database.
env -u DATABASE_URL litellm --config /app/litellm_config.yaml --port 4000 --num_workers 1 > /tmp/litellm.log 2>&1 &
PROXY_PID=$!

ok=0
for _ in $(seq 1 40); do
  if curl -sf -o /dev/null http://127.0.0.1:4000/health/liveliness; then ok=1; break; fi
  if ! kill -0 "$PROXY_PID" 2>/dev/null; then
    echo "[entrypoint] proxy died on startup:"; tail -n 40 /tmp/litellm.log; exit 1
  fi
  sleep 2
done
if [ "$ok" != "1" ]; then
  echo "[entrypoint] proxy never became healthy:"; tail -n 40 /tmp/litellm.log; exit 1
fi
echo "[entrypoint] proxy healthy; starting canonical worker $APPLYPILOT_WORKER_ID"

# exec so the worker is PID 1's child and receives SIGTERM directly on Railway scale-down.
worker_args=(
  --dsn "$DATABASE_URL"
  --worker-id "$APPLYPILOT_WORKER_ID"
  --home-ip "${FLEET_HOME_IP:-railway}"
  --machine-owner "$FLEET_MACHINE_OWNER"
  --agent "${APPLYPILOT_APPLY_AGENT:-claude}"
  --model "${APPLYPILOT_APPLY_MODEL:-deepseek-chat}"
)

if [ -n "${APPLYPILOT_FALLBACK_AGENT:-}" ]; then
  worker_args+=(--fallback-agent "$APPLYPILOT_FALLBACK_AGENT")
fi
if [ -n "${APPLYPILOT_CHROME_SLOT:-}" ]; then
  worker_args+=(--chrome-slot "$APPLYPILOT_CHROME_SLOT")
fi

exec applypilot-fleet-apply "${worker_args[@]}"
