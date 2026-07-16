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

forbidden_database_vars=(
  APPLYPILOT_FLEET_DSN
  APPLYPILOT_ADMIN_PG_DSN
  APPLYPILOT_CONTROLLER_PG_DSN
  APPLYPILOT_SUPER_DSN
  APPLYPILOT_BRAIN_ADMIN_DSN
  DATABASE_URL
  DATABASE_PUBLIC_URL
  DATABASE_PRIVATE_URL
  POSTGRES_URL
  POSTGRES_PUBLIC_URL
  POSTGRES_PRIVATE_URL
  PGAPPNAME
  PGCHANNELBINDING
  PGCLIENTENCODING
  PGCONNECT_TIMEOUT
  PGDATESTYLE
  PGGEQO
  PGGSSDELEGATION
  PGGSSENCMODE
  PGGSSLIB
  PGHOST
  PGHOSTADDR
  PGKRBSRVNAME
  PGLOADBALANCEHOSTS
  PGLOCALEDIR
  PGMAXPROTOCOLVERSION
  PGMINPROTOCOLVERSION
  PGOPTIONS
  PGPASSFILE
  PGPASSWORD
  PGPORT
  PGREQUIREAUTH
  PGREQUIREPEER
  PGREQUIRESSL
  PGSERVICE
  PGSERVICEFILE
  PGSYSCONFDIR
  PGSSLCERT
  PGSSLCERTMODE
  PGSSLCOMPRESSION
  PGSSLCRL
  PGSSLCRLDIR
  PGSSLKEY
  PGSSLMAXPROTOCOLVERSION
  PGSSLMINPROTOCOLVERSION
  PGSSLMODE
  PGSSLNEGOTIATION
  PGSSLROOTCERT
  PGSSLSNI
  PGTARGETSESSIONATTRS
  PGTZ
  PGUSER
  PGDATABASE
)
ambient_admin_vars=()
for name in "${forbidden_database_vars[@]}"; do
  if [[ -v "$name" ]]; then
    ambient_admin_vars+=("$name")
    unset "$name"
  fi
done
if [ "${#ambient_admin_vars[@]}" -ne 0 ]; then
  echo "[entrypoint] refusing ambient admin database variables: ${ambient_admin_vars[*]}" >&2
  exit 64
fi

require_env FLEET_PG_DSN
require_env DEEPSEEK_API_KEY
require_env APPLYPILOT_WORKER_ID
require_env APPLYPILOT_WORKER_CONTRACT
require_env APPLYPILOT_RELEASE_VERSION
require_file "${APPLYPILOT_DIR:-/data/applypilot}/profile.json"
require_file "${APPLYPILOT_DIR:-/data/applypilot}/resume.pdf"

export FLEET_MACHINE_OWNER="${FLEET_MACHINE_OWNER:-railway}"
export APPLYPILOT_FLEET_LABEL="${APPLYPILOT_FLEET_LABEL:-$FLEET_MACHINE_OWNER}"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-litellm-local-proxy}"

if [ "$APPLYPILOT_FLEET_LABEL" != "$FLEET_MACHINE_OWNER" ]; then
  echo "[entrypoint] APPLYPILOT_FLEET_LABEL must match FLEET_MACHINE_OWNER" >&2
  exit 64
fi
if [ "$APPLYPILOT_WORKER_CONTRACT" != "apply" ]; then
  echo "[entrypoint] this image requires APPLYPILOT_WORKER_CONTRACT=apply" >&2
  exit 64
fi

python - <<'PY'
import os

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from applypilot.fleet.pg_roles import validate_runtime_principal

dsn = os.environ["FLEET_PG_DSN"]
params = conninfo_to_dict(dsn)
user = params.get("user")
if not user or user in {"postgres", "fleet_worker"}:
    raise SystemExit(
        "[entrypoint] FLEET_PG_DSN must name a unique mapped per-node login role"
    )
if not params.get("password"):
    raise SystemExit("[entrypoint] FLEET_PG_DSN must contain an explicit password")
with psycopg.connect(dsn, row_factory=dict_row) as conn:
    identity = validate_runtime_principal(
        conn,
        worker_id=os.environ["APPLYPILOT_WORKER_ID"],
        contract=os.environ["APPLYPILOT_WORKER_CONTRACT"],
    )
print(
    "[entrypoint] database identity validated: "
    f"session_user={identity.session_user} current_user={identity.current_user}"
)
PY

echo "[entrypoint] starting LiteLLM proxy (DeepSeek -> Anthropic /v1/messages) on :4000..."
litellm --config /app/litellm_config.yaml --port 4000 --num_workers 1 > /tmp/litellm.log 2>&1 &
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
