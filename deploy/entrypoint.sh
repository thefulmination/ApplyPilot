#!/usr/bin/env bash
# Container entrypoint: bring up the in-container DeepSeek proxy, then run the worker.
set -uo pipefail

export PYTHONUTF8=1 PYTHONIOENCODING=utf-8

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
echo "[entrypoint] proxy healthy; starting worker (APPLYPILOT_WORKER_ID=${APPLYPILOT_WORKER_ID:-0})"

# exec so the worker is PID 1's child and receives SIGTERM directly on Railway scale-down.
exec python3 -m applypilot.apply.container_worker
