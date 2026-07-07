#!/usr/bin/env bash
# run-worker-mac.sh -- launchd-supervised offsite-ATS apply worker for macOS.
#
# launchd (com.applypilot.fleetworker, KeepAlive) runs THIS script whenever the Mac is
# on; the script supervises ONE applypilot-fleet-apply worker:
#   * on start and every UPDATE_CHECK_SECONDS: fetch the pinned branch via the read-only
#     deploy key; if origin advanced, SIGTERM the worker (it finishes the CURRENT job --
#     see install_stop_handler in apply_worker_main.py -- then exits), update, restart.
#     pip re-installs only when pyproject.toml changed (editable install).
#   * if the worker crashes, restart after RESTART_BACKOFF_SECONDS.
#   * on SIGTERM to the wrapper (launchctl unload / shutdown): drain the worker, exit 0.
# All state lives in the fleet Postgres; killing this Mac at any time is safe (leases
# expire and the watchdog reclaims). LinkedIn NEVER runs here (separate entrypoint,
# home box only). macOS ships bash 3.2 -- keep this file 3.2-compatible.
set -u

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$INSTALL_DIR/.applypilot/fleet-worker.env"

log() { printf '%s [wrapper] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
caffeinate_pid=0

load_env() {
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a
}

resolve_chrome_path() {
  # Newest Playwright chromium build (version sort: chromium-999 < chromium-1140);
  # exclude nested Helper bundles, whose binaries also live under Contents/MacOS.
  local d bin
  d=$(ls -d "$PLAYWRIGHT_BROWSERS_PATH"/chromium-* 2>/dev/null | sort -V | tail -1)
  [ -n "$d" ] || return 1
  bin=$(find "$d" -type f -path "*/Contents/MacOS/*" -not -path "*Helper*" 2>/dev/null | head -1)
  [ -n "$bin" ] || return 1
  printf '%s' "$bin"
}

detect_egress_ip() {
  # Residential egress IP = the per-IP rate-governor key (FLEET_HOME_IP).
  curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || printf '0.0.0.0'
}

start_caffeinate() {
  # Prevent idle sleep from taking an otherwise healthy Mac off Tailscale. This cannot
  # keep a powered-off or closed-lid laptop online, and it is intentionally opt-out for
  # machines where the owner wants normal sleep behavior.
  if [ "${APPLYPILOT_MAC_CAFFEINATE:-1}" != "0" ] && command -v caffeinate >/dev/null 2>&1; then
    caffeinate -dims -w $$ &
    caffeinate_pid=$!
    log "caffeinate active pid=$caffeinate_pid"
  fi
}

updates_available() {
  git -C "$INSTALL_DIR" fetch --quiet origin "$APPLYPILOT_BRANCH" || return 1
  [ "$(git -C "$INSTALL_DIR" rev-parse HEAD)" != "$(git -C "$INSTALL_DIR" rev-parse "origin/$APPLYPILOT_BRANCH")" ]
}

apply_update() {
  local old_sha
  old_sha=$(git -C "$INSTALL_DIR" rev-parse HEAD)
  git -C "$INSTALL_DIR" reset --hard "origin/$APPLYPILOT_BRANCH" || return 1
  if ! git -C "$INSTALL_DIR" diff --quiet "$old_sha" HEAD -- pyproject.toml; then
    log "pyproject.toml changed; re-installing package"
    "$INSTALL_DIR/.venv/bin/pip" install -q -e "$INSTALL_DIR" || return 1
  fi
  log "updated $old_sha -> $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
}

main() {
  load_env
  mkdir -p "$INSTALL_DIR/logs"
  start_caffeinate
  CHROME_PATH="$(resolve_chrome_path)" || { log "FATAL: no Playwright chromium under $PLAYWRIGHT_BROWSERS_PATH"; exit 1; }
  export CHROME_PATH
  FLEET_HOME_IP="$(detect_egress_ip)"
  export FLEET_HOME_IP
  log "egress=$FLEET_HOME_IP chrome=$CHROME_PATH branch=$APPLYPILOT_BRANCH"

  # Verification codes must go through the owner-side relay. Direct Gmail MCP
  # access from apply workers can trigger codes without creating otp_request rows.
  export APPLYPILOT_INBOX_AUTH=1
  export APPLYPILOT_INBOX_AUTH_MODE=relay
  export APPLYPILOT_ENABLE_GMAIL_MCP=0

  child=0
  trap 'log "SIGTERM: draining worker"; [ "$child" -gt 0 ] && kill -TERM "$child" 2>/dev/null; [ "$caffeinate_pid" -gt 0 ] && kill "$caffeinate_pid" 2>/dev/null; wait "$child" 2>/dev/null; exit 0' TERM INT

  while true; do
    if updates_available; then apply_update || log "WARN: update failed; running current code"; fi
    fallback_args=()
    if [ -n "${WORKER_FALLBACK_AGENT:-}" ]; then
      fallback_args=(--fallback-agent "${WORKER_FALLBACK_AGENT}")
    elif [ "${WORKER_AGENT:-claude}" = "claude" ]; then
      fallback_args=(--fallback-agent "codex")
    fi
    "$INSTALL_DIR/.venv/bin/applypilot-fleet-apply" \
      --worker-id "${WORKER_LABEL:-mac}-${WORKER_SLOT:-0}" \
      --agent "${WORKER_AGENT:-claude}" \
      --model "${WORKER_MODEL:-sonnet}" \
      --machine-owner "${FLEET_MACHINE_OWNER:-mac}" \
      "${fallback_args[@]}" &
    child=$!
    log "worker started pid=$child id=${WORKER_LABEL:-mac}-${WORKER_SLOT:-0}"
    waited=0
    while kill -0 "$child" 2>/dev/null; do
      sleep 60
      waited=$((waited + 60))
      if [ "$waited" -ge "${UPDATE_CHECK_SECONDS:-21600}" ]; then
        waited=0
        if updates_available; then
          log "update available: draining worker (finishes current job first)"
          kill -TERM "$child" 2>/dev/null
          wait "$child" 2>/dev/null
          break
        fi
      fi
    done
    wait "$child" 2>/dev/null
    child=0
    log "worker exited; restart in ${RESTART_BACKOFF_SECONDS:-30}s"
    sleep "${RESTART_BACKOFF_SECONDS:-30}"
  done
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then main "$@"; fi
