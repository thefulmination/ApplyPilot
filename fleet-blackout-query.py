"""fleet-blackout-query.py <machine_label> [role]

PowerShell-friendly guard for launchers. Prints:
  OK|label|role|||
  BLOCKED|label|role|policy|expires_at|reason

Any configuration or database error is reported as BLOCKED so the fleet agent
and direct launchers fail closed.
"""
from __future__ import annotations

import os
import re
import sys

from psycopg import pq
from psycopg.conninfo import conninfo_to_dict


# Endpoint, database/role, credentials, TLS/auth, and routing define authority.
# Client tuning such as timeouts, application names, and keepalives is excluded.
_AUTHORITY_IDENTITY_FIELDS = frozenset(
    {
        "service",
        "servicefile",
        "host",
        "hostaddr",
        "port",
        "dbname",
        "user",
        "password",
        "passfile",
        "options",
        "channel_binding",
        "sslmode",
        "sslnegotiation",
        "sslcompression",
        "sslcert",
        "sslkey",
        "sslcertmode",
        "sslpassword",
        "sslrootcert",
        "sslcrl",
        "sslcrldir",
        "sslsni",
        "requirepeer",
        "require_auth",
        "min_protocol_version",
        "max_protocol_version",
        "ssl_min_protocol_version",
        "ssl_max_protocol_version",
        "gssencmode",
        "krbsrvname",
        "gsslib",
        "gssdelegation",
        "replication",
        "target_session_attrs",
        "load_balance_hosts",
        "scram_client_key",
        "scram_server_key",
        "oauth_issuer",
        "oauth_client_id",
        "oauth_client_secret",
        "oauth_scope",
    }
)
_SENSITIVE_AUTHORITY_FIELDS = frozenset(
    {
        "password",
        "sslpassword",
        "passfile",
        "sslkey",
        "oauth_client_secret",
        "scram_client_key",
        "scram_server_key",
    }
)
_SENSITIVE_KEY_PATTERN = "|".join(
    re.escape(key) for key in sorted(_SENSITIVE_AUTHORITY_FIELDS, key=len, reverse=True)
)


def _normalized_dsn(dsn: str) -> dict[str, str]:
    defaults = {
        option.keyword.decode(): option.val.decode()
        for option in pq.Conninfo.get_defaults()
        if option.val is not None
    }
    normalized = dict(defaults)
    normalized.update(conninfo_to_dict(dsn))
    for key in ("host", "hostaddr"):
        if normalized.get(key) == "":
            normalized.pop(key)
    if normalized.get("port") == "":
        if "port" in defaults:
            normalized["port"] = defaults["port"]
        else:
            normalized.pop("port")
    if not normalized.get("dbname") and normalized.get("user"):
        normalized["dbname"] = normalized["user"]
    normalized.setdefault("sslcertmode", "allow")
    return {key: value for key, value in normalized.items() if key in _AUTHORITY_IDENTITY_FIELDS}


def _safe_field(value: object) -> str:
    return " ".join(str(value).replace("|", "/").split())[:300]


def _sanitized_error(exc: Exception, *dsns: str) -> str:
    message = str(exc)
    for dsn in dsns:
        try:
            parsed = _normalized_dsn(dsn)
        except Exception:
            parsed = {}
        for key in _SENSITIVE_AUTHORITY_FIELDS:
            secret = parsed.get(key)
            if secret:
                message = message.replace(secret, "***")
    message = re.sub(
        r"(?i)\b(postgres(?:ql)?://)(?:[^@\s]+@)?([^\s]+)",
        r"\1***@\2",
        message,
    )
    message = re.sub(
        rf"(?i)\b({_SENSITIVE_KEY_PATTERN})\s*=\s*(?:'[^']*'|\"[^\"]*\"|\S+)",
        lambda match: f"{match.group(1)}=***",
        message,
    )
    return _safe_field(message)

label = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("APPLYPILOT_FLEET_LABEL", "home")
role = sys.argv[2] if len(sys.argv) > 2 else "fleet"

try:
    from applypilot.apply import pgqueue
    from applypilot.fleet import machine_blackout

    fleet_dsn = (os.environ.get("FLEET_PG_DSN") or "").strip()
    applypilot_fleet_dsn = (os.environ.get("APPLYPILOT_FLEET_DSN") or "").strip()
    if (
        fleet_dsn
        and applypilot_fleet_dsn
        and _normalized_dsn(fleet_dsn) != _normalized_dsn(applypilot_fleet_dsn)
    ):
        raise RuntimeError("Inconsistent fleet Postgres DSN references")
    dsn = fleet_dsn or applypilot_fleet_dsn
    if not dsn:
        raise RuntimeError("No fleet Postgres DSN: set FLEET_PG_DSN or APPLYPILOT_FLEET_DSN")
    conn = pgqueue.connect(dsn)
    conn.read_only = True
    print(machine_blackout.status_line(conn, label, role=role))
except Exception as exc:
    diagnostic = _sanitized_error(
        exc,
        os.environ.get("FLEET_PG_DSN", ""),
        os.environ.get("APPLYPILOT_FLEET_DSN", ""),
    )
    safe_label = _safe_field(label).lower()
    safe_role = _safe_field(role).lower()
    print(f"fleet-blackout-query: {type(exc).__name__}: {diagnostic}", file=sys.stderr)
    print(f"BLOCKED|{safe_label}|{safe_role}|blackout-query-error||{diagnostic}")
