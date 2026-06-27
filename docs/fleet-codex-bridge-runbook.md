# Codex Fleet Bridge — runbook

Run on the home box (Postgres + Codex local). Codex CLI is the verified-reliable path;
Codex desktop additionally needs `cwd` + `enabled` (openai/codex #14449).

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.applypilot-fleet]
command = "C:\\Users\\JStal\\OneDrive\\Documents\\New project\\ApplyPilot\\.conda-env\\python.exe"
args = ["-m", "applypilot.fleet.codex_bridge"]
cwd = "C:\\Users\\JStal\\OneDrive\\Documents\\New project\\ApplyPilot"
enabled = true

[mcp_servers.applypilot-fleet.env]
FLEET_PG_DSN = "postgresql://…?connect_timeout=5"
```

- If every tool returns "FLEET_PG_DSN is not set", the env block did not inject — confirm it's a
  SUBTABLE (`[mcp_servers.applypilot-fleet.env]`), not an inline `env = {…}`.
- Use the ABSOLUTE `.conda-env\python.exe` — a GUI-launched Codex won't inherit the conda PATH and a
  bare `python` will ImportError on `applypilot`/`mcp`.
- Codex defaults: 10s startup, 60s per-tool. If the PG is remote, bump `tool_timeout_sec`.
- Leave the action tools (restart_worker / pause_scope / quarantine_job) on Codex's per-call approval.
- Tools: fleet_status, health_report, recent_results, challenges, caps (read); restart_worker,
  pause_scope, quarantine_job (action, audited via the bridge's logger).
