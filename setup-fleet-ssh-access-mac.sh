#!/usr/bin/env bash
set -euo pipefail

KEY="${APPLYPILOT_FLEET_SSH_PUBLIC_KEY:-}"
AUTH_KEYS="$HOME/.ssh/authorized_keys"

if [[ -z "$KEY" ]]; then
  echo "Set APPLYPILOT_FLEET_SSH_PUBLIC_KEY to the public key printed by setup-fleet-ssh-access.ps1 -GenerateKey." >&2
  exit 2
fi

case "$KEY" in
  ssh-ed25519\ *|ssh-rsa\ *|ecdsa-sha2-nistp*\ *) ;;
  *)
    echo "Public key must start with ssh-ed25519, ssh-rsa, or ecdsa-sha2-nistp*." >&2
    exit 2
    ;;
esac

mkdir -p "$HOME/.ssh"
touch "$AUTH_KEYS"
chmod 700 "$HOME/.ssh"
chmod 600 "$AUTH_KEYS"

if ! grep -qxF "$KEY" "$AUTH_KEYS"; then
  printf '%s\n' "$KEY" >> "$AUTH_KEYS"
  echo "Added codex-fleet-access key to $AUTH_KEYS"
else
  echo "codex-fleet-access key already present in $AUTH_KEYS"
fi

if command -v systemsetup >/dev/null 2>&1; then
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    systemsetup -setremotelogin on >/dev/null
  else
    sudo systemsetup -setremotelogin on >/dev/null
  fi
fi

echo "Remote Login is enabled. Test from home with: ssh palomaperez@palomas-macbook-air hostname"
