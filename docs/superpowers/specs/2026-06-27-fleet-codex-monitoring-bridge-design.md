# Fleet Codex Monitoring Bridge — Design Spec

**Date:** 2026-06-27
**Status:** design, pending review (adversarially critiqued by a 4-lens panel — mcp-sdk / security / completeness / codex-integration — findings folded in)
**Repo:** `New project/ApplyPilot` (Python tool)
**Depends on:** the `dashboard_snapshot`/`build_health_report` primitives from `src/applypilot/fleet/heartbeat.py` + `src/applypilot/fleet/monitor.py`, the `MonitorActions` wrapper, and the v3 coordination Postgres. See [`2026-06-27-fleet-watchdog-monitoring-design.md`](2026-06-27-fleet-watchdog-monitoring-design.md).

## 1. Goal & success criteria

Let the owner watch — and minimally steer — the distributed fleet from **Codex** (CLI today; desktop once its MCP-discovery quirks are configured), by exposing the fleet's already-built telemetry and the bounded-safe monitor actions through a local **MCP server**. Codex launches the server as a stdio subprocess and calls its tools; the server **reuses** the `dashboard_snapshot`/`build_health_report` primitives (the bridge is their first runtime caller — the watchdog runtime does NOT invoke them) and runs a few read-only queries against the coordination Postgres, plus surfaces a small allowlist of conservative actions.

**Done when:** a FastMCP stdio server registers EXACTLY eight tools (five read, three action); each read tool returns the documented structure against seeded Postgres; each action tool produces the real DB effect by delegating to `monitor.MonitorActions`; **a registry test proves the live `@mcp.tool()` set is exactly those 8 names** (the load-bearing safety gate); importing the module with `FLEET_PG_DSN` unset does NOT raise and each tool then returns a structured DSN-missing error; an error path (bad arg / unreachable DB) returns a structured error rather than crashing; and the Codex config to launch it (absolute interpreter, `cwd`, `enabled`, env subtable) is documented and Windows-correct.

**Non-goals:** any apply authority; resolving parked challenges; changing cost caps; unpausing scopes; a web UI; multi-user/remote/authenticated transport (stdio, local, single-owner only); historical charting; per-tool rate limiting (noted as a residual-risk follow-up, §7).

## 2. Why this shape

- **MCP over stdio via the official Python `mcp` SDK (`mcp.server.fastmcp.FastMCP`, verified v1.28.1).** Codex launches MCP servers as local subprocesses speaking JSON-RPC over stdin/stdout (configured in `~/.codex/config.toml` under `[mcp_servers.<name>]`). stdio binds no IP/port and needs no auth — the minimal correct transport for a local, single-owner monitor. `mcp.run()` defaults to `transport="stdio"`. Tool functions may be plain **sync** functions returning `dict`/`str`/`list` (the SDK supports sync and async; sync is intentional here — see §3.3).
- **The safety boundary is the tool REGISTRY, not the import graph.** (Corrected from an earlier draft.) Opening a connection requires `applypilot.apply.pgqueue`, whose module also defines `set_paused` (which *unpauses*) and `set_spend_cap`; importing the whole module would bind those dangerous callables in the bridge's namespace. So the guarantee is **not** "nothing dangerous is importable." The guarantee is: **only eight functions are decorated `@mcp.tool()`, and a prompt-injected agent can invoke only registered tools over JSON-RPC.** The §5 registry test that asserts the live tool set is exactly the 8 names is therefore the *real* safety gate and is a hard merge gate. As defense-in-depth we also **minimize the namespace**: the bridge imports only the `connect` symbol (`from applypilot.apply.pgqueue import connect`), `monitor.MonitorActions`, and `heartbeat` (whose functions — beat/detect_stuck/dashboard_snapshot — are not dangerous), so `set_paused`/`set_spend_cap`/`resolve_challenge` are never bound in the module namespace.
- **Conservative-direction actions, with an acknowledged residual self-DoS risk (§7).** Every exposed action can only *slow* the fleet (restart a worker, pause a scope, quarantine a job); none can apply, unpause, resolve a parked challenge, or change a cap. A prompt-injected Codex agent's worst case through the registered tools is a needless restart/pause/quarantine — recoverable. This is NOT zero-risk: the actions are abusable for an availability attack on the owner's own pipeline (§7).

## 3. Architecture

A single module `src/applypilot/fleet/codex_bridge.py`:

- `mcp = FastMCP("applypilot-fleet")` at module scope; the eight tools registered with `@mcp.tool()`. In the official SDK `@mcp.tool()` returns the original function unchanged, so unit tests call the module-level functions directly; the registry test reaches them via the tool manager (§5).
- **No DB access at import or in `main()`** (hard requirement). `main()` does nothing but `mcp.run()`. All DB access happens inside a tool call, through `_with_conn`. This is what lets the server start cleanly so Codex can connect and surface a friendly DSN-missing message instead of a dead subprocess.
- **`_with_conn(fn)` helper** — the single DB entry point for every tool:
  1. `dsn = os.environ.get("FLEET_PG_DSN")`. If missing/empty → return `{"error": "FLEET_PG_DSN is not set; set it in the Codex MCP env block"}`. **Do NOT call `pgqueue.connect()` with no arg** — its `get_dsn()` falls back to `DATABASE_URL`/`APPLYPILOT_FLEET_DSN`, and `DATABASE_URL` is set on the home box (Railway), so a no-arg connect would silently hit the wrong database. The bridge reads `FLEET_PG_DSN` itself and passes it positionally.
  2. `conn = connect(dsn)` — **must** use `applypilot.apply.pgqueue.connect` (it sets `row_factory=dict_row`, which every read primitive relies on with `row["col"]` indexing; a bare `psycopg.connect` would break them). `pgqueue.connect(dsn, *, autocommit=False)` takes no timeout kwarg, so the bridge documents adding `connect_timeout=5` (libpq) to the DSN itself so a dead DB returns the structured error promptly instead of blocking the stdio loop.
  3. `return fn(conn)` inside `try`; on `OperationalError`/`RuntimeError`/`Exception` → return `{"error": str(e)}` (a normal, model-readable tool result).
  4. `finally`: `conn.rollback()` (best-effort) then `conn.close()`. The rollback enforces read-only discipline for the read tools (any accidental write is discarded) and is a harmless no-op after an action tool's own commit. `_with_conn` itself never commits — the action primitives (`issue_command`/`quarantine_job`/`pause_scope`) commit themselves with `commit=True`.
- **Entry point** `applypilot-fleet-codex-bridge = "applypilot.fleet.codex_bridge:main"` in `pyproject.toml`; also runnable as `python -m applypilot.fleet.codex_bridge`.
- **New dependency:** `mcp>=1.28,<2` in `pyproject.toml` dependencies (cap below a FastMCP-3.0 reorg). It pulls transitive deps (starlette, pydantic-settings, anyio, httpx-sse, rich) even for stdio-only use; `mcp` requires `python>=3.10` (project is `>=3.11` — OK) and `pydantic>=2.12` (project pins `pydantic>=2.13,<2.14` — 2.13 satisfies it, no conflict today; the narrow `<2.14` ceiling is the only future-resolver watch-point). **No new test dependency** — the registry test uses the *sync* tool-manager path (§5), so pytest-asyncio is not required.

### 3.1 Tool surface (exactly 8) — all params explicitly type-annotated

FastMCP infers each tool's JSON-Schema from the function's **type annotations**, so every parameter and return is annotated (untyped params degrade Codex's validation or fail registration). Read tools that return dicts/lists containing datetimes/Decimals are annotated permissively (`-> dict[str, Any]` / `-> list[dict[str, Any]]`) — the SDK serializes datetime/Decimal cleanly via `to_json(fallback=str)`; over-specific return types would validate-reject them.

**Read (telemetry) — no mutation:**
| Tool | Signature | Returns | Source |
|---|---|---|---|
| `fleet_status()` | `() -> dict[str, Any]` | machines, governor, queue_depth, captcha_backlog, quarantine, spend_today | `heartbeat.dashboard_snapshot(conn)` (rolls back internally) |
| `health_report()` | `() -> dict[str, Any]` | `{"report": <text incl. "NEEDS YOUR DECISION">}` | `monitor.build_health_report(snapshot, captcha_threshold=0.4, cost_cap_total=<cost_cap_daily_usd>)` — see note below |
| `recent_results(limit: int = 20)` | `(int) -> dict[str, Any]` | `{"results": [<normalized merged rows>]}` | `compute_queue` + `apply_queue`, see §3.1.1 |
| `challenges()` | `() -> dict[str, Any]` | `{"challenges": [<open rows>]}` | `auth_challenge` WHERE `resolved_at IS NULL`, columns id,url,worker_id,machine_owner,kind,route,raised_at |
| `caps()` | `() -> dict[str, Any]` | paused, cost_cap_daily_usd, cost_cap_total_usd, spend_today (24h), spend_total (all-time) | `fleet_config` + `SUM(llm_usage.cost_usd)` over 24h and all-time |

**Uniform return type:** every tool returns `dict[str, Any]` (text/list tools wrap under `report`/`results`/`challenges`), so the `{"error": …}` sentinel never collides with a narrow `-> str`/`-> list` annotation and datetime/Decimal serialize cleanly.

**`health_report` cap pairing (corrected):** `snapshot.spend_today` is a **24-hour** SUM, but `build_health_report`'s `cost_cap_total` kwarg flags spend ≥ 90% of the cap. Comparing 24h spend to a *lifetime* cap is apples-to-oranges, so the bridge passes `cost_cap_total=<fleet_config.cost_cap_daily_usd>` (the daily cap, matching the 24h window). The kwarg name is historical; the value passed is the daily cap.

**Action (bounded safe) — delegate to `MonitorActions`:**
| Tool | Signature | Effect | Delegation |
|---|---|---|---|
| `restart_worker(worker_id: str)` | `(str) -> dict[str, Any]` | enqueue a `restart` command | `MonitorActions(conn).restart_worker(worker_id)` → `{"action":"restart","worker_id":…,"command_id":…}` |
| `pause_scope(scope_key: str)` | `(str) -> dict[str, Any]` | set scope `breaker_state='paused'` | `MonitorActions(conn).pause_scope(scope_key)` → `{"action":"pause","scope_key":…}` |
| `quarantine_job(url: str, worker: str, reason: str)` | `(str,str,str) -> dict[str, Any]` | quarantine a job (manual one-shot) | `MonitorActions(conn).quarantine(url, worker=worker, reason=reason)` → `{"action":"quarantine","url":…,"newly_quarantined":<bool>}` |

**`quarantine` delegation detail (manual one-shot — v1 fix):** `MonitorActions.quarantine(self, url, *, worker, reason)` makes `worker`/`reason` **keyword-only** — the bridge MUST call `quarantine(url, worker=worker, reason=reason)` (positional would `TypeError`). Per §7, this lane adds a **manual one-shot** path so a deliberate quarantine is not a crash strike: `heartbeat.quarantine_job` gains a `manual: bool = False` kwarg; `manual=True` sets `poison_jobs.quarantined_at = now()` **immediately**, records the reason with a `manual:` prefix, and does **NOT** increment `crash_count` (so bridge/monitor quarantines never pollute real crash signal). `MonitorActions.quarantine` passes `manual=True`. The watchdog's *automatic* over-max quarantine keeps calling `heartbeat.quarantine_job` directly with the default (`manual=False`) crash-strike behavior, so the semantic split is clean: crash-driven quarantine accumulates `crash_count`; deliberate quarantine is a one-shot. A single bridge call therefore actually pulls the job; `newly_quarantined` is `True` on the pull and `False` only if it was already quarantined. (This touches the already-shipped `heartbeat.py` + `monitor.py` — a small, well-scoped change folded into this lane.)

`MonitorActions.report` is NOT exposed (it is a no-op echo for the in-process monitor; Codex narrates its own findings).

#### 3.1.1 `recent_results` — normalized merge of two non-union-compatible tables

`compute_queue` and `apply_queue` have different columns and different terminal-status enums, with no shared completion-timestamp column. So:
- **apply_queue:** terminal statuses `('applied','failed','blocked','crash_unconfirmed')`; `ORDER BY updated_at DESC` (NOT `applied_at` — it is NULL for failed/blocked rows). Select `url`/`status`/`updated_at` + the lane-detail columns `company`/`title`/`apply_error`.
- **compute_queue:** terminal statuses `('done','failed','quarantined')`; `ORDER BY updated_at DESC`. Select `url`/`status`/`updated_at` + the lane-detail columns `task`/`est_cost_usd`.
- Each table is queried separately with `LIMIT min(limit,100)`, projected into a **normalized row** with a **structured `detail` dict** (not a flattened string) so per-lane fidelity is preserved while the feed stays chronological:
  ```
  {"lane": "apply"|"compute", "url": …, "status": …, "finished_at": <updated_at iso>,
   "detail": {"company": …, "title": …, "apply_error": …}   # apply lane
           | {"task": …, "cost": <est_cost_usd>}}            # compute lane
  ```
  then merged and sorted by `finished_at` DESC in Python and sliced to `min(limit, 100)`. (The implementer confirms the exact `apply_queue`/`compute_queue` column names against the schema before SELECTing; a column absent on a table is simply omitted from that lane's `detail`.)

### 3.2 What is structurally absent (precise statement)

There is **no** generic `query`/`execute`/`sql` tool, and **no** tool that maps to apply/approve, `resolve_challenge`, `set_paused`/unpause, or `set_spend_cap`. The action tools touch only `MonitorActions`, whose surface is exactly `{restart_worker, quarantine, pause_scope, report}` with no `__getattr__` proxy. Defense-in-depth: the module imports only `connect` (not the whole `pgqueue` module), `MonitorActions`, and `heartbeat`, so the dangerous callables are not even bound in the namespace. The **enforced** guarantee, however, is the §5 registry test: the live `@mcp.tool()` set == the 8 names, full stop.

## 4. Error handling

- The official SDK already isolates a tool's uncaught exception into an `isError` result; it does **not** crash the stdio server. So `_with_conn`'s `try/except` exists to (a) return a clean, model-readable `{"error": …}` payload and (b) guarantee `rollback()`+`close()` in `finally` — not as crash prevention.
- `_with_conn` catches `RuntimeError` too (not only `OperationalError`): `pgqueue.get_dsn()` raises `RuntimeError` when no DSN resolves, and the bridge's own DSN-missing branch returns the structured error before any connect.
- Bad arguments are not SQL-layer errors: an unknown `worker_id` still enqueues a `remote_commands` row; a manual `quarantine` of an already-quarantined url returns `False`. The tool result reflects what actually happened (e.g. `newly_quarantined: false`), never a false success.

## 5. Testing (subagent-driven TDD, against the `fleet_db` disposable Postgres)

The `fleet_db` fixture already truncates/reseeds every table the bridge reads (`compute_queue`, `apply_queue`, `auth_challenge`, `llm_usage`, `fleet_config`, `worker_heartbeat`, `rate_governor`, `poison_jobs`, `remote_commands`) — no new fixture work.

- **Read tools:** seed PG, call each read tool's **module-level function** directly (the `@mcp.tool()` decorator returns it unchanged), assert structure/keys; `health_report()` surfaces a seeded anomaly; `recent_results` respects `min(limit,100)`, returns the normalized row with a structured per-lane `detail` dict, and merges both lanes ordered by `finished_at`; `challenges()` returns only `resolved_at IS NULL` rows; `caps()` returns both daily/total caps + 24h and all-time spend.
- **Action tools:** call each, assert the real DB effect (a `remote_commands` row; `breaker_state='paused'`; a `poison_jobs` strike), proving the `MonitorActions` delegation — including the kwarg-only `quarantine(url, worker=…, reason=…)` and that the result dict reports `newly_quarantined` honestly.
- **Registry / safety (hard gate, sync — no pytest-asyncio):** `names = {t.name for t in mcp._tool_manager.list_tools()}`; assert `names == {fleet_status, health_report, recent_results, challenges, caps, restart_worker, pause_scope, quarantine_job}`. A second assertion confirms no denied-op name is registered.
- **DSN discipline:** (a) importing the module with `FLEET_PG_DSN` unset does NOT raise (no DB at import/main); (b) a tool called with `FLEET_PG_DSN` unset returns `{"error": …}` and does NOT silently fall through to `DATABASE_URL`; (c) a tool called with an unreachable/closed DSN returns `{"error": …}`, not a raised exception.
- **MCP wiring smoke:** constructing `FastMCP` and listing tools via the tool manager succeeds (catches a decorator/registration regression) without a live client.

## 6. Owner-run (not code)

Run on the home box (Postgres + Codex both local). **Codex CLI is the verified-reliable path today**; Codex desktop additionally needs `cwd` + `enabled` (a known desktop MCP-discovery quirk — openai/codex #14449 — where a valid stdio server is not exposed without them). Use the **`.env` subtable** form (the official reference renders env as a subtable, not an inline table) and an **absolute interpreter path** (a GUI-launched Codex does not inherit the conda-activated PATH, so a bare `python` would `ModuleNotFoundError` on `applypilot`/`mcp`):

```toml
[mcp_servers.applypilot-fleet]
command = "C:\\Users\\JStal\\OneDrive\\Documents\\New project\\ApplyPilot\\.conda-env\\python.exe"
args = ["-m", "applypilot.fleet.codex_bridge"]
cwd = "C:\\Users\\JStal\\OneDrive\\Documents\\New project\\ApplyPilot"
enabled = true

[mcp_servers.applypilot-fleet.env]
FLEET_PG_DSN = "postgresql://…"
```

(Alternatively `command` = the installed console script `applypilot-fleet-codex-bridge.exe` in `.conda-env\Scripts`, which resolves the right interpreter without `-m`.) Notes to document: if the tools all return "FLEET_PG_DSN not set," the env block did not inject — confirm it is a subtable, not inline. Codex defaults: `startup_timeout_sec` 10s, `tool_timeout_sec` 60s — if the PG is remote, connect-per-call cold-start can approach these; recommend bumping `tool_timeout_sec` and keeping per-call connect fast (no retries inside a tool). Codex's tool-approval mode governs whether each call prompts; **leave the three action tools on prompt** for an extra human gate consistent with the "minimally steer" goal.

## 7. Residual risk (the three actions are abusable for self-DoS)

The actions are conservative in *direction* but are still an availability attack surface on the owner's own pipeline, which the design accepts and surfaces rather than hides:
- `quarantine_job(good_url, …)` pulls a job out of the pool. **The sharpest edge — that repeated calls accumulated `crash_count` and polluted real crash signal — is closed in v1** by the manual one-shot quarantine (§3.1): a bridge quarantine sets `quarantined_at` directly with a `manual:` reason and never touches `crash_count`. The residual is that one call still pulls one job; an injected agent could quarantine a good job — recoverable (the owner sees it in the audit log and can clear it), and no longer corrupts crash data.
- `pause_scope` can pause the most productive board, and there is **no unpause tool** — recovery requires owner CLI intervention.
- `restart_worker` can be spammed to thrash the fleet.

Mitigations: **(v1, in scope)** (a) the manual one-shot quarantine above, so deliberate quarantines never pollute `crash_count`; (b) every action tool logs (stdlib `logging`) the action, args, and result, giving the owner an audit trail of bridge-initiated changes. The practical front-line guard is leaving the action tools on Codex's per-call approval (§6) — a human approves each action. **(follow-ups, out of v1 scope, noted)** a per-tool rate cap (its value materializes mainly if the owner runs the action tools auto-approved, since approval-on already bounds burst blast radius); persisting the audit trail to a small table.

## 8. Decided questions

- Surface = read **+** the three bounded safe actions (owner decided). Denied ops stay off the registry. **Decided.**
- Framework/transport = official `mcp` SDK FastMCP over stdio; sync tools; connection-per-call. **Decided.**
- Safety guarantee = the 8-name registry test is the hard gate; namespace-minimization is defense-in-depth. **Decided (corrected).**
- Read tools enforce read-only via `_with_conn` rollback-on-finally; `dict_row` via `pgqueue.connect`. **Decided.**
- `recent_results` = a single merged chronological feed (newest-first across both lanes), with a **structured per-lane `detail` dict** (not a flattened string) to keep fidelity. **Decided.**
- `FLEET_PG_DSN` read directly (no `get_dsn` fallback). **Decided.**
- Registry test uses the sync `_tool_manager` path (no async test infra). **Decided.**
- Quarantine via the bridge is a **manual one-shot** that does not pollute `crash_count` (heartbeat `manual=` path); audit log + Codex per-call approval are the v1 guards. **Decided.**
- No web UI, no remote/authenticated transport, no per-tool rate limiting in v1 (deferred — relevant mainly under auto-approve). **Decided.**
