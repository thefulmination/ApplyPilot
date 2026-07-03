"""hydrate-gmail.py [--force]

Write the Gmail MCP OAuth files (~/.gmail-mcp/gcp-oauth.keys.json + credentials.json)
from the fleet_assets blob store, so an apply worker box can complete ATS email-
verification without any manual credential copy. The @gongrzhe/server-gmail-autoauth-mcp
server reads those two files from ~/.gmail-mcp/ (a fixed, HOME-based path).

Idempotent: skips a file that already exists and is non-empty unless --force is given
(so a locally auto-refreshed token is never clobbered). Reads FLEET_PG_DSN from the env.
Exit 0 if both files are present afterwards, 1 otherwise (so callers can branch)."""
import os
import sys

from applypilot.apply import pgqueue

FILES = {
    "gmail_mcp_oauth_keys": "gcp-oauth.keys.json",
    "gmail_mcp_credentials": "credentials.json",
}


def main(argv):
    force = "--force" in argv
    dest_dir = os.path.join(os.path.expanduser("~"), ".gmail-mcp")
    os.makedirs(dest_dir, exist_ok=True)
    conn = pgqueue.connect()
    ok = True
    for asset, fname in FILES.items():
        path = os.path.join(dest_dir, fname)
        if os.path.exists(path) and os.path.getsize(path) > 0 and not force:
            print(f"  keep {fname} (already present)")
            continue
        data = pgqueue.get_asset(conn, asset)
        if not data:
            print(f"  MISSING asset {asset} in fleet_assets -- store it from home first", file=sys.stderr)
            ok = False
            continue
        with open(path, "wb") as fh:
            fh.write(data)
        print(f"  wrote {fname} ({len(data)} bytes)")
    both = all(os.path.exists(os.path.join(dest_dir, f)) and os.path.getsize(os.path.join(dest_dir, f)) > 0
               for f in FILES.values())
    print(f"gmail-mcp creds {'READY' if both else 'INCOMPLETE'} at {dest_dir}")
    return 0 if (ok and both) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
