# Web browser automation options for minimizing ApplyPilot cost per verified job application

Consolidated from 31 validated research records. Values explicitly marked uncertain are omitted from the detailed comparison and listed separately for each option.

## Table of contents

1. [Chrome DevTools MCP and CLI](#chrome-devtools-mcp-and-cli) - Category: agent_control
2. [Playwright CLI and Playwright skills](#playwright-cli-and-playwright-skills) - Category: agent_control
3. [Playwright MCP over local Chrome CDP](#playwright-mcp-over-local-chrome-cdp) - Category: agent_control
4. [Existing-browser extension and content-script executor](#existing-browser-extension-and-content-script-executor) - Category: authenticated_browser
5. [Camoufox](#camoufox) - Category: compatibility_browser
6. [nodriver](#nodriver) - Category: compatibility_browser
7. [Patchright](#patchright) - Category: compatibility_browser
8. [ATS-specific deterministic Playwright adapters](#ats-specific-deterministic-playwright-adapters) - Category: deterministic
9. [Browser-assisted HTTP handoff with Playwright APIRequestContext](#browser-assisted-http-handoff-with-playwright-apirequestcontext) - Category: deterministic
10. [Direct ATS HTTP or supported API submission](#direct-ats-http-or-supported-api-submission) - Category: deterministic
11. [Puppeteer](#puppeteer) - Category: driver
12. [Raw Chrome DevTools Protocol](#raw-chrome-devtools-protocol) - Category: driver
13. [Selenium WebDriver](#selenium-webdriver) - Category: driver
14. [WebDriver BiDi and WebdriverIO](#webdriver-bidi-and-webdriverio) - Category: driver
15. [Apify browser automation](#apify-browser-automation) - Category: hosted_browser
16. [Bright Data Browser API (formerly Scraping Browser)](#bright-data-browser-api-formerly-scraping-browser) - Category: hosted_browser
17. [Browserbase](#browserbase) - Category: hosted_browser
18. [Browserless](#browserless) - Category: hosted_browser
19. [Cloudflare Browser Run (formerly Browser Rendering)](#cloudflare-browser-run-formerly-browser-rendering) - Category: hosted_browser
20. [Steel](#steel) - Category: hosted_browser
21. [AgentQL semantic locator layer](#agentql-semantic-locator-layer) - Category: model_assisted
22. [Browser Use](#browser-use) - Category: model_assisted
23. [Skyvern](#skyvern) - Category: model_assisted
24. [Stagehand](#stagehand) - Category: model_assisted
25. [Crawlee AdaptivePlaywrightCrawler](#crawlee-adaptiveplaywrightcrawler) - Category: preflight
26. [Amazon Nova Act](#amazon-nova-act) - Category: premium_agent
27. [BrowserGym and AgentLab evaluation harness](#browsergym-and-agentlab-evaluation-harness) - Category: verification
28. [Independent deterministic submission verifier](#independent-deterministic-submission-verifier) - Category: verification
29. [Magnitude](#magnitude) - Category: vision_agent
30. [Microsoft Fara and local small computer-use models](#microsoft-fara-and-local-small-computer-use-models) - Category: vision_agent
31. [WebMCP](#webmcp) - Category: watchlist

## Detailed results

### Chrome DevTools MCP and CLI

Source record: `Chrome_DevTools_MCP_and_CLI.json`

#### Identity

- **Name:** Chrome DevTools MCP and CLI
- **Category:** agent_control
- **Official Sources:**

  > - https://github.com/ChromeDevTools/chrome-devtools-mcp<br>- https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/cli.md<br>- https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/tool-reference.md<br>- https://developer.chrome.com/blog/chrome-devtools-mcp<br>- https://developer.chrome.com/blog/remote-debugging-port<br>- https://www.npmjs.com/package/chrome-devtools-mcp
- **Maintenance Status:**

  > Active. The official repository was pushed on 2026-07-09, release chrome-devtools-mcp-v1.5.0 was published on 2026-07-03, and npm reported version 1.5.0 on 2026-07-09. The project has had material 2025-2026 work including the standalone CLI, network body export, slim mode, concurrent page routing, extension tools, and Chrome 144 auto-connect support.
- **License:**

  > Apache-2.0. This is permissive and compatible with use from ApplyPilot's AGPL-3.0 codebase, subject to preserving the Apache license and notices and respecting its patent terms. Calling the Node package as an MCP server or CLI does not require relicensing ApplyPilot.

#### Cost Economics

- **Pricing Model:**

  > The open-source MCP server and CLI have no license, subscription, browser-hour, or per-call charge. Costs are the external agent/model, the local Chrome and Node processes, and any separately selected remote browser or proxy. npm update checks and Google usage statistics are not billable.
- **Billing Granularity:**

  > There is no Chrome DevTools MCP billing unit, idle fee, cold-start fee, egress fee, or included-usage quota. External model billing follows the selected agent, and any hosted CDP endpoint follows that provider. Local idle cost continues while the persistent CLI daemon and Chrome process remain alive.

#### Reliability

- **Failure Modes:**

  > Relevant failures are Node or MCP startup errors, Chrome launch failure, CDP connection loss, profile lock or wrong-profile attachment, stale snapshot UIDs, dynamic conditional fields, file-path restrictions, navigation timeouts, model misuse of powerful evaluate/network tools, submit followed by process loss, and confirmation responses that do not expose an application identifier. Manual remote-debugging ports also expose full browser control to local processes if not isolated.
- **Crash Recovery:**

  > The CLI reuses a background daemon and browser across commands, and the MCP profile persists across runs unless isolated. Debug logs, snapshots, screenshots, trace files, network request/response bodies, and experimental screencasts can support diagnosis. There is no workflow checkpoint/resume or action replay layer, so ApplyPilot must persist a pre-submit checkpoint and quarantine any crash after an application-touching or submit action.
- **Exactly Once Submit Safety:**

  > No job-level idempotency or duplicate-submit protection is built in. ApplyPilot must retain its lease and dedup ledger, record a pre-submit fingerprint, expose final submit through a dedicated guarded operation, monitor the submit network request, and quarantine an ambiguous post-submit result rather than retrying.
- **Verification Evidence Tier:**

  > Strong candidate for high-grade evidence. get_network_request can return or save request and response bodies, list_network_requests can preserve requests across three navigations, evaluate_script can inspect a success state, and snapshot/screenshot/screencast tools provide secondary evidence. It reaches the highest tier only when ApplyPilot parses a durable ATS/application ID or explicit successful response; DOM confirmation is lower tier and a screenshot alone remains weak evidence.

#### Applypilot Fit

- **Python Integration:**

  > No native Python SDK is required or supplied. ApplyPilot can invoke chrome-devtools with --output-format=json through a subprocess, run the npm package as an MCP server, or use a Python MCP client. The CLI's persistent daemon is attractive for low-overhead probes, but its interface is explicitly experimental and some MCP tools such as fill_form, wait_for, and extension tools are excluded from CLI generation.
- **Playwright Cdp Reuse:**

  > Good CDP reuse. The server can attach by --browser-url, --ws-endpoint with optional headers, or Chrome 144+ --auto-connect, and can use a supplied user-data directory. Chrome requires a non-default user-data directory for a manually opened remote-debugging port, so ApplyPilot's dedicated seeded profiles fit better than the user's normal profile. Do not let Playwright and this server concurrently drive the same tab without explicit ownership.
- **Observability Joinability:**

  > High if wrapped correctly. CLI JSON output, debug log files, network request IDs and bodies, console messages, snapshots, screenshots, performance traces, and screencasts can all be keyed by ApplyPilot attempt_id and result_event_id. The wrapper should write artifact hashes and paths into apply_result_events rather than relying on an agent transcript alone.

#### Authentication

- **Persistent Profiles:**

  > The default dedicated Chrome profile persists across runs and is shared by instances; --user-data-dir selects another profile and --isolated creates a temporary profile that is deleted on close. Separate per-worker or per-account directories and an exclusive lock are required. Chrome channel and version changes should be canaried before reusing long-lived profiles.
- **Existing Session Attach:**

  > Supported through Chrome 144+ permissioned --auto-connect, --browser-url, or --ws-endpoint. Auto-connect can see all windows in the chosen profile and asks the user to allow the connection. Manual CDP attachment should bind to loopback, use a dedicated non-default profile, and never expose the port to the network.
- **Login And Otp:**

  > Good for owner takeover because the server can attach to a manually authenticated browser and preserve cookies. It does not provide email polling, OTP extraction, TOTP generation, or a human approval queue. ApplyPilot should keep login and OTP in the owner/home recovery lane and pass control back only after a verified authenticated state.
- **Extension Support:**

  > MCP 1.5.0 exposes install, list, reload, trigger, and uninstall extension tools when the extension category is enabled. Those tools are not available in the generated CLI. For ApplyPilot, use only a reviewed unpacked extension with narrow host permissions and disable extension-management tools during normal application attempts.
- **Session Data Boundary:**

  > Browser content is exposed to the connected MCP client, including authenticated page data. Browser/profile data and saved artifacts remain local unless the client/model uploads them. Google usage statistics are enabled by default but can be disabled, performance traces may send URLs to CrUX unless disabled, and sensitive network headers are only redacted when --redact-network-headers is enabled. ApplyPilot should opt out of telemetry, disable CrUX, redact headers, and keep artifacts in its protected evidence store.

#### Quality And Safety

- **Required Field Completeness:**

  > fill_form can batch known snapshot elements, but it does not prove every required or conditionally revealed field is complete. Add a deterministic pre-submit validator that enumerates visible required controls, checks browser validity, re-snapshots after conditional answers, rejects unmapped labels, and records the final answer plan.
- **Answer Provenance:**

  > No provenance policy is built in. The MCP client can type any model-generated value. ApplyPilot must restrict values to profile, resume, approved-answer, and explicit job evidence; pass answer IDs and source hashes to the fill plan; and prohibit free-form fabrication of dates, metrics, employers, work authorization, or demographic answers.
- **Unsupported Question Fallback:**

  > No native escalation workflow exists. A wrapper should classify unknown, sensitive, legal, demographic, compensation, and free-text questions before any fill, then return a durable needs_review result to the owner lane. The agent must not guess simply because click, fill, and evaluate tools are available.
- **Prompt Injection Resistance:**

  > The server exposes powerful browser, JavaScript, network, file-upload, extension, and optional third-party tools, but it does not provide an ATS domain allowlist or job-application prompt-injection policy. Restrict destinations outside the MCP layer, disable unused tool categories, restrict negotiated roots, keep unrestricted paths off, redact secrets, and test hostile page text that asks the model to reveal data or submit prematurely.
- **Irreversible Action Guard:**

  > No built-in confirmation boundary distinguishes a normal click from final submit. Remove generic submit authority from the exploration agent; require a complete signed plan, current lease, dedup check, allowed host, no active challenge, and an explicit submit token consumed once by a narrow submit function. Start with discovery and verifier-only canaries.

#### Operations

- **Warm Session Reuse:**

  > Strong. The CLI reuses one background daemon and browser, preserving pages and cookies, and MCP profiles persist unless isolated. Explicit start, status, and stop commands support lifecycle control. ApplyPilot still needs idle TTLs, profile locks, health checks, and a clean-tab reset between jobs.
- **Tracing And Replay:**

  > Provides debug logs, console data, accessibility snapshots, screenshots, network request/response body files, performance traces, and experimental screencast video. CLI can emit raw JSON. It does not provide deterministic action replay or workflow resume, so replay must be implemented as saved ApplyPilot commands/scripts and must never repeat a final submit automatically.
- **Deployment Modes:**

  > Local npm package as MCP stdio server, local standalone CLI and daemon, plugin integrations for several agent clients, attachment to an external or sandbox-host Chrome over loopback/forwarded CDP, and attachment to a remote WebSocket endpoint with headers. There is no first-party managed browser service bundled with the project.
- **Browser Version Matrix:**

  > Official support is Google Chrome and Chrome for Testing only, with fixes targeted at the latest Extended Stable Chrome. Chrome stable, beta, dev, and canary channels are selectable. Other Chromium browsers are explicitly not guaranteed; Firefox, WebKit, and WebDriver BiDi are not supported by this package. Auto-connect requires Chrome 144+.
- **Canary Stop Loss:**

  > Wrap the server with per-attempt limits: allowed hosts, one owned tab, at most 12 browser-changing calls, two minutes before submit review, no extension/third-party tools, no unrestricted paths, and immediate quarantine after a submit-touching crash. Stop a route when positive confirmation falls, duplicate/ambiguous outcomes occur, or p95 cost exceeds the control; the MCP server itself has no dollar breaker.

#### Benchmark

- **Benchmark Design:**

  > Use the current Playwright MCP route as the control. Run synthetic ATS mirrors repeatedly for form variants and hostile-page tests, then randomize unique live jobs within Ashby, Greenhouse, Lever, Workable, Workday, and long-tail strata so no job is submitted twice. Capture tool calls, serialized bytes, model tokens, network evidence, human minutes, positive confirmation, and ambiguous outcomes.
- **Route Funnel:**

  > Record eligible, policy-allowed, Chrome attached, initial snapshot obtained, network capture active, required fields mapped, plan approved, submit token issued, submit request observed, positive confirmation parsed, delayed email reconciled, fallback selected before submit, human recovery, ambiguous quarantine, and final disposition.
- **Cache Replay Yield:**

  > The product caches browser state through its persistent daemon/profile but does not provide Stagehand-style action caching or a validated no-model replay engine. Count only externally generated deterministic scripts as replay hits, version them by ATS/form signature, and invalidate on DOM or endpoint change. Session reuse alone is not a replay success.
- **Recommendation:**

  > CANARY. Use Chrome DevTools MCP/CLI first for network-aware adapter discovery, deterministic preflight probes, and independent confirmation evidence. Do not replace the current submit path or grant autonomous final-submit authority until a matched canary proves lower all-in cost and equal or better positive-confirmation quality. Pin the package version and prefer the MCP fill_form/network tools over the experimental CLI where those capabilities are required.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `concurrency`
- `confidence_intervals`
- `control_plane_overhead`
- `deterministic_coverage`
- `escape_rate`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `model_cost`
- `projected_cost_per_verified_apply`
- `site_permission_and_terms`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`

### Playwright CLI and Playwright skills

Source record: `Playwright_CLI_and_Playwright_skills.json`

#### Identity

- **Name:** Playwright CLI and Playwright skills
- **Category:** agent_control
- **Official Sources:**

  > - https://github.com/microsoft/playwright-cli<br>- https://github.com/microsoft/playwright-cli/blob/main/skills/playwright-cli/SKILL.md<br>- https://www.npmjs.com/package/@playwright/cli<br>- https://playwright.dev/agent-cli/introduction<br>- https://playwright.dev/agent-cli/sessions<br>- https://playwright.dev/agent-cli/commands/attach<br>- https://playwright.dev/agent-cli/commands/tracing
- **Maintenance Status:**

  > Actively maintained and early-stage. The npm registry reported @playwright/cli 0.1.17 published on 2026-07-09, and upstream added attach, session, tracing, dashboard, and skill features during 2026. Its young 0.x interface should be pinned and canaried for breaking changes.
- **License:**

  > Apache-2.0. It is a permissive dependency and can be used from ApplyPilot's AGPL-3.0 runtime; preserve upstream license and notice material when redistributing or modifying it.

#### Cost Economics

- **Pricing Model:**

  > Open-source, self-hosted CLI and skill with no per-command vendor fee. Costs are the controlling model or agent subscription, local browser and Node compute, storage for snapshots/traces, and operator recovery.
- **Billing Granularity:**

  > No CLI billing unit, concurrency charge, cold-start fee, or included-usage tier. Each shell command has process/IPC overhead, while the named browser session stays warm. Model billing remains token/turn/subscription based; local compute is consumed while browser sessions and the dashboard run.

#### Reliability

- **Failure Modes:**

  > Stale refs after navigation, wrong named session, browser daemon or command process loss, shell quoting/encoding errors, snapshot file missing or unread, CDP/extension attach failure, profile lock, agent failing to inspect a scoped snapshot, command timeout, validation loops, broad eval/run-code misuse, unsupported custom widgets, login/OTP walls, and ambiguous submit confirmation.
- **Crash Recovery:**

  > Named sessions can keep cookies, tabs, and browser state across separate CLI invocations; persistent profiles survive browser restart. The dashboard can expose stuck sessions, and traces preserve steps. ApplyPilot must map each command/session/artifact to an attempt ID and quarantine any crash after the submit checkpoint because CLI session reuse is not transactional replay.
- **Exactly Once Submit Safety:**

  > CLI commands have no idempotency key or submit transaction. Preserve ApplyPilot's lease, durable pre-submit checkpoint, required-field/provenance check, one submit action, positive DOM/network confirmation, and crash_unconfirmed quarantine. Do not let a restarted skill replay an old click command after an ambiguous outcome.
- **Verification Evidence Tier:**

  > Can capture Tier 1 network response/reference data through network and trace commands, Tier 2 success DOM/URL/snapshot and screenshot evidence, and later Tier 3 email evidence through ApplyPilot reconciliation. The default command output links to snapshot files, so final evidence must be copied into durable attempt-scoped storage before the session is deleted.

#### Applypilot Fit

- **Python Integration:**

  > Moderate. ApplyPilot can supervise playwright-cli as a subprocess and parse line/file outputs, but the control path is shell/Node rather than native Python. A narrow Python command broker is preferable to granting the page-influenced model unrestricted shell access.
- **Playwright Cdp Reuse:**

  > Excellent. CLI supports attach --cdp=<url>, channel discovery, Playwright server endpoints, extension attach, named sessions, --persistent, and --profile. ApplyPilot's existing Chrome profiles and CDP ports can be reused, subject to one process per profile and Chromium-only CDP attachment.
- **Ats Fit:**

  > Functionally similar to MCP because both use Playwright accessibility snapshots and actions. It should fit Ashby, Greenhouse, Lever, Workable, Workday, and long-tail pages to the same degree only if the agent sees equivalent scoped state. It does not by itself solve low-yield Workday tenants, auth gates, CAPTCHAs, or missing answer provenance.
- **Observability Joinability:**

  > Potentially strong if every invocation includes an attempt-derived session name and output directory. Command stdout, snapshot paths, console/network logs, trace.zip, video, current URL, and session metadata can join to apply_queue and apply_result_events. The integration must persist these references before cleanup and redact secrets.

#### Authentication

- **Persistent Profiles:**

  > Supported with --persistent or --profile, while default sessions retain storage only in memory until close. Each named session has isolated cookies and history. A disk profile cannot be used by multiple browser processes simultaneously, and Chrome 136 requires automation profiles separate from the default user data directory.
- **Existing Session Attach:**

  > Strong. attach supports a CDP URL, discovered Chrome/Edge channel, Playwright server endpoint, or browser extension. Extension mode reuses logged-in tabs, cookies, and installed extensions; CDP remains Chromium-family only and should bind to loopback.
- **Login And Otp:**

  > Persistent or extension-attached sessions reuse existing authentication. The dashboard allows owner takeover for SSO, OTP, or explicit decisions. The automated agent should stop rather than enter unknown credentials or bypass access controls, then resume the same named session only after a durable human handoff result.
- **Extension Support:**

  > Extension attach is a first-class CLI mode for existing Chrome/Edge tabs and installed extensions. For custom automation extensions, use a separate persistent Chromium profile and narrowly scoped permissions; native messaging remains a separately audited OS capability.
- **Session Data Boundary:**

  > Local CLI/CDP keeps cookies in the selected profile, but snapshots, storage-state files, console/network logs, videos, traces, resumes, and command history are written locally and model-visible when read. Extension mode exposes existing logged-in tabs. Restrict output ACLs and retention, isolate each worker, redact PII/secrets, and never expose CDP remotely without authentication.

#### Quality And Safety

- **Required Field Completeness:**

  > Snapshot refs and Playwright locators can enumerate visible required controls, but CLI does not add a semantic completeness guarantee over MCP. Require a deterministic DOM validator for required and conditional fields and refuse submit when any required field or upload is unresolved.
- **Answer Provenance:**

  > The skill provides browser commands, not evidence-constrained answers. Keep ApplyPilot's profile/resume/approved-answer sources outside page control and require source IDs for every generated answer. Disable arbitrary file discovery so hostile page text cannot widen the evidence set.
- **Unsupported Question Fallback:**

  > Return a typed unresolved field, invoke an evidence-constrained cheap answerer, or route to human review. Do not use eval/run-code to invent or conceal an answer and do not submit an incomplete required response.
- **Prompt Injection Resistance:**

  > Higher integration risk than the current MCP isolation. The upstream skill explicitly allows Bash(playwright-cli:*), Bash(npx:*), and Bash(npm:*), and exposes eval and run-code. ApplyPilot currently forbids shell and file tools for the browser agent. A production canary therefore needs a dedicated executable allowlist, fixed argv construction, ATS-origin policy, no arbitrary npm/install/eval/run-code, isolated output paths, and an external submit policy gate.
- **Site Permission And Terms:**

  > CLI control does not grant permission to automate. Use only legitimate candidate application flows allowed by the site and account, honor terms and rate limits, prefer documented ATS interfaces, and stop at login, CAPTCHA, challenge, or access-control boundaries rather than bypassing them.
- **Irreversible Action Guard:**

  > Expose no generic click access to final-submit refs after the plan is complete. The broker should require a submit_once command carrying attempt ID, approved host, expected button identity, required-field/provenance digest, and pre-submit screenshot hash, then persist confirmation before allowing another command.

#### Operations

- **Warm Session Reuse:**

  > Strong. Cookies, localStorage, tabs, navigation history, and console state persist between CLI commands; --persistent survives restarts. ApplyPilot can map one named session to each existing Chrome slot and explicitly detach/close it during worker cleanup.
- **Tracing And Replay:**

  > First-class tracing-start/stop, screenshots, DOM snapshots, console, network, video, storage-state, and dashboard inspection. Trace Viewer supports forensic replay, while run-code or generated scripts can replay deterministic prefixes. Never automatically replay the irreversible submit step.
- **Deployment Modes:**

  > Primarily local CLI plus local or attached browser. It can attach to local/remote CDP and Playwright server endpoints and can run in containerized worker images, but it is not a managed browser service and has no built-in fleet scheduler.
- **Browser Version Matrix:**

  > Can launch Chrome, Chromium, Firefox, WebKit, and Edge through Playwright. CDP and channel attach are Chrome/Edge/Chromium-specific; extension attach is Chrome/Edge. Pin CLI and Playwright core, use separate automation profiles after Chrome 136, and canary browser updates.
- **Canary Stop Loss:**

  > Start with dry-run and synthetic forms. Enforce one worker, small ATS-stratified batch, current 900-second ceiling, an equivalent dollar/token cap, at most one submit, fixed command allowlist, full-snapshot and command limits, zero arbitrary npm/eval/run-code, host/systemic breakers, and immediate rollback on any false APPLIED or duplicate submit.

#### Benchmark

- **Benchmark Design:**

  > Paired comparison against MCP using the same agent/model, candidate evidence, Chrome build, profile class, host policy, and unique ATS-stratified live jobs. Use repeated synthetic forms for crashes, hostile text, required fields, custom widgets, and confirmation. Measure verified completion, false positive/duplicate rate, total agent context, command count, wall time, cost, artifact size, and human minutes.
- **Recommendation:**

  > Canary. It is the closest lower-context replacement for MCP and reuses the current Playwright/CDP/profile investment, but the security model conflicts with ApplyPilot's deliberate no-shell browser-agent isolation. Build a narrow broker, pin 0.x versions, run paired synthetic and live canaries, and adopt only if verified quality is non-inferior and measured all-in cost falls materially.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `cache_replay_yield`
- `concurrency`
- `confidence_intervals`
- `control_plane_overhead`
- `deterministic_coverage`
- `escape_rate`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `model_cost`
- `projected_cost_per_verified_apply`
- `route_funnel`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`

### Playwright MCP over local Chrome CDP

Source record: `Playwright_MCP_over_local_Chrome_CDP.json`

#### Identity

- **Name:** Playwright MCP over local Chrome CDP
- **Category:** agent_control
- **Official Sources:**

  > - https://github.com/microsoft/playwright-mcp<br>- https://www.npmjs.com/package/@playwright/mcp<br>- https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp<br>- https://playwright.dev/docs/api/class-tracing<br>- https://developer.chrome.com/blog/remote-debugging-port
- **Maintenance Status:**

  > Actively maintained. The npm registry reported @playwright/mcp 0.0.78 published on 2026-07-09; ApplyPilot deliberately pins 0.0.76 so upgrades can be canaried rather than changing the live tool surface silently. The upstream repository and Playwright core both show material 2025-2026 development.
- **License:**

  > Apache-2.0. It is a permissive dependency and can be invoked by ApplyPilot's AGPL-3.0 runtime; preserve upstream license and notice material when redistributing or modifying it.

#### Cost Economics

- **Pricing Model:**

  > Open-source, self-hosted software with no MCP subscription or per-tool-call fee. Cash cost comes from the controlling model or fixed agent subscription, local browser compute and power, optional remote infrastructure, and operator recovery.
- **Billing Granularity:**

  > No vendor billing granularity for MCP. The Node MCP process and Chrome consume local resources while running; the agent is billed by token, turn, budget, or subscription according to the selected CLI. ApplyPilot currently starts a fresh MCP process per job against a warm per-worker browser and caps Claude runs at $3.50 and 900 seconds.

#### Reliability

- **Failure Modes:**

  > Observed cost buckets include agent/browser runtime (1,894 failures, $310.2143), preflight or policy (2,042, $289.3490), other failures (842, $469.4951), email/auth (74, $42.1457), and challenges (610, $2.2555). MCP-specific modes include MCP startup/handshake failure, oversized snapshots or context, stale element refs, CDP disconnect, profile lock, browser crash, timeout, model quota/auth failure, unsupported custom widgets, validation loops, and submit-without-confirmation.
- **Crash Recovery:**

  > ApplyPilot keeps Chrome outside the per-job MCP process, so a replacement MCP process can reconnect to the same CDP browser and inspect surviving tabs. It stores the transcript, digest, final-result source, tool counts, and job log. A post-form or post-submit crash is quarantined as crash_unconfirmed/no_confirmation instead of being retried blindly. MCP can save session artifacts, but it does not provide transactional action replay or automatic exactly-once recovery.
- **Exactly Once Submit Safety:**

  > Safety is supplied by ApplyPilot, not MCP: lease renewal before the run, a pre-submit review, one controlled retry after visible validation errors, positive confirmation before APPLIED, and quarantine when submit may have occurred without confirmation. The raw browser_click tool has no intrinsic idempotency key, so final-submit access should remain wrapped by an ApplyPilot guard.
- **Verification Evidence Tier:**

  > Supports Tier 2 evidence directly: confirmation DOM/accessibility snapshot, success URL/state, and screenshot. With DevTools/network capture it can collect stronger Tier 1 evidence such as a successful submission response or ATS reference ID. Email confirmation is a delayed Tier 3 reconciler. Screenshot or agent inference alone must not mark an apply verified.

#### Applypilot Fit

- **Python Integration:**

  > Good existing integration. Python launches and supervises Chrome, writes per-worker MCP configuration, starts Claude or Codex, parses stream JSON, records costs and tool calls, and classifies outcomes. MCP itself is a Node subprocess rather than a native Python API.
- **Playwright Cdp Reuse:**

  > Excellent and already implemented. ApplyPilot launches isolated custom Chrome user-data directories and unique CDP ports, then invokes @playwright/mcp with --cdp-endpoint. Playwright documents CDP attachment as Chromium-only and significantly lower fidelity than its native Playwright protocol, but the current profile, port, fixtures, and browser lifecycle are directly reusable.
- **Migration Effort:**

  > Small to retain as the control arm. Near-term work is instrumentation, version-canary coverage, stronger submit gating, and optional trace/network capture rather than migration. Moving from CDP to the native Playwright protocol would be a separate medium effort because ApplyPilot currently owns Chrome outside Playwright.
- **Ats Fit:**

  > Broadest fit for long-tail and unfamiliar forms because the model can interpret changing page structure. It is expensive on stable Ashby, Greenhouse, and Workable flows and historically weak on Workday, auth-gated tenants, challenge pages, and pages with inaccessible custom controls. Best used after host policy and deterministic adapters decline the job.
- **Observability Joinability:**

  > Strong in the current launcher. worker_id, URL, agent/model, cost, tool counts, last tool, result line, transcript digest, job-log path, and final-result source can join queue and result-event records. Add attempt_id plus network/trace artifact IDs to make browser, model, email, and queue evidence unambiguous across retries.

#### Authentication

- **Persistent Profiles:**

  > ApplyPilot uses separate worker user-data directories, avoiding the Chrome 136 restriction on remote-debugging the default profile. A profile can be reused across jobs but only one Chrome instance may lock it at a time; cloning and rotation must protect cookies and Login Data. Upstream MCP also supports persistent, isolated, and storage-state modes.
- **Existing Session Attach:**

  > Supported through --cdp-endpoint for an existing Chromium browser and through the Playwright browser extension for existing Chrome/Edge tabs. CDP exposes the default context and is lower fidelity than a native Playwright connection. Bind CDP to loopback and never expose an unauthenticated debugging port to the network.
- **Login And Otp:**

  > Can reuse an authenticated worker profile and pause for owner takeover. ApplyPilot may relay approved inbox hints through a separate email path, but MCP should stop on password, SSO, OTP, or security decisions it cannot resolve from approved evidence. It must not bypass CAPTCHAs or access controls.
- **Extension Support:**

  > The Playwright extension can attach MCP to logged-in Chrome/Edge tabs. Testing a side-loaded extension requires a persistent Chromium context; branded Chrome and Edge have tightened command-line side-loading. ApplyPilot should allow only narrowly permissioned extensions and treat native messaging as a separately audited OS boundary.
- **Session Data Boundary:**

  > With local stdio MCP and local CDP, cookies and credentials stay in the local Chrome profile; page snapshots and selected form values are sent to the controlling model, and transcripts, screenshots, traces, resumes, and logs remain on disk unless another service uploads them. Storage-state and trace files can contain secrets and PII, so use restricted ACLs, retention limits, redaction, and no remote CDP exposure.

#### Quality And Safety

- **Required Field Completeness:**

  > Accessibility snapshots, browser_fill_form, and post-fill snapshots can identify required controls, while ApplyPilot's prompt requires a final review. MCP does not guarantee that inaccessible custom widgets, conditional sections, or client-side validation are complete; a deterministic required-field scanner and fail-closed submit gate are still needed.
- **Answer Provenance:**

  > ApplyPilot supplies profile, tailored resume, and approved instructions and explicitly forbids fabricated experience or metrics. MCP has no provenance type system, so enforcement currently depends on the model prompt and review. Structured answer records with source IDs should be required before free-text values reach the browser.
- **Unsupported Question Fallback:**

  > Unknown, sensitive, contradictory, or unsupported questions should return a typed unresolved field, route to a cheap evidence-constrained answerer, or require human review. The agent must not invent an answer merely to clear validation.
- **Prompt Injection Resistance:**

  > Current ApplyPilot isolates the agent, disables host file/shell tools, treats page content as data, and can restrict MCP origins and file access. Upstream explicitly says Playwright MCP is not a security boundary and origin lists do not stop redirects. Keep the model on an ATS allowlist, deny unrelated navigation and secrets, test hostile hidden text, and put final submit behind a non-model policy check.
- **Site Permission And Terms:**

  > MCP is a browser-control interface, not permission to automate. Operate only on legitimate candidate application flows allowed by the site and account, honor rate limits and relevant terms, prefer documented public ATS interfaces, and stop rather than bypass login, challenge, CAPTCHA, or other access controls.
- **Irreversible Action Guard:**

  > ApplyPilot already requires a pre-submit snapshot review and positive post-submit confirmation. Strengthen this by exposing a single submit_once operation that checks required-field completeness, provenance, host policy, current URL, and a durable pre-submit checkpoint before allowing the final click.

#### Operations

- **Warm Session Reuse:**

  > Strong. Chrome, cookies, cache, tabs, and per-worker profile can survive across job turns while each agent/MCP process reconnects through the same CDP port. Reuse must close or reset irrelevant tabs and prevent two workers from sharing one profile.
- **Tracing And Replay:**

  > Playwright can capture DOM snapshots, screenshots, console, network, downloads, video, and trace artifacts. Current ApplyPilot primarily stores agent transcripts and result evidence; enable trace/network capture selectively around canaries and final submit. Trace Viewer supports inspection, not deterministic resubmission, so replay must stop before the irreversible click.
- **Deployment Modes:**

  > Local stdio, local standalone HTTP, self-hosted Docker, and connection to local or remote CDP/Playwright endpoints. The official Docker example currently supports headless Chromium. ApplyPilot's baseline is local Chrome plus local stdio MCP.
- **Browser Version Matrix:**

  > MCP can launch Chrome/Chromium, Firefox, WebKit, or Edge, but --cdp-endpoint attachment is Chromium-family only. ApplyPilot currently uses local Chrome/Chromium. CDP is lower fidelity and browser changes such as Chrome 136 require a non-default user-data directory; pin MCP and canary browser upgrades.
- **Canary Stop Loss:**

  > Retain the current $3.50 per-Claude-attempt budget, 900-second wall timeout, host breaker after 3 host faults, systemic breaker after 5 systemic failures, fleet daily spend cap, and crash quarantine. Add limits for MCP tool calls, full snapshots, repeat-page loops, submit attempts, and per-route verified-failure rate.

#### Benchmark

- **Benchmark Design:**

  > Use this route as the control in a matched ATS-stratified benchmark: Ashby, Greenhouse, Lever, Workable, Workday, and long-tail. Run repeatable synthetic forms three times per route for required fields, custom widgets, auth gates, hostile-page text, crash points, and confirmation states; use unique eligible live applications only once. Compare positive confirmation, cost, duration, commands, context bytes, and human minutes against CLI and deterministic drivers.
- **Historical Bucket Impact:**

  > This is the observed baseline, not a projected saving: $2.0405 all-in per verified apply. Current high-cost targets are Workday at $14.6340/apply, Lever at $4.0949, other hosts at $3.1846, agent/browser runtime failures at $310.2143, and other failures at $469.4951. Keeping MCP for every eligible form will not reach the sub-$1 target without host policy and deterministic routes.
- **Recommendation:**

  > Adopt as the required control arm and retain as the guarded fallback for unfamiliar or high-risk forms, but do not use it as the default for stable ATS variants. Pin versions, add route-level attribution and stronger submit/network evidence, and compare every cheaper route against its verified quality and $2.0405 all-in baseline.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `cache_replay_yield`
- `concurrency`
- `confidence_intervals`
- `control_plane_overhead`
- `deterministic_coverage`
- `escape_rate`
- `headless_headed_parity`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `model_cost`
- `projected_cost_per_verified_apply`
- `route_funnel`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`

### Existing-browser extension and content-script executor

Source record: `Existingbrowser_extension_and_contentscript_executor.json`

#### Identity

- **Name:** Existing-browser extension and content-script executor
- **Category:** authenticated_browser
- **Official Sources:**

  > - https://developer.chrome.com/docs/extensions/develop/concepts/content-scripts<br>- https://developer.chrome.com/docs/extensions/reference/api/scripting<br>- https://developer.chrome.com/docs/extensions/reference/api/debugger<br>- https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging<br>- https://developer.chrome.com/docs/extensions/develop/concepts/declare-permissions<br>- https://developer.chrome.com/blog/remote-debugging-port<br>- https://chromedevtools.github.io/devtools-protocol/
- **Maintenance Status:**

  > Platform route rather than a standalone project. Chrome continues to maintain Manifest V3 extension APIs; current documentation includes Chrome 125 flat debugger sessions and the Chrome 136 remote-debugging security change. ApplyPilot would own all executor code and maintenance.
- **License:**

  > ApplyPilot-authored extension code can remain AGPL-3.0. Chrome documentation examples are Apache-2.0 and documentation text is CC BY 4.0. Browser APIs themselves impose no code-license dependency, but Chrome Web Store policies and third-party site terms remain operational constraints.

#### Cost Economics

- **Pricing Model:**

  > Self-hosted in the user's existing Chrome or Edge profile. No vendor subscription, token, query, browser-hour, or concurrency fee; costs are local compute, extension development, signing/distribution if used, and operator recovery.
- **Billing Granularity:**

  > No external billing unit. Browser and extension idle time are not vendor-billed. Local infrastructure is paid at machine/power/storage granularity, while premium fallback remains billed under its own model or service terms.
- **Model Cost:**

  > Zero for deterministic DOM inventory, fill, validation, submit, and confirmation logic. Semantic interpretation or unsupported-question fallback may invoke ApplyPilot's existing models and must be attributed separately.

#### Reliability

- **Failure Modes:**

  > Extension missing or disabled; host permission absent; content script not injected; MV3 service worker suspended; stale tab/frame IDs; cross-origin or out-of-process iframe handling error; restricted browser page; profile locked; navigation invalidates state; unsupported widget; OTP or challenge; native host unavailable; wrong control; duplicate click; or ambiguous confirmation.
- **Exactly Once Submit Safety:**

  > Strong if designed explicitly: durable pre-submit intent, tab/form fingerprint, all-fields-complete checkpoint, one authorized submit command, immediate network/DOM capture, duplicate-application check, and crash_unconfirmed quarantine. Chrome APIs provide mechanisms, not idempotency.
- **Headless Headed Parity:**

  > Headed and user-controlled by design. It avoids headless parity risk for the primary route, but there is no equivalent unattended headless mode unless a separate browser is launched and tested.

#### Applypilot Fit

- **Python Integration:**

  > Medium to strong. The content script is JavaScript, but a native-messaging host, loopback authenticated service, or extension service worker can exchange typed commands and events with ApplyPilot's Python runtime. Keep Python as queue, answer, policy, and evidence authority.
- **Playwright Cdp Reuse:**

  > Partial. It reuses the same authenticated tabs and profiles without Playwright attachment. chrome.debugger is an in-extension CDP transport for approved domains, but it is not a Playwright Browser object. Shared adapter logic and fixtures can be reused; Playwright-specific Locator and Trace Viewer integration needs a bridge or parallel implementation.
- **Control Plane Overhead:**

  > Low per step: compact typed commands and structured DOM/evidence results over local IPC, with no screenshot or full-DOM model round trip for deterministic paths. MV3 wakeups, frame routing, serialization, and acknowledgements still need measurement.
- **Observability Joinability:**

  > Strong if every command carries ApplyPilot attempt_id, tab_id, frame_id, adapter/version, step sequence, field provenance, timestamps, and evidence hashes. chrome.debugger can add network and trace signals; store them in existing result events rather than extension-local storage.

#### Authentication

- **Persistent Profiles:**

  > Strong for a user-owned browser profile because the extension runs where cookies and sessions already exist. Chrome owns profile locking and version migration. Do not clone an active profile directory; use dedicated user profiles and explicit account-to-worker ownership.
- **Existing Session Attach:**

  > Strongest feature. An installed extension can enumerate permitted existing tabs and inject or register content scripts without launching another browser. Unlike ordinary Chrome 136+ remote debugging, it does not require exposing the default data directory through a remote-debugging port.
- **Login And Otp:**

  > Strong owner-takeover ergonomics: the user can log in, complete OTP, or resolve a permitted challenge in the same visible tab, then return control. The extension should not read password fields or OTPs unless narrowly required and explicitly authorized.
- **Extension Support:**

  > Native capability. Manifest V3 supports static and dynamic content scripts, one-off scripting, tab messaging, optional host permissions, chrome.debugger with a prominent permission, and native messaging. Minimize permissions and avoid broad debugger access unless network evidence requires it.
- **Session Data Boundary:**

  > Best local boundary of the evaluated routes: cookies remain in the user's browser and commands can stay on-device. Content scripts can still see sensitive rendered data, and a compromised extension or native host is high impact. Use domain allowlists, authenticated local IPC, no remote listener, secret redaction, minimal retention, and signed updates.

#### Quality And Safety

- **Answer Provenance:**

  > Strong. The extension can receive only field IDs plus values already approved by ApplyPilot's profile, resume, and answer store, with provenance IDs attached. It should never invent or semantically expand answers.
- **Unsupported Question Fallback:**

  > Fail closed. Return the exact label, surrounding section, control type, required state, and redacted DOM fingerprint to Python; route to an approved answer, semantic fallback, or owner review. Never guess inside the content script.
- **Site Permission And Terms:**

  > Operate only on employer/ATS pages the account owner is authorized to use. Extension installation does not grant permission to automate a site. Respect ATS/employer terms, rate limits, robots or documented interfaces where applicable, and never use the route to bypass access controls or challenges.
- **Irreversible Action Guard:**

  > Strong. Keep final submit unavailable to ordinary content-script planning. Require a separate signed/nonce-bound command from Python after complete-field and policy checks, verify the exact form/button again, consume the nonce once, and capture immediate outcome evidence.

#### Operations

- **Concurrency:**

  > One active attempt per authenticated account/profile is the conservative default. Multiple tabs and profiles are technically possible, but MV3 service-worker state, host permissions, and account rate limits require isolation and per-profile leases.
- **Warm Session Reuse:**

  > Excellent. Reuse the user's existing tab, cookies, browser cache, extension installation, and local adapter cache. Revalidate URL, account, form fingerprint, and ownership before every attempt.
- **Tracing And Replay:**

  > Good but custom. Capture typed command/event logs, redacted DOM snapshots, network evidence through chrome.debugger when authorized, screenshots, and adapter fingerprints. Deterministic replay can run against stored synthetic DOM fixtures; full live replay remains unsafe around submit.
- **Deployment Modes:**

  > Local unpacked extension for development, enterprise/policy-installed or store-distributed extension for managed fleets, optional native-messaging host, or authenticated loopback Python service. No managed cloud is required.
- **Canary Stop Loss:**

  > Implement per-attempt step/time limits, one submit nonce, no automatic retry after submit intent, maximum extension disconnects, per-host failure-rate breaker, daily fallback/model dollar cap, permission-change alarm, and immediate pause on duplicate or ambiguous outcomes.

#### Benchmark

- **Benchmark Design:**

  > Build synthetic MV3 fixtures first, then use at least 600 matched forms stratified across Ashby, Greenhouse, Lever, Workable, Workday tenants, and long tail. Compare existing Playwright adapters and extension execution on field recall, value accuracy, latency, auth reuse, resource cost, escape, and positive confirmation. Canary submit only after shadow parity.
- **Route Funnel:**

  > Record eligible authenticated tab -> permission present -> extension handshake -> adapter selected -> required fields inventoried -> provenance-approved plan complete -> fill/validate -> submit nonce issued -> one click -> network/DOM evidence -> delayed email evidence -> verified applied, fallback, blocked, failed, or crash_unconfirmed.
- **Step Context Budget:**

  > Use compact local messages: one inventory result, one approved fill plan, bounded validation corrections, one submit authorization, and one confirmation result. Record DOM nodes inspected, bytes transferred, debugger events, screenshots, and fallback calls; no model tokens on the deterministic path.
- **Recommendation:**

  > ADOPT FOR DEVELOPMENT, THEN CANARY. This is the preferred route when the explicit goal is to reuse an authenticated user tab without another browser launch. Keep it deterministic, local, least-privileged, and subordinate to ApplyPilot's Python policy/evidence control plane; do not use it for access-control bypass.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `browser_version_matrix`
- `cache_replay_yield`
- `confidence_intervals`
- `crash_recovery`
- `deterministic_coverage`
- `escape_rate`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `projected_cost_per_verified_apply`
- `prompt_injection_resistance`
- `required_field_completeness`
- `startup_latency`
- `verification_evidence_tier`
- `verification_latency`
- `verified_completion_rate`

### Camoufox

Source record: `Camoufox.json`

#### Identity

- **Name:** Camoufox
- **Category:** compatibility_browser
- **Official Sources:**

  > - https://github.com/daijro/camoufox<br>- https://github.com/daijro/camoufox/releases<br>- https://camoufox.com/python/<br>- https://pypi.org/project/camoufox/<br>- https://github.com/daijro/camoufox/blob/main/LICENSE
- **Maintenance Status:**

  > Active but higher-risk maintenance profile. The repository is not archived, was pushed in July 2026, and its latest GitHub prerelease is v150.0.2-beta.25 from May 11, 2026. Official project text acknowledges a prior year-long maintenance gap and says development has resumed. The Python package and browser release channels can lag each other.
- **License:**

  > The patched browser source is MPL-2.0, a file-level copyleft license generally compatible with aggregation alongside AGPL-3.0 when MPL-covered modifications remain under MPL and notices/source obligations are met. PyPI metadata lists the Python wrapper as MIT. Confirm packaging boundaries and preserve both sets of notices.

#### Cost Economics

- **Pricing Model:**

  > Free open-source/self-hosted software. No browser-hour or token fee. Costs are local CPU/RAM/storage/power, browser binary downloads, integration, regression testing, and maintenance of a patched Firefox/Juggler dependency.
- **Billing Granularity:**

  > No external metering or included-usage tier. Costs accrue at machine uptime, power, storage, bandwidth, and engineering-time granularity. Optional proxies or hosted infrastructure are separate services.
- **Model Cost:**

  > None inherent. Camoufox is a browser plus Playwright-compatible Python interface; ApplyPilot retains all model selection and token cost.

#### Reliability

- **Failure Modes:**

  > Browser/package release mismatch; prerelease regression; unsupported Playwright behavior; Firefox-specific ATS defect; add-on conflict; download or binary startup failure; profile/auth loss; fingerprint configuration inconsistency; iframe or custom-widget mismatch; challenge; unknown field; browser crash; and ambiguous post-submit state.
- **Exactly Once Submit Safety:**

  > No native idempotency. Reuse ApplyPilot's durable pre-submit checkpoint, duplicate detection, one-click authorization, positive response/DOM capture, and crash_unconfirmed quarantine. Browser compatibility does not change the irreversible-action contract.

#### Applypilot Fit

- **Python Integration:**

  > Strong at API shape. Official sync and async Python wrappers are presented as Playwright-compatible with changed browser initialization, and PyPI currently declares Python >=3.8,<4.0. ApplyPilot's Python >=3.11 runtime is compatible.
- **Playwright Cdp Reuse:**

  > Weak for current assets. Camoufox is Firefox-based and controlled through patched Juggler rather than Chromium CDP, so existing Chrome CDP ports, Chrome profiles, Chromium-only fixtures, and browser channels cannot be reused directly. Higher-level Playwright page adapters may transfer.
- **Control Plane Overhead:**

  > Low model overhead because Camoufox itself adds no agent loop. Process startup, a separate browser binary, fingerprint/config generation, and Playwright protocol traffic add local overhead; precise latency and memory are unmeasured.
- **Observability Joinability:**

  > Good through Playwright if ApplyPilot preserves attempt IDs across launch, context, page, trace, network, and result events. Also log Camoufox Python package, browser build/channel, Firefox version, config hash, profile ID, and fallback reason.

#### Authentication

- **Existing Session Attach:**

  > Weak. The wrapper launches Camoufox or can expose a Playwright server; it does not attach to an already-authenticated Chrome tab/profile. Reconnecting to a Camoufox server is a separate managed session, not reuse of the user's ordinary browser.
- **Login And Otp:**

  > Headed operation allows manual login and OTP, but ApplyPilot must build or reuse its own owner-takeover and inbox relay. A separate Camoufox profile may increase initial auth work and cannot inherit Chrome App-Bound session data.
- **Session Data Boundary:**

  > Local/self-hosted by default; cookies, resumes, screenshots, and traces can remain on the fleet machine. Optional proxy geolocation performs a network lookup and proxy use sends traffic to that provider. Treat third-party add-ons, update assets, and proxy services as separate trust boundaries.

#### Quality And Safety

- **Answer Provenance:**

  > Neutral. The browser does not generate answers. ApplyPilot can preserve its profile/resume/approved-answer provenance unchanged if adapters remain deterministic.
- **Unsupported Question Fallback:**

  > Use existing fail-closed behavior: capture exact question and control context, do not infer, and route to approved-answer lookup, semantic fallback, or owner review. Camoufox adds no policy layer.
- **Site Permission And Terms:**

  > Use only for legitimate compatibility testing and authorized applications. The project's anti-detection positioning does not authorize evasion. Do not configure it to bypass bot controls, challenges, or site restrictions; separately comply with ATS/employer terms and access policies.
- **Irreversible Action Guard:**

  > Keep final submit in ApplyPilot's existing deterministic guard. Require complete approved answers, expected domain/form fingerprint, one durable submit intent, one click, and immediate positive-evidence capture; never let browser configuration authorize submission.

#### Operations

- **Concurrency:**

  > Technically bounded by local CPU/RAM, profiles, and account limits; there is no vendor cap or surcharge. Use one active attempt per authenticated profile/account and benchmark multi-process stability before fleet use.
- **Warm Session Reuse:**

  > Moderate. A long-lived Camoufox browser/context or Playwright server can be reused, and page cache can be enabled, but safe durable auth/profile reuse across updates and crashes is not documented to ApplyPilot's needs.
- **Tracing And Replay:**

  > Expected Playwright tracing, screenshots, video, DOM, and network logging should be available subject to Firefox/Juggler compatibility. Camoufox has no separate deterministic workflow replay; store version/config and replay only against synthetic fixtures.
- **Deployment Modes:**

  > Local Windows, macOS, and Linux packaged binaries; self-built binaries; Docker-based builds; Linux virtual display; and a Playwright server mode. No official managed browser cloud is part of the core project.
- **Browser Version Matrix:**

  > Camoufox is a patched Firefox distribution with its own numbered builds and Juggler integration. It is not Chrome/Chromium, WebKit, CDP, or BiDi parity. Official releases provide platform/architecture assets, but ApplyPilot must pin and test an exact browser/package pair.
- **Canary Stop Loss:**

  > Pin one stable browser/package pair; stop on unexpected update, launch/profile failure, host regression, evidence loss, or increased auth intervention. Enforce per-attempt time/step limits, no post-submit retry, per-host error breaker, and a small shadow-only cohort before any submit canary.

#### Benchmark

- **Benchmark Design:**

  > Use the same matched 600-form ATS-stratified set as Chromium, with exact Camoufox build/config recorded. Compare deterministic field inventory, fill, validation, headed behavior, login persistence, latency, resource use, fallback, and positive confirmation. Exclude anti-detection tests from the adoption gate; this research is compatibility-only.
- **Route Funnel:**

  > Record eligible compatibility cohort -> binary/profile ready -> browser launch -> adapter selected -> fields inventoried -> approved plan complete -> fill/validate -> submit authorized -> one click -> positive evidence -> verified applied, fallback to Chrome, blocked, failed, or crash_unconfirmed.
- **Step Context Budget:**

  > No inherent model context. Record Playwright actions, DOM/accessibility observations, network events, screenshots, trace bytes, process CPU/RAM, and any fallback model tokens. Use the same bounded action plan as the current deterministic route.
- **Recommendation:**

  > WATCH; optional shadow compatibility canary only. Do not adopt as the default ApplyPilot browser: it cannot reuse current Chrome profiles/CDP infrastructure, adds a second engine and maintenance surface, and has no ATS evidence. Consider only if a measured, legitimate Firefox-compatibility cohort materially outperforms standard Playwright Firefox/Chrome without bypass behavior.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `cache_replay_yield`
- `confidence_intervals`
- `crash_recovery`
- `deterministic_coverage`
- `escape_rate`
- `extension_support`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `persistent_profiles`
- `projected_cost_per_verified_apply`
- `prompt_injection_resistance`
- `required_field_completeness`
- `startup_latency`
- `verification_evidence_tier`
- `verification_latency`
- `verified_completion_rate`

### nodriver

Source record: `nodriver.json`

#### Identity

- **Name:** nodriver
- **Category:** compatibility_browser
- **Official Sources:**

  > - Official repository: https://github.com/ultrafunkamsterdam/nodriver<br>- Official documentation: https://ultrafunkamsterdam.github.io/nodriver/<br>- Official quickstart and profile configuration: https://ultrafunkamsterdam.github.io/nodriver/nodriver/quickstart.html<br>- PyPI releases and metadata: https://pypi.org/project/nodriver/<br>- AGPL-3.0 license: https://github.com/ultrafunkamsterdam/nodriver/blob/main/LICENSE.txt
- **Maintenance Status:**

  > Active but operationally immature. PyPI shows repeated releases during 2025 and version 0.50.3 on 2026-05-13; however, PyPI classifies the project as Development Status 3 - Alpha, the documentation contains stale examples, and the 0.50.x flat-connection rewrite requests thorough testing.
- **License:**

  > AGPL-3.0. This is license-compatible with ApplyPilot's AGPL-3.0 codebase when distributed under the same terms, but deployment and source-offer obligations still require normal AGPL compliance review.

#### Cost Economics

- **Pricing Model:**

  > Free, self-hosted Python package. There is no vendor usage fee; cost is local Chrome compute, profile storage, traces, and any model used above the deterministic control layer.
- **Billing Granularity:**

  > No billing minimum. Warm browsers consume memory while idle; cost accrues at machine, power, storage, and operator granularity rather than per API call.
- **Model Cost:**

  > Zero for fixed selectors, typed field mappings, CDP events, and deterministic verification. Any semantic question handling or general-agent fallback must be metered separately.

#### Reliability

- **Failure Modes:**

  > - Selector or text-match drift and custom-control incompatibility<br>- Async event races, detached targets, iframe or flat-session regressions<br>- Profile locking, stale cookies, browser process leakage, or incompatible Chrome updates<br>- Missing Playwright-style actionability and auto-wait guarantees<br>- Login, OTP, CAPTCHA, policy, and unsupported-field walls<br>- Submit request succeeds but confirmation evidence is absent<br>- Crash after submit creates an ambiguous outcome that must not be retried
- **Crash Recovery:**

  > The library exposes CDP events and supports persistent data directories, but ApplyPilot must implement checkpoints, process cleanup, artifact retention, and restart policy. A pre-submit crash may restart; any crash after submit_started must be quarantined and independently reconciled.
- **Exactly Once Submit Safety:**

  > Not built in. Add ApplyPilot queue deduplication, durable pre-submit and submit_started states, one submit authority, response capture, and a no-retry ambiguous state. nodriver's low-level click and CDP APIs do not provide transaction semantics.
- **Verification Evidence Tier:**

  > Can capture strong evidence through CDP Network response bodies/IDs, resulting URL and DOM, cookies/storage, and screenshots. Prefer explicit ATS application ID or success response, then allowlisted confirmation DOM/URL, then matched email; never accept element disappearance alone.

#### Applypilot Fit

- **Python Integration:**

  > Good. It is Python >=3.9 and fully asynchronous, while ApplyPilot's current browser path is predominantly synchronous Playwright. Integration needs an asyncio boundary, cancellation handling, and structured conversion of CDP events into existing result metadata.
- **Playwright Cdp Reuse:**

  > Partial. nodriver can start Chrome with an existing user_data_dir and can connect to a running debug session, so dedicated profiles and CDP endpoints are reusable. Playwright Browser/Page objects, locators, traces, fixtures, and existing adapter code are not reusable directly.
- **Control Plane Overhead:**

  > Low serialization overhead and direct in-process CDP access, but ApplyPilot must manage an async event loop, target/session events, retries, and browser lifecycle. It avoids WebDriver and MCP round trips while increasing custom control-plane code.
- **Observability Joinability:**

  > Potentially strong through raw CDP Network, Runtime, Log, DOMSnapshot, Tracing, and screenshot domains. ApplyPilot must persist attempt_id, target/session IDs, request IDs, action index, profile slot, artifact hashes, confirmation source, and browser version into apply_result_events.

#### Authentication

- **Persistent Profiles:**

  > Supported through user_data_dir; a supplied directory is not automatically deleted. Fresh temporary profiles are otherwise the default, and cookies can be saved/loaded. ApplyPilot must enforce one process per profile, locking, secure storage, backup, and Chrome-version compatibility.
- **Existing Session Attach:**

  > Supported through host/port configuration and documented connection to a running Chrome debugging session. Safe use requires loopback-only CDP, browser identity checks, profile ownership, expected account/origin validation, and protection against another controller owning the tab.
- **Login And Otp:**

  > No managed login, inbox, OTP relay, or owner takeover workflow. Existing profile cookies can reduce repeated login, but ApplyPilot must retain dedicated inbox and supervised owner-profile lanes for verification codes and decisions.
- **Extension Support:**

  > Config.add_extension can load an unpacked directory or CRX. Use only narrowly permissioned extensions on dedicated profiles; native messaging and extension lifecycle remain ApplyPilot responsibilities.
- **Session Data Boundary:**

  > Local/self-hosted by default. Cookies, local storage, profiles, resumes, and screenshots stay on the worker unless ApplyPilot transmits them. Raw traces and logs may contain secrets and candidate data, so use local encryption, redaction, access controls, and short retention.

#### Quality And Safety

- **Required Field Completeness:**

  > No built-in form completeness model. Implement typed field discovery, conditional-field reevaluation, required-state checks, validation-message capture, and refusal when any required field is unmapped.
- **Answer Provenance:**

  > Not provided. Keep answers constrained to candidate profile, resume, approved policy fields, and verifier-approved free text, with field-level provenance stored before submission.
- **Unsupported Question Fallback:**

  > Return a structured unmapped-required list and leave submit untouched. Route to the existing semantic or supervised lane before submit; never guess sensitive, legal, demographic, or experience answers.
- **Prompt Injection Resistance:**

  > Deterministic code can be strong if it uses host allowlists, fixed CDP commands, typed schemas, and no page-directed tool choice. Treat all page text as untrusted and do not use nodriver's challenge-bypass helpers against access controls.
- **Irreversible Action Guard:**

  > Keep dry-run and shadow modes, require a complete plan and durable pre-submit checkpoint, grant submit authority to one component, capture the exact request/action once, and disable all fallback submitters after submit_started.

#### Operations

- **Warm Session Reuse:**

  > Good through running-browser attachment, persistent user_data_dir, multiple tabs, and cookie persistence. Reuse the browser but create a fresh attempt page/state and verify current login, posting liveness, and form schema.
- **Tracing And Replay:**

  > Raw CDP includes network, console, DOM snapshot, screenshot, screencast, and tracing capabilities, but nodriver does not supply Playwright Trace Viewer or deterministic business replay. Build structured action logs and replay only against synthetic fixtures, never a production submit endpoint.
- **Deployment Modes:**

  > Local or self-hosted Python on machines that can launch or reach Chromium-family browsers. No first-party managed cloud, hosted profile service, or turnkey container control plane is included.
- **Browser Version Matrix:**

  > Documented to work with Chromium, Chrome, Edge, and Brave through CDP. Firefox and WebKit are unsupported; exact Chrome/CDP version guarantees are not published, so pin nodriver and certify current stable Chrome per worker OS.
- **Canary Stop Loss:**

  > Shadow only first. Stop immediately on duplicate or ambiguous submit, wrong answer, challenge interaction, profile crossover, browser leak, secret exposure, or false applied state; pause if positive confirmation is below 85%, no-confirmation exceeds 10%, or all-in cost exceeds the Playwright control.

#### Benchmark

- **Benchmark Design:**

  > Compare pinned nodriver against current Playwright/CDP on synthetic ATS mirrors and unique live jobs stratified by Ashby, Greenhouse, Lever, Workable, Workday, and long-tail. Repeat warm/cold, headed/headless, profile attach, iframe, upload, crash, and delayed-confirmation cases with at least 30 attempts per promoted stratum.
- **Route Funnel:**

  > Record eligibility, profile acquired, browser attached, page live, fields discovered, plan complete, fill complete, validation clean, submit gate, submit_started, request observed, positive DOM/network evidence, email match, pre-submit fallback, ambiguous quarantine, and final disposition.
- **Recommendation:**

  > WATCH, with a small shadow benchmark only. nodriver's async Python CDP, profile support, and active releases are useful, but alpha status, a separate API from Playwright, custom reliability work, and bypass-oriented features make it a weak replacement for ApplyPilot's existing deterministic Playwright architecture. Reconsider only if a pinned comparator materially reduces runtime failures or latency.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `cache_replay_yield`
- `concurrency`
- `confidence_intervals`
- `deterministic_coverage`
- `escape_rate`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `projected_cost_per_verified_apply`
- `site_permission_and_terms`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`

### Patchright

Source record: `Patchright.json`

#### Identity

- **Name:** Patchright
- **Category:** compatibility_browser
- **Official Sources:**

  > - https://github.com/Kaliiiiiiiiii-Vinyzu/patchright<br>- https://github.com/Kaliiiiiiiiii-Vinyzu/patchright/releases<br>- https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python<br>- https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python/releases<br>- https://pypi.org/project/patchright/<br>- https://playwright.dev/python/docs/intro
- **Maintenance Status:**

  > Actively maintained fork with synchronization risk. The driver released v1.61.1 on June 23, 2026; the Python repository released v1.61.0 on July 5, 2026; PyPI reports 1.61.2. The project runs Playwright tests after releases but explicitly says not all tests pass and upstream changes can take days to repair.
- **License:**

  > Apache-2.0 for the driver and Python package, permissive and compatible with inclusion in ApplyPilot's AGPL-3.0 codebase when notices are retained. The repository also carries an educational-use/no-warranty disclaimer; legal use and third-party terms remain ApplyPilot's responsibility.

#### Cost Economics

- **Pricing Model:**

  > Free open-source/self-hosted package. No subscription, token, query, browser-hour, or concurrency fee. Costs are local Chromium/Chrome resources, package/browser downloads, testing, integration, and maintenance of a downstream Playwright fork.
- **Billing Granularity:**

  > No external metering. Local cost accrues by machine uptime, power, storage, bandwidth, and engineering time. Any proxy, hosted compute, or model service is billed independently.
- **Model Cost:**

  > None inherent. Patchright is a Playwright fork and does not include a model or agent. ApplyPilot retains its current token/model economics.

#### Reliability

- **Failure Modes:**

  > Fork lag after Playwright releases; Python/driver/browser version mismatch; known test incompatibility; console API unavailable; init-script route side effects; execution-context race; Chromium-only limitation; extension/profile launch issue; ATS widget regression; auth/challenge; unknown required field; crash; or ambiguous submit result.
- **Exactly Once Submit Safety:**

  > No built-in idempotency. Preserve ApplyPilot's duplicate checks, complete-plan checkpoint, durable submit intent, exactly one authorized click, immediate response/DOM capture, and crash_unconfirmed handling.

#### Applypilot Fit

- **Python Integration:**

  > Strong. The official Python package uses sync and async Playwright-shaped APIs and is presented as an import-level replacement. Current PyPI metadata requires Python >=3.10, compatible with ApplyPilot's Python >=3.11 runtime.
- **Playwright Cdp Reuse:**

  > Strong but not identical. Most Chromium Playwright adapters, locators, fixtures, persistent contexts, and likely connect_over_cdp flows transfer. Patchright changes driver internals, execution contexts, console behavior, and launch defaults, so current tests must run unchanged before claiming parity.
- **Control Plane Overhead:**

  > Low. It retains local Playwright command/response control and adds no model loop or hosted serialization. Fork-specific routing for init scripts and isolated execution contexts may add unmeasured overhead.
- **Observability Joinability:**

  > Good through Playwright traces, network, DOM, screenshots, and video, but console instrumentation is explicitly disabled. Persist attempt_id, Patchright package/driver/browser versions, launch args, profile ID, route, adapter version, trace ID, and fallback reason.

#### Authentication

- **Persistent Profiles:**

  > Strong for dedicated automation profiles. The project recommends launch_persistent_context with user_data_dir, channel='chrome', headless=False, and no viewport override. Do not point concurrent workers at one profile or assume an active default Chrome profile can be opened safely.
- **Login And Otp:**

  > Comparable to headed Playwright with a persistent Chrome profile: manual takeover and ApplyPilot's inbox/OTP flow can be reused. Patchright adds no OTP relay, password vault, or owner-control plane.
- **Extension Support:**

  > Better than Playwright's default launch flags in one narrow sense: Patchright removes the default --disable-extensions flag, and persistent Chrome contexts can load approved extensions. Actual MV3/native-messaging compatibility and packaging must still be tested.
- **Session Data Boundary:**

  > Local by default. Cookies, resumes, screenshots, traces, and browser state stay on the fleet machine unless ApplyPilot configures proxies, cloud storage, or model calls. A downstream browser driver is privileged code, so pin hashes, review updates, and isolate profiles.

#### Quality And Safety

- **Answer Provenance:**

  > Neutral and compatible. The browser does not generate answers; ApplyPilot can retain profile/resume/approved-answer provenance and send only authorized values to locators.
- **Unsupported Question Fallback:**

  > Fail closed using existing ApplyPilot policy. Capture exact question/control context, do not infer, and route to approved-answer lookup, semantic fallback, or owner review.
- **Site Permission And Terms:**

  > Use only for ordinary authorized browser compatibility. Do not use anti-detection features or claims to evade access controls, challenges, bot policies, or third-party restrictions. Comply separately with each ATS/employer's terms and documented interfaces.
- **Irreversible Action Guard:**

  > Keep the current ApplyPilot submit guard unchanged: expected domain and form fingerprint, all required answers approved, durable intent, one click, immediate evidence capture, and no automatic retry after ambiguous outcome.

#### Operations

- **Concurrency:**

  > No vendor cap; bounded by local CPU/RAM, dedicated profiles, account rules, and browser stability. Use one attempt per authenticated profile/account and qualify multi-worker behavior with exact pinned versions.
- **Warm Session Reuse:**

  > Strong with a long-lived browser or dedicated persistent context. Revalidate page, account, profile lease, adapter fingerprint, and package/browser versions before reuse; never share one writable profile concurrently.
- **Tracing And Replay:**

  > Mostly inherited from Playwright: traces, network, DOM, screenshots, and video should be available, while console functionality is disabled. Deterministic fixture replay remains viable, but store the exact fork and browser versions because upstream traces may not reproduce identically.
- **Deployment Modes:**

  > Local/self-hosted Python, Node.js, and community .NET packages with installed Chromium or Chrome. Containers are possible through normal Playwright patterns. No official managed cloud service is required.
- **Browser Version Matrix:**

  > Chromium-based browsers only; Firefox and WebKit are explicitly unsupported. The project recommends Google Chrome for its preferred setup. CDP is inherited through the Chromium stack; WebDriver BiDi support is not documented as a compatibility target.
- **Canary Stop Loss:**

  > Pin package, driver, and browser; stop on version drift, console/evidence loss, fixture regression, profile failure, or increased ambiguous outcomes. Enforce per-attempt time/step limits, no post-submit retry, host failure breaker, and immediate rollback to standard Playwright.

#### Benchmark

- **Benchmark Design:**

  > Run the current Playwright suite unchanged first, then at least 600 matched ATS-stratified forms with standard Playwright and Patchright on the same Chrome version/profile class. Compare field recall, action correctness, latency, resource cost, auth reuse, fallback, trace completeness, and positive confirmation. Do not include access-control bypass as a success criterion.
- **Route Funnel:**

  > Record eligible compatibility experiment -> exact versions ready -> profile acquired -> browser attached/launched -> adapter selected -> required fields inventoried -> approved plan complete -> fill/validate -> submit authorized -> one click -> positive evidence -> verified applied, fallback to standard Playwright, blocked, failed, or crash_unconfirmed.
- **Step Context Budget:**

  > No inherent model context. Record Playwright actions, DOM/accessibility observations, network events, screenshots, trace bytes, CPU/RAM, and fallback tokens. Keep the same bounded deterministic action budget as the current route.
- **Recommendation:**

  > REJECT AS A DEFAULT; WATCH OR SHADOW-CANARY ONLY FOR A REPRODUCIBLE STANDARD-PLAYWRIGHT COMPATIBILITY DEFECT. Its main advertised differentiation is anti-detection, which is outside the permitted use case. Standard Playwright has lower maintenance and policy risk; Patchright needs measured ATS benefit large enough to justify a privileged downstream fork.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `cache_replay_yield`
- `confidence_intervals`
- `crash_recovery`
- `deterministic_coverage`
- `escape_rate`
- `existing_session_attach`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `projected_cost_per_verified_apply`
- `prompt_injection_resistance`
- `required_field_completeness`
- `startup_latency`
- `verification_evidence_tier`
- `verification_latency`
- `verified_completion_rate`

### ATS-specific deterministic Playwright adapters

Source record: `ATSspecific_deterministic_Playwright_adapters.json`

#### Identity

- **Name:** ATS-specific deterministic Playwright adapters
- **Category:** deterministic
- **Official Sources:**

  > - Playwright Python locators: https://playwright.dev/python/docs/locators<br>- Playwright Python actionability and auto-waiting: https://playwright.dev/python/docs/actionability<br>- Playwright Python input and file upload: https://playwright.dev/python/docs/input<br>- Playwright Python tracing: https://playwright.dev/python/docs/api/class-tracing<br>- Playwright Python connect_over_cdp: https://playwright.dev/python/docs/api/class-browsertype<br>- Playwright Python releases: https://github.com/microsoft/playwright-python/releases<br>- Playwright Apache-2.0 license: https://github.com/microsoft/playwright/blob/main/LICENSE<br>- Greenhouse Job Board API: https://developers.greenhouse.io/job-board.html<br>- Lever Postings API: https://github.com/lever/postings-api<br>- ApplyPilot Greenhouse adapter: C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot/.worktrees/codex-apply-cost-quality-router-phase1/src/applypilot/apply/greenhouse_adapter.py<br>- ApplyPilot Greenhouse submit path: C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot/.worktrees/codex-apply-cost-quality-router-phase1/src/applypilot/apply/greenhouse_submit.py<br>- ApplyPilot Lever adapter: C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot/.worktrees/codex-apply-cost-quality-router-phase1/src/applypilot/apply/lever_adapter.py<br>- ApplyPilot cost-quality design: C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot/.worktrees/codex-apply-cost-quality-router-phase1/docs/superpowers/specs/2026-07-06-apply-cost-quality-router-design.md
- **Maintenance Status:**

  > Strong. Playwright Python had regular releases throughout 2025 and reached v1.60.0 in May 2026. ApplyPilot's Greenhouse and Lever adapters, launcher routing, result metadata, and tests are present in the current worktree. The Greenhouse path is guarded and can own a verified submit; Lever is shadow fill-only. Maintenance risk is primarily ATS DOM and form-contract drift, not an abandoned dependency.
- **License:**

  > Playwright is Apache-2.0 licensed and compatible for use by ApplyPilot's AGPL-3.0-only project. The adapter code remains ApplyPilot AGPL code. Greenhouse and Lever documentation describes their interfaces but does not grant a separate software license or override site terms.

#### Cost Economics

- **Pricing Model:**

  > Local or self-hosted browser automation with no Playwright usage fee. Cost is Chrome process time, worker compute, trace storage, and optional verified-answer model calls. Existing fleet hardware and profiles can be reused, so there is no required hosted-browser subscription.
- **Billing Granularity:**

  > No minimum billing unit beyond machine uptime. Chrome consumes RAM while warm even when idle, and each Playwright action waits up to its configured timeout. Keep per-step and per-job deadlines below the expensive agent path, retain traces only on canary or failure, and account for optional answer calls individually.
- **Model Cost:**

  > Zero for identity, contact, location, resume, standard work-authorization, demographic-decline, and fixed select mappings. Only genuine free-text questions may use the existing verifier-gated answerer. If the plan is incomplete, the general agent cost is recorded on the fallback route rather than averaged into successful deterministic runs.

#### Reliability

- **Failure Modes:**

  > - Public ATS question schema differs from the rendered form or omits conditional and hidden validation<br>- Raw ID, name, label, or role selectors drift, become duplicated, or point to custom JavaScript controls<br>- Required multi-select, date, education, location, attachment, consent, or conditional fields are unsupported<br>- Verifier-gated free-text answer is unavailable or fails provenance checks<br>- File upload, client-side validation, navigation, popup, iframe, or controlled-component event handling fails<br>- CAPTCHA, login, OTP, anti-spam, or tenant-specific account gate appears<br>- Submit succeeds but confirmation text, URL, network response, or email evidence is absent<br>- Chrome, CDP, page, or worker crashes after the final click and creates an ambiguous result<br>- Headed and headless modes render different form variants or challenge behavior
- **Crash Recovery:**

  > Persist route, adapter version, normalized field schema, plan readiness, unmapped required fields, action index, and a pre-submit checkpoint. Pre-submit crashes may safely restart from discovery in a fresh tab. After the submit action begins, never replay automatically; retain trace and response evidence, mark crash_unconfirmed when needed, and reconcile through network, DOM, and email signals.
- **Exactly Once Submit Safety:**

  > Retain ApplyPilot's effective-target dedup and lease rules. The adapter must expose discovery, plan, fill, pre_submit_check, submit_once, and verify as distinct states. Commit submit_started before clicking, bind the click to one attempt object, disable fallback after a possibly successful click, and quarantine no-confirmation instead of letting the agent submit again.
- **Verification Evidence Tier:**

  > Prefer a captured ATS response with explicit success or application ID, then a known success URL and allowlisted confirmation DOM, then a matched confirmation email. Save a screenshot and trace as supporting evidence. The current Greenhouse marker-based DOM check is a useful minimum but should be strengthened with response and URL evidence; inference from a disabled button is not enough.

#### Applypilot Fit

- **Python Integration:**

  > Excellent. ApplyPilot already uses Python, synchronous Playwright, typed answer plans, HTTPX question discovery, profile loading, resume paths, route metadata, and pytest fixtures. Extend the existing modules and shared AnswerPlan rather than introducing a second automation runtime.
- **Playwright Cdp Reuse:**

  > Excellent for Chromium. The launcher already calls chromium.connect_over_cdp on the worker's port, reuses the first BrowserContext, opens a scratch page, and closes it after Greenhouse or Lever shadow work. Preserve current profile and fixture reuse, while recognizing Playwright's documented lower-fidelity warning for CDP attachment.
- **Control Plane Overhead:**

  > Low and bounded: one question API or DOM discovery pass, one local answer plan, usually 5-25 locator actions, one submit, and one verification pass. No repeated agent observation loop is required. Serialize only normalized fields, action outcomes, and evidence rather than full accessibility trees or screenshots on every step.
- **Observability Joinability:**

  > Strong and already partially implemented. The launcher records adapter_shadow:greenhouse or adapter_submit:greenhouse, adapter_name, adapter_plan_ready, failure_class, application_tool_calls, tool_calls_total, and last_tool. Add adapter_version, schema_digest, unmapped_required_count, action count, confirmation method, trace path, request ID, and email evidence ID to the same apply_result_events join key.

#### Authentication

- **Persistent Profiles:**

  > Strong through the existing dedicated worker Chrome profiles and CDP ports. Keep one process owner and one active submit per profile, use profile locks, and maintain browser-version compatibility. Never automate the user's default Chrome profile; create dedicated candidate profiles for authenticated lanes.
- **Existing Session Attach:**

  > Supported for Chromium via connect_over_cdp. Validate the browser identity, expected context, active account, target origin, and profile owner before action. A scratch tab is appropriate for public forms; an authenticated or owner-controlled tab should be handed off explicitly and not closed unexpectedly.
- **Login And Otp:**

  > Public adapter routes should normally be login-free. When an ATS requires email verification or login, use ApplyPilot's existing inbox relay or owner-supervised profile lane, then resume only if the adapter can prove the same attempt state. Passkeys, authenticators, suspicious-login prompts, CAPTCHA, and uncertain consent always require fallback or supervision.
- **Extension Support:**

  > Compatible with Chromium extensions already loaded in a dedicated profile, although the core adapters require none. Any extension must be narrowly permissioned to the ATS origins and used for owner takeover or approved evidence only; native messaging and broad page-data extraction are unnecessary for standard adapters.
- **Session Data Boundary:**

  > Candidate data stays on the local worker and is sent only through the ATS hosted form. Playwright traces can contain DOM, screenshots, network bodies, resume paths, and answers; retain them locally with redaction and short TTLs. Hosted traces or browsers should be treated as separate deployment modes with explicit privacy review.

#### Quality And Safety

- **Required Field Completeness:**

  > This is the adapter's central gate. Normalize all current fields, evaluate conditional branches, track required status, and refuse submission whenever unmapped_required is non-empty. Extend the current Greenhouse and Lever handling for multi-selects, dates, location IDs, education, consent, and custom controls before claiming full coverage. Recheck after client-side validation.
- **Answer Provenance:**

  > Continue the existing separation: identity and contact data from profile, resume facts from the tailored resume, standard authorization from approved profile fields, demographic selects from an explicit decline policy, and only verifier-approved text from the answerer. Store field-level provenance and capture approved text into the answer corpus only after a verified submit.
- **Unsupported Question Fallback:**

  > Return a structured list of unmapped required labels without touching submit. Reuse the partial plan in the agent or supervised lane so work is not repeated. Sensitive, novel, or unverifiable free text must never be guessed; optionally park the job when no safe answer source exists.
- **Prompt Injection Resistance:**

  > The adapter should use allowlisted ATS origins, fixed code paths, typed field schemas, label classifiers, locator contracts, and no page-directed tool choice. Treat job text and labels as untrusted inputs to mapping and answer generation. The answerer must remain verifier-gated and unable to access credentials, arbitrary network tools, or the submit action.
- **Irreversible Action Guard:**

  > Keep dry_run as the default and retain two independent enablement gates, as the current Greenhouse path does. Require complete plan, route canary approval, pre-submit screenshot or schema digest, durable submit_started checkpoint, one locator click, and positive evidence. Never let the agent fallback run after an ambiguous adapter submit.

#### Operations

- **Concurrency:**

  > One in-flight submit per candidate profile and page, with multiple workers allowed only when they own isolated profiles and queue leases. Set per-ATS and per-tenant caps, begin at one live adapter submit globally, and increase only after duplicate and confirmation metrics remain clean. Discovery and synthetic tests can run at higher concurrency.
- **Warm Session Reuse:**

  > Excellent. Reuse Chrome, profile cookies, browser context, answer corpus, ATS field classifiers, and stable locator templates. Create a fresh page and fresh AnswerPlan per job, re-fetch liveness and current fields, and invalidate cached selectors when schema digest or canary behavior changes.
- **Tracing And Replay:**

  > Use Playwright traces with screenshots, DOM snapshots, sources, and network data for synthetic tests and retained-on-failure canaries. Add action-level structured logs and response capture. Replay against local synthetic ATS fixtures, never against a production submit endpoint; redact resumes, cookies, CSRF tokens, and sensitive answers before retention.
- **Deployment Modes:**

  > Local and self-hosted fleet workers are the best fit and already supported. Dedicated Windows, macOS, Linux, or containerized Chromium workers are possible with matching browser dependencies and profile storage. Managed browsers are optional but change privacy and economics and should be scored separately.
- **Browser Version Matrix:**

  > Playwright supports Chromium, Firefox, and WebKit, but current ApplyPilot profile reuse and adapters are Chromium/CDP paths. Certify pinned Playwright plus Chrome stable on each worker OS. Use native Playwright contexts for cross-browser synthetic tests, but do not claim Firefox or WebKit production support until selectors, uploads, and confirmation behavior pass.
- **Canary Stop Loss:**

  > Follow the approved rollout: shadow fill first, then a very small owned-submit canary. Require at least 20 shadow forms with complete field inventory and 5 live submits at one-concurrent. Stop on any duplicate, false applied state, wrong answer, missing required field, challenge interaction, cross-job selector error, ambiguous result, or secret leak; pause if completion is below 85%, no-confirmation exceeds 10%, or cost exceeds the agent baseline.

#### Benchmark

- **Benchmark Design:**

  > Build synthetic ATS pages for Greenhouse, Lever, Ashby, Workable, SmartRecruiters, and Workday-like flows covering standard fields, multi-selects, conditional questions, consent, uploads, client validation, iframes, redirects, login, CAPTCHA, and delayed confirmation. Run matched live canaries by ATS and form complexity against the current agent control. Use repeated warm and cold runs in headless, headed, and CDP modes, with at least 30 attempts per promoted ATS stratum and 100 total.
- **Route Funnel:**

  > Record host eligible, adapter selected, schema fetched, fields discovered, plan complete, free-text requested, verifier approved, shadow fill passed, submit gate passed, submit clicked, positive response or DOM observed, email matched, fallback, ambiguous quarantine, and final disposition. Report plan-ready coverage separately from success among submitted plans.
- **Step Context Budget:**

  > Target one schema fetch or DOM scan, 5-25 deterministic actions, zero full-page screenshots during normal execution, one evidence capture at submit, and zero model calls for standard forms. Allow one bounded answer call per novel verified question; use fallback rather than an open-ended action loop.
- **Recommendation:**

  > Adopt and prioritize. Productionize the existing Greenhouse adapter through shadow, route-tagged canary, and positive-evidence hardening; build Ashby next using the same AnswerPlan and fail-closed gates; keep Lever deterministic fill-only while hCaptcha is present. Use browser-assisted HTTP only as a certified execution option inside an adapter, preserve agent fallback for incomplete plans, and judge promotion by all-in verified cost rather than raw attempt speed.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `cache_replay_yield`
- `confidence_intervals`
- `deterministic_coverage`
- `escape_rate`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `projected_cost_per_verified_apply`
- `site_permission_and_terms`
- `startup_latency`
- `verification_latency`
- `verified_completion_rate`

### Browser-assisted HTTP handoff with Playwright APIRequestContext

Source record: `Browserassisted_HTTP_handoff_with_Playwright_APIRequestContext.json`

#### Identity

- **Name:** Browser-assisted HTTP handoff with Playwright APIRequestContext
- **Category:** deterministic
- **Official Sources:**

  > - Playwright Python APIRequestContext: https://playwright.dev/python/docs/api/class-apirequestcontext<br>- Playwright Python API testing guide: https://playwright.dev/python/docs/api-testing<br>- Playwright Python Request network evidence: https://playwright.dev/python/docs/api/class-request<br>- Playwright Python network monitoring: https://playwright.dev/python/docs/network<br>- Playwright Python BrowserContext.request: https://playwright.dev/python/docs/api/class-browsercontext<br>- Playwright Python connect_over_cdp: https://playwright.dev/python/docs/api/class-browsertype<br>- Playwright Python releases: https://github.com/microsoft/playwright-python/releases<br>- Playwright Apache-2.0 license: https://github.com/microsoft/playwright/blob/main/LICENSE<br>- ApplyPilot design baseline: C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot/.worktrees/codex-apply-cost-quality-router-phase1/docs/superpowers/specs/2026-07-06-apply-cost-quality-router-design.md
- **Maintenance Status:**

  > Actively maintained. Playwright Python continued regular releases after January 2025 and reached v1.60.0 in May 2026; APIRequestContext remains supported, v1.51 added stricter request failure controls, v1.58 improved local CDP attachment, and v1.60 added API request tracing. ApplyPilot must pin and canary upgrades because browser and network behavior changes with Playwright and Chrome versions.
- **License:**

  > Playwright is Apache-2.0 licensed and can be used from ApplyPilot's AGPL-3.0-only codebase without imposing a conflicting copyleft requirement. ApplyPilot's host-specific request templates remain AGPL code. ATS site terms, privacy duties, and permission to automate a particular submission are independent of the library license.

#### Cost Economics

- **Pricing Model:**

  > Self-hosted Playwright and Python execution. There is no Playwright per-request fee; cost is browser uptime, worker compute, storage for redacted traces, and any optional answer-model call. The target ATS may impose rate or concurrency limits but does not meter Playwright itself.
- **Billing Granularity:**

  > No subscription, token minimum, or browser-hour charge for the local path. Compute is paid at process or machine granularity, so an idle Chrome process still consumes RAM. APIRequestContext defaults to a 30-second timeout, follows up to 20 redirects, and defaults to zero network retries; keep submit retries explicitly disabled.
- **Model Cost:**

  > Zero for form-state extraction, payload construction, and HTTP submission. A missing approved free-text answer may invoke ApplyPilot's bounded verifier-gated answerer, with that cost recorded against the route. A general browser agent should be counted as fallback, not hidden inside this route's model cost.

#### Reliability

- **Failure Modes:**

  > - Browser cookies are shared, but CSRF values in DOM, JavaScript memory, sessionStorage, IndexedDB, or request headers are not automatically synthesized<br>- One-time token, nonce, CAPTCHA, or anti-spam proof expires or is bound to a renderer event or request sequence<br>- Multipart field names, repeated values, attachment metadata, consent fields, or conditional questions are incomplete<br>- Service-worker requests are not observed by page-level interception or differ from APIRequestContext behavior<br>- Endpoint, method, redirect, header, or response schema changes without a public contract<br>- HTTP 2xx carries application-level validation failure, or a redirect loses required origin semantics<br>- Timeout or connection reset occurs after the server may have accepted the irreversible POST<br>- Attached CDP context is missing, locked, stale, or affected by lower-fidelity CDP behavior
- **Crash Recovery:**

  > Persist browser-state acquisition and payload planning before submit. A worker can reopen the page and rebuild state only while submit_started is absent. Once the POST begins, retain the browser context, request digest, response events, and trace; on crash or timeout, mark crash_unconfirmed and reconcile via ATS response, confirmation page, or email rather than replaying.
- **Exactly Once Submit Safety:**

  > Use the same effective-target dedup key as the fleet queue, commit an immutable payload hash and submit_started event, and make the final APIRequestContext.post call single-use. Set max_retries=0, do not convert a failed transport into a second POST, and quarantine any missing response. A captured browser request is evidence of shape, not permission to replay it.
- **Verification Evidence Tier:**

  > Tier 1 is a structured response with explicit success and an ATS application or candidate ID. Tier 2 is an allowlisted success response plus a deterministic status endpoint. Tier 3 is a known confirmation DOM reached in the same browser context, followed by confirmation email as independent evidence. Status 2xx, redirect alone, screenshot alone, and inferred UI disappearance are insufficient.

#### Applypilot Fit

- **Python Integration:**

  > Strong. The Python Playwright API exposes browser_context.request and page.request, multipart uploads, form encoding, response bodies, and storage state. ApplyPilot already uses synchronous Playwright and CDP attachment, so the handoff can live beside the Greenhouse and Lever adapters with typed host contracts.
- **Playwright Cdp Reuse:**

  > Strong for the current architecture. After chromium.connect_over_cdp, use the existing default BrowserContext and its request property so browser and API requests share cookies. Reuse the current profile, CDP port, page fixtures, and route metadata. Account for Playwright's warning that CDP attachment is Chromium-only and lower fidelity than browser_type.connect.
- **Control Plane Overhead:**

  > Low: one browser navigation or existing tab, bounded schema/token extraction, one local payload serialization, one HTTP call, and response parsing. It avoids repeated DOM-agent observations and screenshots. Store only the compact plan, request contract version, payload digest, and evidence, not raw page context.
- **Observability Joinability:**

  > High. Join the queue attempt ID to browser context ID, CDP port, page URL, request-contract version, submit payload digest, network request and response IDs, ATS application ID, trace path, email evidence ID, and final_result_source in apply_result_events. Redact Cookie, Authorization, CSRF values, resumes, and sensitive answers.

#### Authentication

- **Persistent Profiles:**

  > Uses ApplyPilot's existing dedicated automation profiles and profile-lock discipline. Cookies are shared automatically through browser_context.request. Do not clone a live profile during submission, and do not use the user's default Chrome data directory; keep one owner per profile and a browser-version migration policy.
- **Existing Session Attach:**

  > Supported through chromium.connect_over_cdp and the default context. It is suitable for a known local Chrome process and owner-supervised takeover. Check that the expected context, origin, account identity, and active page exist before extracting state; never attach to an unrelated user tab based only on port availability.
- **Login And Otp:**

  > Let the browser and existing inbox or owner-supervised lane complete permitted login or OTP steps. Only hand off after the page is in a stable authorized form state. CAPTCHA, passkey, authenticator, suspicious-login, and manual-consent gates remain explicit escape conditions; APIRequestContext is not a bypass mechanism.
- **Extension Support:**

  > Unchanged from the attached Chromium profile. A narrowly permissioned extension can assist owner takeover or expose an approved status signal, but the HTTP handoff itself needs no extension. Never use a content script to extract unrelated credentials or hidden secrets.
- **Session Data Boundary:**

  > Browser cookies are shared with the associated APIRequestContext and updated in both directions. CSRF headers and hidden fields are extracted locally and sent only to the allowlisted ATS origin. Traces and request objects can contain cookies, resume bytes, and answers, so retain redacted metadata locally and encrypt or discard raw captures after contract development.

#### Quality And Safety

- **Required Field Completeness:**

  > Build the answer plan from the ATS schema or current rendered form, expand conditional branches, enumerate every visible and hidden required field, and compare the serialized request against the plan before submit. Re-run the completeness check after any browser-side validation step; do not assume a captured request from a different job is complete.
- **Answer Provenance:**

  > Each request value must retain profile, resume, approved corpus, user consent, or verified-answer provenance. Browser-derived defaults may describe field structure but cannot establish candidate facts. Consent and demographic choices must come from explicit policy or user-approved profile data.
- **Unsupported Question Fallback:**

  > If a required value, conditional branch, file, consent decision, or token cannot be mapped with high confidence, cancel before submit and hand the still-open page plus compact plan to the deterministic DOM adapter, general agent, or owner. Do not send a partial request to learn from server errors on a real application.
- **Prompt Injection Resistance:**

  > Use fixed host and path allowlists, strict redirect limits, typed payload schemas, maximum body sizes, and explicit extraction selectors. Treat page text, URLs, form actions, and captured headers as untrusted. Never forward cookies or tokens across origins, execute page-supplied code, or let an LLM choose the submit endpoint.
- **Irreversible Action Guard:**

  > Keep discovery and payload validation read-only. Require route eligibility, complete plan, stable origin, approved contract version, and durable submit checkpoint before exposing one final call. Bind the call to one attempt object, disable retries, capture the response atomically, and quarantine ambiguity.

#### Operations

- **Concurrency:**

  > Limit to one in-flight submit per browser profile and candidate identity, plus conservative per-host and per-tenant caps. State acquisition tabs may run concurrently in isolated contexts, but irreversible writes must respect ATS limits and ApplyPilot's dedup lease. Begin canaries at one concurrent submit globally.
- **Warm Session Reuse:**

  > Strong. Reuse the current Chrome process, context cookie jar, connection pool, and certified request template across jobs while isolating pages and attempt state. Revalidate tokens, job ID, required fields, and origin for every submission; never cache one-time tokens or payloads.
- **Tracing And Replay:**

  > Playwright can capture browser actions, DOM snapshots, screenshots, and network activity, and current APIRequestContext exposes tracing. Save a redacted trace for canary failures and a sanitized request-contract fixture. Replay only against synthetic ATS servers; production submit requests are irreversible and must never be automatically replayed from HAR or trace.
- **Deployment Modes:**

  > Best as a local or self-hosted hybrid inside the existing ApplyPilot worker that owns Chrome and the candidate profile. Container deployment is possible with dedicated profiles and matching browsers. A remote managed browser adds session-data transfer and weakens the cost advantage, so it should be evaluated as a different route.
- **Browser Version Matrix:**

  > Playwright supports Chromium, Firefox, and WebKit for native contexts, but ApplyPilot's existing-session reuse uses Chromium CDP only. Pin compatible Playwright and Chrome versions, certify the current stable channel, and run synthetic contract tests before upgrades. Do not assume Firefox or WebKit parity for a contract discovered in Chrome.
- **Canary Stop Loss:**

  > Run at least 20 shadow captures with no submit, then 5 live submissions at one-concurrent. Stop on any wrong recipient, duplicate, missing required answer, cross-origin token leak, unexpected challenge, false applied state, ambiguous timeout, or contract mismatch. Pause if no-confirmation exceeds 10%, verified completion is below 85%, or maintenance-adjusted cost exceeds the deterministic DOM adapter.

#### Benchmark

- **Benchmark Design:**

  > Use matched jobs and candidate fixtures on Greenhouse, Ashby, SmartRecruiters, Workable, Lever, Workday, and long-tail synthetic pages. For each host, compare DOM adapter, APIRequestContext handoff, and agent fallback across headed, headless, warm CDP, and cold browser modes. Include normal forms, conditional questions, attachments, consent, expired jobs, login, CAPTCHA, 429, 5xx, and timeout-after-commit cases. Use at least 30 attempts per promoted host stratum and 100 total.
- **Route Funnel:**

  > Track host eligible, browser attached, form state acquired, contract matched, plan complete, handoff selected, submit checkpoint committed, request sent, response received, explicit success parsed, DOM or email confirmed, fallback, ambiguous quarantine, and final disposition. Report fallback cost against the original route selection.
- **Step Context Budget:**

  > Target one navigation or reused tab, one bounded schema/token extraction, one request contract, one HTTP submit, and at most one confirmation check. Standard forms should use 0 screenshots and 0 model turns; unsupported free text permits one bounded verified-answer call before fallback.
- **Recommendation:**

  > Canary as an internal execution mode of ATS-specific adapters, not as a general network-replay route. Start after the deterministic plan is complete, on Greenhouse or Ashby synthetic and shadow traffic, with host allowlists, cookie-bound context.request, explicit token extraction, no POST retries, positive evidence, and crash_unconfirmed quarantine. Prefer the ordinary deterministic DOM adapter when the endpoint contract or permission is unclear.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `cache_replay_yield`
- `confidence_intervals`
- `deterministic_coverage`
- `escape_rate`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `projected_cost_per_verified_apply`
- `site_permission_and_terms`
- `startup_latency`
- `verification_latency`
- `verified_completion_rate`

### Direct ATS HTTP or supported API submission

Source record: `Direct_ATS_HTTP_or_supported_API_submission.json`

#### Identity

- **Name:** Direct ATS HTTP or supported API submission
- **Category:** deterministic
- **Official Sources:**

  > - Greenhouse Job Board API: https://developers.greenhouse.io/job-board.html<br>- Lever Postings API: https://github.com/lever/postings-api<br>- Ashby applicationForm.submit: https://developers.ashbyhq.com/reference/applicationformsubmit<br>- Ashby API authentication: https://developers.ashbyhq.com/reference/authentication<br>- SmartRecruiters Application API: https://developers.smartrecruiters.com/docs/application-api<br>- Workable create-candidate endpoint: https://workable.readme.io/reference/job-candidates-create<br>- HTTPX repository and license: https://github.com/encode/httpx<br>- ApplyPilot design baseline: C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot/.worktrees/codex-apply-cost-quality-router-phase1/docs/superpowers/specs/2026-07-06-apply-cost-quality-router-design.md
- **Maintenance Status:**

  > The route is not a separate product. Its contracts are controlled by each ATS. The cited Greenhouse, Ashby, SmartRecruiters, and Workable documentation was active and materially updated or current after January 2025; Lever's official Postings API repository remains the canonical contract but publishes documentation rather than versioned releases. Every adapter therefore needs contract tests and a vendor-change watch.
- **License:**

  > HTTP is not licensed. ApplyPilot can implement this route in its AGPL-3.0-only codebase with its existing HTTPX dependency, which is BSD-3-Clause licensed. ATS documentation and API access terms remain separate contractual constraints; an open-source client license does not grant use of a tenant's write API.

#### Cost Economics

- **Pricing Model:**

  > Self-hosted request execution with no per-call ApplyPilot vendor fee. ATS submission endpoints are generally included in a customer's or partner's ATS account, but access requires tenant-issued API credentials or OAuth rather than applicant payment. Any employer or marketplace commercial agreement is outside this route's compute cost.
- **Billing Granularity:**

  > No browser-hour or token minimum. Costs accrue per HTTP request and worker uptime. Vendor constraints matter more than billing: Lever documents a 2 application-POSTs-per-second limit and 429 handling; SmartRecruiters documents an 8-concurrent-request ceiling and adaptive throttling. Other limits are tenant or vendor specific.
- **Model Cost:**

  > The deterministic core uses no model and therefore has $0 model cost. If a verified free-text answer is absent, the route must either invoke ApplyPilot's cheap verifier-gated answerer and record that cost or fall back; it must never invent an answer merely to preserve a no-model claim.

#### Reliability

- **Failure Modes:**

  > - Missing or unauthorized employer API key, OAuth grant, scope, or partner entitlement<br>- Job unpublished, expired, moved, or not present on the supplied board or tenant<br>- Incomplete required questions, conditional fields, privacy consent, or data-compliance fields<br>- Attachment format, size, upload-handle, or multipart encoding failure<br>- 429 throttling, transient 5xx responses, timeout after the server may have committed, or contract drift<br>- Successful transport response without a durable application, especially when a vendor returns validation details in the response body<br>- Duplicate candidate merging that is not equivalent to exactly-once application creation
- **Crash Recovery:**

  > Persist an append-only state machine before the write: eligible, schema_fetched, plan_complete, submit_started, response_received, verified, or ambiguous. GET operations may be retried. A submission POST must not be replayed automatically after a timeout or lost response; record the request digest and quarantine for status API, confirmation email, or operator reconciliation.
- **Exactly Once Submit Safety:**

  > Require ApplyPilot's effective-target dedup key, a pre-submit checkpoint, one immutable payload hash, and one submit_started event committed before network I/O. Use a vendor idempotency key only when explicitly documented. Lever's email-based candidate dedup is not an idempotency guarantee. Any indeterminate POST becomes crash_unconfirmed and blocks sibling retries until reconciled.
- **Verification Evidence Tier:**

  > Highest tier is a documented success body containing an application identifier, such as Lever applicationId or SmartRecruiters applicationId, or an explicit success boolean plus no validation errors, as required by Ashby. Next is a status endpoint tied to that identifier. A confirmation email is independent secondary evidence. A 2xx status by itself is insufficient, and Greenhouse needs extra confirmation because its API does not validate every job-specific required field.
- **Headless Headed Parity:**

  > Not applicable to the direct route because no browser is used. Any browser fallback is a different route and must be measured independently rather than blended into direct-HTTP success.

#### Applypilot Fit

- **Python Integration:**

  > Excellent mechanically. ApplyPilot already depends on HTTPX and already fetches Greenhouse public questions with it. Add typed per-ATS clients, strict response schemas, multipart helpers, redacted logging, and queue result metadata without introducing a new runtime language.
- **Playwright Cdp Reuse:**

  > None is required for a supported API. Existing Playwright and CDP infrastructure remains the fallback and verification surface. Browser cookies do not substitute for employer API credentials, so attaching to an applicant session must not be treated as authorization for a documented customer API.
- **Ats Fit:**

  > Greenhouse Job Board POST requires a customer Job Board API key; Lever POST requires an API key generated by a Super Admin; Ashby applicationForm.submit requires candidatesWrite; SmartRecruiters Application API requires X-SmartToken or OAuth scope candidate_applications_manage; Workable candidate creation requires an account bearer token and candidate-write scope. These are technically strong only for authorized employer or partner integrations. No supported public candidate-write API was verified for Workday, and long-tail hosts require individual review.
- **Control Plane Overhead:**

  > Very low: generally one schema/configuration GET, local plan construction, one multipart or JSON POST, and optional status GET. Serialize only structured fields, payload digest, response status, vendor identifiers, and redacted error details; no DOM or screenshot context is needed.
- **Observability Joinability:**

  > High if every request carries the ApplyPilot attempt ID in local metadata and records ATS, tenant, posting ID, payload digest, response status, application ID, latency, and reconciliation source into apply_result_events. Secret headers and resume bytes must be excluded from logs.

#### Authentication

- **Persistent Profiles:**

  > Not used. API credentials belong in a scoped secret store with rotation and ownership metadata, not in Chrome profiles. A profile cannot safely or legitimately replace a tenant API key.
- **Existing Session Attach:**

  > Not applicable to supported customer APIs. If the only available authorization is an applicant browser session, classify the experiment as browser-assisted HTTP rather than direct supported API submission.
- **Login And Otp:**

  > Unsupported by design. Candidate login, email verification, OTP, passkey, CAPTCHA, and account recovery remain browser or owner-supervised flows. The API route must fail closed instead of attempting to convert those controls into hidden HTTP calls.
- **Extension Support:**

  > No extension is required. Adding an extension would increase permissions and data exposure without helping a documented server-to-server API; keep the route in the Python worker.
- **Session Data Boundary:**

  > Requests send candidate profile fields, answers, consent choices, and attachments directly from ApplyPilot to the named ATS endpoint. Tenant credentials remain local and encrypted. Logs retain only redacted structured evidence and cryptographic digests; never persist Authorization headers, raw resumes, or full sensitive response bodies in traces.

#### Quality And Safety

- **Required Field Completeness:**

  > Fetch the documented job-specific schema immediately before planning, expand conditional questions, validate all required and consent fields locally, and compare the final payload against that schema. This is mandatory for Greenhouse because it explicitly states that the submission endpoint will not reject every missing job-specific required field.
- **Answer Provenance:**

  > Every value must be tagged internally as profile, resume, approved-answer corpus, explicit user consent, or verifier-approved generated answer. Do not pass ATS-provided labels or descriptions as authority for candidate facts, and do not silently default privacy consent.
- **Unsupported Question Fallback:**

  > If any required, conditional, sensitive, or free-text field lacks an approved answer, do not POST. Route to a deterministic browser adapter that can present the question, a verified answerer, supervised owner review, or a durable unsupported disposition.
- **Prompt Injection Resistance:**

  > The route should use allowlisted ATS hosts, fixed endpoint templates, typed schemas, strict field-name mapping, response size limits, and no execution of page text. Treat job descriptions and question labels as untrusted data. The HTTP client must never expose secrets to redirects outside the allowlisted origin.
- **Irreversible Action Guard:**

  > Separate plan from submit. A route policy and complete-plan assertion must pass before a single submit function is callable; dry-run must be the default. Record submit_started durably, send once, require structured positive evidence, and quarantine every ambiguous result rather than retrying.

#### Operations

- **Concurrency:**

  > Use per-tenant limiters rather than global fleet concurrency. Enforce Lever's documented maximum of 2 application POSTs per second and SmartRecruiters' documented 8 concurrent calls, honor Retry-After, and set lower canary caps. Never retry a possibly committed POST solely because of 429 or a transport timeout without proving the server rejected it.
- **Warm Session Reuse:**

  > Reuse a bounded HTTPX connection pool per ATS origin, but isolate tenant credentials and rate limiters. Cache public question schemas only briefly and revalidate posting liveness and required fields before the irreversible POST.
- **Tracing And Replay:**

  > Capture timing, DNS/connect errors, status, selected response headers, redacted response schema, payload digest, and vendor identifiers. GETs can be replayed in fixtures. Production submission POSTs must never be included in automatic HAR replay; use synthetic ATS fixtures for deterministic replay tests.
- **Deployment Modes:**

  > Local or self-hosted Python worker is preferred. A managed relay is justified only for an employer-authorized integration and must keep credentials regionally scoped. Do not send candidate data through an unrelated hosted browser or proxy merely to save local compute.
- **Browser Version Matrix:**

  > Not applicable to the direct route. Validate HTTP behavior across supported Python and HTTPX versions, TLS stacks, ATS global/EU endpoints, and multipart encoders. Browser compatibility belongs to fallback routes.
- **Canary Stop Loss:**

  > Begin only with a tenant-owned test credential and synthetic candidates, then at most 5 live applications. Stop immediately on any duplicate, missing required answer, consent mismatch, application without positive evidence, secret leakage, unexpected redirect, or ambiguous timeout. Also stop if verified completion is below 90% or maintenance-adjusted cost exceeds the deterministic browser adapter.

#### Benchmark

- **Benchmark Design:**

  > Create synthetic contracts for Greenhouse, Lever, Ashby, SmartRecruiters, and Workable, then run live tests only where credentials and permission exist. For each ATS, use at least 30 varied forms covering required text, selects, conditional questions, consent, and attachments; compare direct API with the same jobs through the hosted form. Keep unauthorized public jobs in the eligibility denominator so coverage is not overstated.
- **Route Funnel:**

  > Track discovered jobs, documented write endpoint found, authorization available, schema fetched, plan complete, submit started, response received, application ID or explicit success received, delayed evidence matched, fallback, ambiguous quarantine, and final disposition. Report both eligible-route success and fleet-wide coverage.
- **Step Context Budget:**

  > Target 1-3 HTTP requests, 0 browser actions, 0 DOM or screenshot tokens, and 0 model calls for standard forms. Permit one bounded verified-answer call only when provenance checks pass; otherwise escape before submission.
- **Recommendation:**

  > Reject as a general public-applicant route. Adopt only as an explicit tenant-authorized or posting-instructed integration with documented credentials, complete schema validation, positive response evidence, and exactly-once quarantine. Keep it in the route registry as a high-priority eligibility check because its marginal cost is excellent when legitimately available, but never probe write endpoints speculatively.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `cache_replay_yield`
- `confidence_intervals`
- `deterministic_coverage`
- `escape_rate`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `projected_cost_per_verified_apply`
- `site_permission_and_terms`
- `startup_latency`
- `verification_latency`
- `verified_completion_rate`

### Puppeteer

Source record: `Puppeteer.json`

#### Identity

- **Name:** Puppeteer
- **Category:** driver
- **Official Sources:**

  > - https://pptr.dev/guides/what-is-puppeteer<br>- https://pptr.dev/guides/installation<br>- https://pptr.dev/api/puppeteer.connectoptions<br>- https://pptr.dev/api/puppeteer.launchoptions<br>- https://pptr.dev/webdriver-bidi<br>- https://pptr.dev/guides/chrome-extensions<br>- https://github.com/puppeteer/puppeteer/releases
- **Maintenance Status:**

  > Active, with material maintenance since January 2025. Official documentation displayed Puppeteer 25.3.0 in July 2026, and the official repository published multiple 2026 releases while rolling current Chrome and Firefox versions.
- **License:**

  > Apache License 2.0. It is compatible for use as a dependency of ApplyPilot's AGPL-3.0 codebase when Apache notices and attribution requirements are retained. Puppeteer's license does not change the AGPL obligations for ApplyPilot modifications or distribution.

#### Cost Economics

- **Pricing Model:**

  > No Puppeteer license fee. The full package downloads managed Chrome for Testing binaries; puppeteer-core uses a browser supplied by ApplyPilot. Cost is local or cloud browser compute, artifact storage, and engineering. Any remote browser provider has separate session and concurrency pricing.
- **Billing Granularity:**

  > Local puppeteer-core has no billing minimum. Allocate process and browser runtime by second or attempt. The full puppeteer package adds a browser download and cache footprint, not a per-run charge. Remote browser services may round session time and separately bill concurrency, storage, proxy traffic, or egress.
- **Model Cost:**

  > Zero for deterministic Puppeteer scripts. A semantic planner, vision model, or answer generator layered on top is separate and must be attributed to the same ApplyPilot attempt and route.
- **Infrastructure Cost:**

  > Requires Node.js, Chrome or Firefox, browser dependencies, RAM, CPU, and trace or screenshot storage. The official package downloads sizable Chrome for Testing artifacts; puppeteer-core avoids that when ApplyPilot manages browsers. The official container runs Chrome in sandbox mode and documents additional container capabilities and process-init requirements.

#### Reliability

- **Failure Modes:**

  > Target closed or crashed; browser disconnect; CDP or BiDi protocol timeout; stale element handle; frame or navigation race; request-interception deadlock or double handling; unsupported BiDi operation; browser mismatch when using an unmanaged executable; profile lock; extension or headless difference; login or challenge wall; unsupported ATS field; and submit without confirmation.
- **Crash Recovery:**

  > Puppeteer emits browser-disconnected and target lifecycle events and can attach to an existing browser through browserWSEndpoint or browserURL if the browser survives. It does not persist workflow checkpoints or resume an application transaction. ApplyPilot must checkpoint before submit, log target and request IDs, reconnect only for evidence collection, and quarantine any post-submit ambiguity.
- **Exactly Once Submit Safety:**

  > Not built in. Add ApplyPilot pre-submit fingerprints, an irreversible-action gate, one submit command, network-response capture, and ATS idempotency keys where available. Never retry after a target close, protocol timeout, or browser disconnect if submit may have been sent; use crash_unconfirmed and independent reconciliation.
- **Verification Evidence Tier:**

  > Strong on Chrome/CDP: raw response and request events, response body where available, known confirmation DOM and URL, console events, screenshots, and direct CDPSession commands. Firefox/BiDi support can throw UnsupportedOperation for missing features. Email evidence remains outside Puppeteer.
- **Verification Latency:**

  > Network, DOM, URL, and screenshot evidence can be collected synchronously within seconds. Delayed confirmation email should use ApplyPilot's existing reconciler. A navigation success or fulfilled click promise alone is not positive application evidence.

#### Applypilot Fit

- **Python Integration:**

  > Puppeteer is a JavaScript/TypeScript library with no official native Python API. ApplyPilot must add a supervised Node subprocess or sidecar with typed RPC, cancellation, backpressure, log routing, and structured results. Community Python ports are separate projects and are not evidence for Puppeteer's own maintenance or API compatibility.
- **Playwright Cdp Reuse:**

  > Moderate at the browser boundary and low at the object boundary. Puppeteer can connect to an existing Chrome browser by WebSocket or HTTP endpoint and can use a managed userDataDir, so Chrome binaries, debug ports, and dedicated profiles can be reused. Playwright BrowserContext, Page, locator, and fixture objects cannot be shared. Concurrent CDP clients can interfere, and Chrome 136+ requires a non-default user-data-dir for remote-debugging switches on regular Chrome.
- **Migration Effort:**

  > Medium-to-large. Build the Python-to-Node control contract, port Playwright selectors, waits, uploads, downloads, evidence capture, and tests, add browser lifecycle and profile ownership, and validate all ATS routes. A narrow benchmark sidecar is moderate effort; replacing the current Python Playwright path is large effort with limited functional gain.
- **Ats Fit:**

  > Good for deterministic automation of known Ashby, Greenhouse, Lever, Workable, Workday, and long-tail variants, especially on Chrome. Puppeteer offers form input, locators, frame control, file upload, network interception, screenshots, and raw CDP sessions. It does not supply ATS semantics, truthful-answer policy, or Workday tenant normalization.
- **Control Plane Overhead:**

  > Low within a Node process: high-level calls become CDP JSON messages over a pipe or WebSocket; Chrome uses CDP by default and Firefox uses BiDi. It is higher for ApplyPilot because every command crosses Python-to-Node RPC. This should still be much smaller than repeatedly serializing DOM state through an MCP and general model, but must be measured rather than assumed.
- **Observability Joinability:**

  > Strong with explicit instrumentation. Puppeteer exposes browser, target, page, request, response, console, and disconnect events, screenshots, performance tracing, and raw CDPSession access. Carry ApplyPilot attempt_id, queue row, worker, route, browser process, context, target, loader, request, and confirmation identifiers into append-only result metadata.

#### Authentication

- **Persistent Profiles:**

  > Supported with launch userDataDir and the default browser context. Separate BrowserContexts isolate cookies and local storage, but Chrome non-default contexts are incognito-like and do not replace a persistent authenticated profile. Give each profile one browser owner, pin compatible browser versions, clone only while quiescent, and encrypt stored credentials.
- **Existing Session Attach:**

  > Strong for Chrome when a browser exposes browserWSEndpoint or browserURL; Puppeteer.connect attaches and Browser.disconnect detaches without necessarily closing it. ConnectOptions can also discover an endpoint for a Chrome channel in newer versions, though that path is experimental. Use dedicated debug profiles and loopback endpoints, not the user's default profile.
- **Login And Otp:**

  > Supports headful browsing, screenshots, event waits, and detaching for owner takeover, but has no identity or OTP service. ApplyPilot must route login gates to trusted owner profiles, use the approved inbox relay for email OTP, and park CAPTCHA or account-choice flows instead of attempting bypass or blind retry.
- **Extension Support:**

  > Strong and documented for Chrome. Puppeteer can load unpacked extensions at launch, install and list them at runtime, access extension service workers and content-script realms, and trigger extension actions. Some extension APIs are CDP-specific and are listed as unsupported over WebDriver BiDi. Keep permissions narrow and native-messaging secrets outside page reach.
- **Session Data Boundary:**

  > Local puppeteer-core keeps cookies, credentials, resumes, network data, screenshots, and traces on the worker and ApplyPilot-controlled storage. Connecting to a remote browser transfers commands and potentially sensitive evidence to that endpoint and provider. Bind local debug endpoints to loopback, authenticate remote transport, redact secrets, and define retention before cloud use.

#### Quality And Safety

- **Required Field Completeness:**

  > No native guarantee. Inventory controls before filling, resolve frames and shadow roots, map every required field, re-scan after conditional questions, and fail closed when any required value or source is absent. Locator success is not equivalent to application-plan completeness.
- **Answer Provenance:**

  > Not provided by Puppeteer. Attach every filled value to a profile field, resume span, approved-answer ID, or deterministic transform and persist that mapping. Reject generated claims that are not supported by approved candidate data.
- **Unsupported Question Fallback:**

  > Stop before submit with a structured payload containing label, control type, choices, required status, current plan, DOM locator, and screenshot. Consult approved answers, a bounded helper, or a human. Never pick a default or fabricate a response merely because Puppeteer can manipulate the control.
- **Prompt Injection Resistance:**

  > Deterministic Puppeteer code has no prompt surface. If a model is added, treat page text, accessibility data, console output, and network content as hostile. Newer ConnectOptions include experimental Chrome URL allowlist and blocklist guardrails, but the official docs say they are not a complete network sandbox; combine them with OS or container isolation, secret separation, restricted commands, and an external submit gate.
- **Irreversible Action Guard:**

  > Use a two-phase ApplyPilot API: prepare and validate the complete plan, persist a pre-submit checkpoint and evidence, then expose one narrowly scoped submit operation. Model-assisted code must not receive unrestricted page.click, evaluate, CDPSession.send, or request-interception access to the submit boundary.

#### Operations

- **Warm Session Reuse:**

  > Supported by keeping Browser and BrowserContext objects alive or reconnecting to a browser endpoint. Reset pages, dialogs, downloads, permissions, request handlers, storage policy, and attempt IDs between jobs. Do not share one authenticated profile across simultaneous applications without explicit ATS and exactly-once proof.
- **Tracing And Replay:**

  > Puppeteer can capture Chrome performance traces, optionally with screenshots, and exposes network, console, target, and page events plus screenshots and PDFs. Only one Puppeteer trace can run at a time per browser. It has no built-in application action replay or transaction resume; ApplyPilot must create its own action journal and never replay submit.
- **Deployment Modes:**

  > Local Windows, macOS, or Linux; self-hosted containers using the official image or a custom image; remote Chrome endpoints; and managed browser providers. The lowest integration and data cost would be a local Node sidecar next to the Python worker, but that still duplicates Playwright-era lifecycle code.
- **Browser Version Matrix:**

  > Puppeteer controls Chrome over CDP by default and Firefox over WebDriver BiDi by default. Chrome can also use BiDi, but official documentation lists unsupported Puppeteer features over BiDi and throws UnsupportedOperation for gaps. Puppeteer is guaranteed against its managed browser build; arbitrary executablePath versions are explicitly at the user's risk. It does not provide WebKit or Safari automation.
- **Canary Stop Loss:**

  > ApplyPilot must enforce host allowlists, per-ATS daily caps, one-profile concurrency, maximum protocol calls, elapsed time and bytes, zero post-submit automatic retries, maximum fallback/model spend, and circuit breakers for disconnects, target crashes, login gates, unsupported operations, ambiguous confirmation, and completion-rate regression.

#### Benchmark

- **Benchmark Design:**

  > Compare Puppeteer against the current Playwright route on matched, randomized ATS and host strata with the same answers and profiles. Include iframes, uploads, conditional fields, custom widgets, redirects, auth gates, delayed confirmations, and synthetic hostile pages. Pin browser versions and repeat headed, headless, cold, warm, and attach modes.
- **Route Funnel:**

  > Record eligible, selected_puppeteer, node_ready, browser_connected, profile_ready, form_inventory_complete, plan_complete, submit_armed, submit_sent, synchronous_verified, delayed_verified, escaped, pre_submit_retry, ambiguous_quarantined, and final disposition. Join by ATS, host, browser, protocol, version, worker, and profile mode.
- **Step Context Budget:**

  > Deterministic Puppeteer should consume zero model tokens. Track Python RPC calls and bytes, Puppeteer API calls, CDP or BiDi messages and bytes, DOM or accessibility extraction, screenshots, response bodies, elapsed time, and fallback model tokens separately. Store large artifacts by content-addressed pointer.
- **Recommendation:**

  > CANARY AS A COMPARATOR, not a migration target. Puppeteer is mature, actively maintained, observable, and strong for Chrome attachment, but ApplyPilot already owns Python Playwright/CDP assets and would pay a Node integration and fixture-porting cost for similar capabilities. Keep it as a small benchmark to test whether control-plane simplicity materially reduces runtime failures or latency; do not replace the current route without measured all-in and verification gains.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `cache_replay_yield`
- `concurrency`
- `confidence_intervals`
- `deterministic_coverage`
- `escape_rate`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `marginal_cost_per_attempt`
- `projected_cost_per_verified_apply`
- `site_permission_and_terms`
- `startup_latency`
- `verified_completion_rate`

### Raw Chrome DevTools Protocol

Source record: `Raw_Chrome_DevTools_Protocol.json`

#### Identity

- **Name:** Raw Chrome DevTools Protocol
- **Category:** driver
- **Official Sources:**

  > - https://chromedevtools.github.io/devtools-protocol/<br>- https://chromedevtools.github.io/devtools-protocol/tot/Target/<br>- https://chromedevtools.github.io/devtools-protocol/tot/Network/<br>- https://chromedevtools.github.io/devtools-protocol/tot/DOM/<br>- https://chromedevtools.github.io/devtools-protocol/tot/Input/<br>- https://developer.chrome.com/blog/remote-debugging-port<br>- https://github.com/ChromeDevTools/devtools-protocol
- **Maintenance Status:**

  > Active, with material maintenance since January 2025. CDP is maintained with Chromium and Chrome DevTools, the generated devtools-protocol repository was updated in May 2026, and generated protocol definitions show 2026 Chromium updates. Tip-of-tree is intentionally fast-moving rather than a stable compatibility contract.
- **License:**

  > The generated ChromeDevTools/devtools-protocol repository is BSD-3-Clause, and the canonical Chromium protocol definitions carry a BSD-style license. This is compatible with ApplyPilot's AGPL-3.0 codebase when notices are retained. Chrome browser distribution terms remain separate from the protocol-definition license.

#### Cost Economics

- **Pricing Model:**

  > No CDP protocol fee. Raw CDP is self-hosted browser control; cash cost is Chrome or Chromium compute, storage for captured evidence, and substantial client-engineering and maintenance. A remote Chrome provider, proxy, or VM introduces separate runtime, concurrency, and egress pricing.
- **Billing Granularity:**

  > Local CDP has no billing minimum; measure browser process time, CPU, RAM, and artifact bytes by attempt. A persistent idle browser consumes memory but no protocol fee. Remote execution may round VM or browser-session time and charge storage, proxy traffic, and egress.
- **Model Cost:**

  > Zero for deterministic raw-CDP commands. Any LLM that chooses commands, interprets the DOM, or answers questions has separate token cost and a much larger safety surface; attribute that usage to the same attempt and route.
- **Infrastructure Cost:**

  > Requires Chrome, Chromium, Edge, or another compatible Blink browser, a secured WebSocket or pipe transport, a client process, and storage for network, DOM, log, screenshot, or trace evidence. No GPU is normally required. Client-side buffering and comprehensive protocol capture can materially increase memory and storage.

#### Reliability

- **Failure Modes:**

  > Tip-of-tree command or event change with no backward-compatibility guarantee; unsupported stable-1.3 capability; command ID timeout; wrong target or session ID; detached target; renderer crash; frame, execution-context, or node lifetime race; stale backendNodeId or objectId; missed event subscription; input coordinate error; dialog stall; request interception deadlock; WebSocket disconnect; exposed debug port; profile lock; login or challenge wall; and submit without confirmation.
- **Crash Recovery:**

  > CDP provides targetCrashed, targetCreated, targetDestroyed, attached, detached, and browser/version events, but recovery is entirely client policy. Persist target, frame, loader, request, and application checkpoints; reconnect to a surviving browser only to reconcile state; recreate crashed targets before submit; and quarantine any disconnect after submit may have been dispatched.
- **Exactly Once Submit Safety:**

  > Not provided by CDP. Raw Input.dispatchMouseEvent or Runtime.evaluate can issue duplicate irreversible actions unless ApplyPilot wraps them. Require a persisted pre-submit fingerprint, one scoped submit primitive, request and response correlation, ATS idempotency where available, and crash_unconfirmed quarantine for every ambiguous post-submit failure.
- **Verification Evidence Tier:**

  > Potentially strongest synchronous evidence: Network request and response metadata and bodies, Fetch interception, DOM and Accessibility state, Page lifecycle and URL, Runtime values, browser logs, screenshots, and traces. The low-level client must correlate them correctly. Confirmation email remains a separate delayed source.
- **Verification Latency:**

  > CDP events arrive bidirectionally during the session, so network and DOM confirmation can be captured within seconds. Event ordering, out-of-process frames, redirects, and target changes require careful correlation. Delayed email stays asynchronous; no matching synchronous evidence means ambiguous rather than applied.

#### Applypilot Fit

- **Python Integration:**

  > Protocol messages are JSON over WebSocket or pipe, so Python can implement CDP directly with an async WebSocket library and generated or hand-written types. There is no official high-level raw-Python client supplied by the protocol project. ApplyPilot would own concurrency, event dispatch, cancellation, schema/version negotiation, and all browser abstractions.
- **Playwright Cdp Reuse:**

  > Good at the browser endpoint and poor at the API-object layer. Raw CDP can connect to ApplyPilot's existing remote-debugging port, query /json/version and /json/list, and use the same dedicated browser profile. It cannot reuse Playwright BrowserContext, Page, locator, or test-fixture objects. Multiple CDP clients can change target state and must coordinate ownership; Chrome 136+ rejects remote-debugging switches on the default data directory unless a non-standard user-data-dir is supplied.
- **Migration Effort:**

  > Large. A production raw client must recreate capabilities already supplied by Playwright: robust locators and auto-waiting, frame and target management, uploads, downloads, dialogs, context isolation, retries, traces, browser installation, and cross-version testing. A narrow instrumentation probe is small-to-medium; replacing the ApplyPilot driver is large and high risk.
- **Ats Fit:**

  > Technically capable for Chromium-based Ashby, Greenhouse, Lever, Workable, Workday, and long-tail pages through DOM, Runtime, Input, Network, Fetch, Page, Target, Storage, and Accessibility domains. Suitability depends entirely on ApplyPilot adapters. Raw CDP supplies no ATS semantics, truthful-answer policy, resilient locator layer, or non-Chromium coverage.
- **Control Plane Overhead:**

  > Lowest possible wire abstraction among the three comparators: compact JSON command, result, and event messages over one browser WebSocket or pipe, with no mandatory driver server, WebdriverIO layer, or Puppeteer object mapping. However, an ApplyPilot client must process high-volume events and may make extra round trips that mature libraries coalesce or hide. Benchmark bytes, calls, and latency, not just dependency count.
- **Observability Joinability:**

  > Maximum raw visibility but all joins are manual. CDP exposes target, session, frame, loader, execution-context, request, interception, trace, and browser identifiers. Add ApplyPilot attempt_id, queue row, route, worker, profile, and confirmation ID to a structured event journal and store large payloads by content-addressed pointer with secret redaction.

#### Authentication

- **Persistent Profiles:**

  > Launch Chrome with a dedicated --user-data-dir and optional profile directory, then discover its endpoint. One browser process should own each profile; clone only while Chrome is stopped and pin compatible browser versions. Chrome 136+ deliberately prevents remote-debugging switches from controlling the default data directory, so existing authenticated state must live in a managed non-default profile or a carefully copied profile.
- **Existing Session Attach:**

  > Strong for a browser deliberately launched with remote debugging. /json/version exposes the browser WebSocket and /json/list exposes page targets; Target.attachToTarget and flat sessions support control. Do not expose the endpoint beyond loopback or attach to the user's default profile. Attaching to a regular browser without a debug endpoint is not supported.
- **Login And Otp:**

  > CDP can run headed, observe dialogs and network, take screenshots, and pause for owner input, but it has no identity or OTP service. Route login gates to trusted owner profiles, use ApplyPilot's approved inbox relay for email OTP, and park CAPTCHA or account-decision flows without bypass attempts.
- **Extension Support:**

  > Chrome can load extensions through launch arguments, and a narrowly permissioned extension can itself use chrome.debugger as an alternate CDP transport after declaring the powerful debugger permission. Raw CDP does not manage extension packaging or native messaging. Treat debugger permission and remote-debug endpoints as privileged capabilities and isolate secrets.
- **Session Data Boundary:**

  > A loopback raw-CDP client can keep cookies, credentials, resumes, screenshots, traces, and response bodies on the worker. CDP exposes highly sensitive cookie, network, DOM, and runtime capabilities, so never bind the debug port publicly. Authenticate and encrypt any remote tunnel, redact secrets, restrict filesystem artifacts, and define retention before remote use.

#### Quality And Safety

- **Required Field Completeness:**

  > No high-level guarantee and no auto-waiting. The client must traverse frames and shadow roots, inventory all visible and conditionally revealed controls, identify required semantics, map every field, and fail closed on missing values. Direct DOM mutation should not be treated as equivalent to user-valid input unless the ATS behavior is verified.
- **Answer Provenance:**

  > Not provided. Carry a profile field, resume span, approved-answer ID, or deterministic transform with every value before any CDP input command. Runtime.evaluate must not manufacture candidate claims or bypass the answer-verification layer.
- **Unsupported Question Fallback:**

  > Stop before submit and return label, required state, options, node and frame identity, DOM or accessibility snapshot, screenshot, and partial plan. Use an approved-answer lookup, constrained helper, or human review. Never use arbitrary Runtime.evaluate to suppress validation or silently remove a required field.
- **Prompt Injection Resistance:**

  > Deterministic CDP has no prompt surface. If a model chooses raw commands, the capability is extremely broad: Runtime, Network, Storage, Browser, and Target can expose secrets or execute page code. Enforce an origin allowlist at network and OS layers, a small command allowlist, schema-validated parameters, isolated credentials, hostile-page tests, and an external irreversible-action gate.
- **Irreversible Action Guard:**

  > Expose no generic Input or Runtime method to the submit decision. Build a two-phase ApplyPilot command that validates and persists the complete plan, arms one known submit target or request, records pre-submit evidence, sends it once, and immediately transitions to evidence-only reconciliation.

#### Operations

- **Warm Session Reuse:**

  > Strong. Keep the browser WebSocket open, create and dispose BrowserContexts and targets, or reconnect through the browser endpoint. Reset event subscriptions, targets, dialogs, downloads, permissions, storage policy, and attempt IDs. Never share one authenticated profile across concurrent applications without proven isolation and exactly-once controls.
- **Tracing And Replay:**

  > Raw Tracing, Network, Runtime, Log, Page screenshot or screencast, DOMSnapshot, and Accessibility domains can produce rich evidence. CDP does not turn those events into a reliable application replay system. ApplyPilot must journal high-level actions and state transitions, version protocol schemas, and explicitly exclude final submit from replay.
- **Deployment Modes:**

  > Local Chrome on Windows, macOS, or Linux; self-hosted containers; remote Chrome over a secured tunnel; and managed CDP-compatible browsers. There is no protocol-managed deployment plane. Prefer loopback or pipe transport beside the Python worker and isolate each persistent owner profile.
- **Browser Version Matrix:**

  > CDP targets Chrome, Chromium, Edge, and other Blink-based browsers. Tip-of-tree changes frequently with no backward-compatibility guarantee; stable protocol 1.3 is frozen at Chrome 64 and is only a subset. Query /json/protocol and Browser.getVersion, pin Chrome channels, generate types per supported range, and maintain compatibility tests. Firefox, Safari, and WebKit are not CDP targets.
- **Canary Stop Loss:**

  > ApplyPilot must enforce an ATS and origin allowlist, allowed CDP domain and method list, per-route daily cap, one-profile ownership, maximum commands, event bytes, time and spend, zero automatic post-submit retry, and circuit breakers for target crashes, disconnects, command-version errors, login gates, ambiguous confirmation, and verified-completion regression.

#### Benchmark

- **Benchmark Design:**

  > Compare a minimal raw-CDP adapter with current Playwright on identical, randomized ATS and host strata. Include frame and target churn, shadow DOM, uploads, downloads, custom widgets, redirects, auth gates, delayed confirmation, browser crashes, and hostile pages. Pin Chrome versions and repeat cold, warm, headed, headless, and multi-context runs.
- **Route Funnel:**

  > Record eligible, selected_raw_cdp, endpoint_ready, websocket_connected, target_attached, profile_ready, form_inventory_complete, plan_complete, submit_armed, submit_sent, synchronous_verified, delayed_verified, escaped, pre_submit_retry, ambiguous_quarantined, and final disposition. Join by protocol schema, Chrome version, target, ATS, host, worker, and profile mode.
- **Step Context Budget:**

  > Deterministic raw CDP should use zero model tokens. Track command and event counts, JSON bytes, enabled domains, DOM or accessibility nodes, screenshots, response-body bytes, trace bytes, round trips, elapsed time, and fallback model input/output separately. Disable noisy domains when not needed and store large artifacts by pointer.
- **Recommendation:**

  > CANARY ONLY FOR NARROW INSTRUMENTATION OR ADAPTER DISCOVERY; REJECT AS THE PRIMARY FORM DRIVER. Raw CDP offers the lowest serialization overhead and richest browser evidence, but ApplyPilot would reimplement mature Playwright behavior under an unstable tip-of-tree protocol and incur the highest maintenance and safety burden. Use direct CDP selectively when Playwright lacks a required event or diagnostic, with pinned schemas and strict command allowlists.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `cache_replay_yield`
- `concurrency`
- `confidence_intervals`
- `deterministic_coverage`
- `escape_rate`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `marginal_cost_per_attempt`
- `projected_cost_per_verified_apply`
- `site_permission_and_terms`
- `startup_latency`
- `verified_completion_rate`

### Selenium WebDriver

Source record: `Selenium_WebDriver.json`

#### Identity

- **Name:** Selenium WebDriver
- **Category:** driver
- **Official Sources:**

  > - https://www.selenium.dev/documentation/<br>- https://www.selenium.dev/documentation/selenium_manager/<br>- https://www.selenium.dev/documentation/webdriver/bidi/w3c/<br>- https://www.selenium.dev/documentation/grid/<br>- https://github.com/SeleniumHQ/selenium/releases<br>- https://pypi.org/project/selenium/<br>- https://developer.chrome.com/docs/chromedriver/help/operation-not-supported-when-using-remote-debugging<br>- https://developer.chrome.com/blog/remote-debugging-port
- **Maintenance Status:**

  > Mature and actively maintained. PyPI and the upstream repository reported Selenium 4.45.0 released on 2026-06-16, following frequent 2025-2026 releases across Python, Java, JavaScript, .NET, Ruby, Grid, Selenium Manager, and BiDi support.
- **License:**

  > Apache-2.0. It is a permissive dependency and can be included or invoked by ApplyPilot's AGPL-3.0 runtime; preserve upstream license and notice material when redistributing or modifying it.

#### Cost Economics

- **Pricing Model:**

  > Open-source bindings, drivers, Selenium Manager, and self-hosted Grid have no per-session software fee. Cost comes from browser compute, optional Grid/container/cloud infrastructure, engineering maintenance, any model used for unknown answers, and human recovery.
- **Billing Granularity:**

  > No Selenium billing units, idle fees, or concurrency surcharge when self-hosted. Each browser and driver process consumes resources for its session; Grid or third-party cloud providers introduce their own VM, container, browser-minute, concurrency, storage, and egress billing.

#### Reliability

- **Failure Modes:**

  > Locator drift, stale element references, intercepted or non-interactable elements, incorrect waits, SPA readiness after document load, iframe/shadow DOM handling, custom comboboxes, file-upload paths, popup/window focus, driver/browser mismatch, debuggerAddress attach limitations, profile lock, browser/driver crash, Grid session loss, unsupported required questions, auth/challenge walls, and ambiguous post-submit state.
- **Crash Recovery:**

  > A driver can be restarted and a fresh session created, or ChromeDriver can attach to a still-running Chromium debuggerAddress. Selenium does not provide transactional replay; page-object checkpoints, screenshots, network/DOM evidence, session ID, command log, and durable attempt state must be added by ApplyPilot. Any crash after submit remains quarantined until independently reconciled.
- **Exactly Once Submit Safety:**

  > Well suited to an explicit code-level submit_once function: verify URL/host, all required fields, answer provenance, upload hashes, and pre-submit checkpoint; click once; wait for known network/DOM confirmation; store reference ID; quarantine timeout or driver loss. WebDriver itself does not make clicks idempotent.
- **Verification Evidence Tier:**

  > Can collect Tier 2 confirmation DOM, URL/state, and screenshots through standard WebDriver. BiDi or Chromium CDP can capture Tier 1 submission response/status and ATS reference data, while email reconciliation remains Tier 3. Selenium lacks Playwright's integrated Trace Viewer, so evidence packaging must be implemented.

#### Applypilot Fit

- **Python Integration:**

  > Excellent native Python support. Selenium fits ApplyPilot's Python runtime, pytest suite, process supervision, dataclasses, database writes, and deterministic adapter architecture without a Node agent-control subprocess.
- **Playwright Cdp Reuse:**

  > Partial. ChromeOptions debuggerAddress can attach ChromeDriver to ApplyPilot's existing per-worker CDP endpoint and profile, but ChromeDriver documents that some commands are unsupported because its automation extension was not loaded at browser startup. Existing Playwright locators, fixtures, traces, and adapter code are not reusable directly.
- **Ats Fit:**

  > Strong for stable, explicitly modeled Ashby, Greenhouse, Workable, and selected Lever variants. Weak as a general solution for Workday tenant variation, auth-gated sites, inaccessible custom widgets, and long-tail forms unless each variant is coded. It supplies deterministic mechanics, not semantic field mapping or answer generation.
- **Observability Joinability:**

  > Good only after integration. Attach attempt ID, queue URL, worker/profile slot, WebDriver session ID, browser version, command/event timestamps, screenshots, DOM/network confirmation, exception class, and artifact paths to apply_result_events. Selenium Grid adds OpenTelemetry tracing for Grid components but not automatic business-level replay.

#### Authentication

- **Persistent Profiles:**

  > Chrome/Edge can launch with a custom --user-data-dir or attach to an already launched custom profile. One browser process may lock a profile at a time. Chrome 136 does not honor remote debugging against the default Chrome data directory, so ApplyPilot's separate worker profiles are the correct pattern.
- **Existing Session Attach:**

  > Supported for Chromium through ChromeOptions debuggerAddress. ChromeDriver warns that commands requiring its startup automation extension, such as some window operations, are unavailable when attaching. Standard WebDriver session attachment is not a portable cross-browser feature, so test every required command on the ApplyPilot launch mode.
- **Login And Otp:**

  > Can reuse authenticated custom profiles and pause a headed browser for manual login, then resume deterministic code. OTP relay and owner takeover require ApplyPilot orchestration. Do not automate unknown passwords, SSO decisions, CAPTCHAs, or access-control bypasses.
- **Extension Support:**

  > ChromeOptions can load configured extensions when ChromeDriver launches the browser, and an attached browser can retain its installed extensions. Extension behavior and side-loading vary by browser and policy; native messaging is outside Selenium and requires a separate permission and executable audit.
- **Session Data Boundary:**

  > Local WebDriver keeps cookies in the chosen browser profile and commands on the machine. Screenshots, page source, logs, network events, resumes, and database evidence may contain PII. Remote Grid or cloud WebDriver transmits session data to nodes/providers, so use TLS/authentication, restricted artifacts, retention limits, and never expose driver/CDP ports publicly.

#### Quality And Safety

- **Required Field Completeness:**

  > A deterministic adapter can enumerate required attributes, aria-required, validation state, known conditional sections, and ATS-specific custom controls, then fail closed on any unknown required field. This can be stronger than an agent prompt, but only for variants represented in the adapter schema and fixtures.
- **Answer Provenance:**

  > Strong when the adapter accepts only typed values carrying profile/resume/approved-answer source IDs and refuses raw model text without verifier approval. Selenium provides no provenance layer; ApplyPilot must enforce it before send_keys or script execution.
- **Unsupported Question Fallback:**

  > Return a structured unresolved-field result before submit, then use the evidence-constrained answerer or human review. Do not add generic plausible text or silently skip a required field.
- **Prompt Injection Resistance:**

  > Strongest of the three when code is fully deterministic: page text is parsed as data and cannot choose arbitrary host tools or shell commands. Keep navigation on an ATS allowlist, avoid executing page-supplied JavaScript, sanitize downloads/filenames, restrict secrets, and test hostile labels and hidden DOM. Risk returns if a general model is allowed to generate arbitrary Selenium code at runtime.
- **Site Permission And Terms:**

  > WebDriver is an automation mechanism, not permission. Use only legitimate candidate flows allowed by the ATS/site and account, honor terms and rate limits, prefer documented interfaces, and stop at login, CAPTCHA, challenge, or other access-control boundaries rather than bypassing them.
- **Irreversible Action Guard:**

  > A deterministic submit method can be the only code path with access to the final button. Require complete plan and provenance digests, expected host and form state, durable pre-submit event, one click, known confirmation predicate, and crash quarantine. This is easier to enforce than unrestricted agent click tools.

#### Operations

- **Warm Session Reuse:**

  > Strong for repeated deterministic jobs when one worker retains its driver and custom profile. Reset tabs, navigation, dialogs, downloads, and per-job state between attempts; recycle the browser on memory growth or driver failure without sharing the profile concurrently.
- **Tracing And Replay:**

  > Standard screenshots, page source, browser/driver logs, and command listeners are available; BiDi provides network/log/script events, and Grid can emit OpenTelemetry traces. Selenium has no equivalent integrated DOM-snapshot Trace Viewer or deterministic action replay, so ApplyPilot must build an attempt timeline and safe pre-submit replay boundary.
- **Deployment Modes:**

  > Local Python process, self-hosted standalone server, Selenium Grid, Docker/Grid nodes, Kubernetes, and third-party remote WebDriver clouds. ApplyPilot can begin local and add Grid only if browser capacity, cross-browser testing, or remote isolation justifies its operations cost.
- **Browser Version Matrix:**

  > Broad W3C WebDriver coverage for Chrome/Chromium, Edge, Firefox, Safari, and other vendor drivers; Selenium Manager handles compatible binaries. WebDriver BiDi support is expanding and CDP support is documented as temporary/version-sensitive. Existing-session debuggerAddress attach is Chromium-specific.
- **Canary Stop Loss:**

  > Use synthetic forms and shadow plan generation first, then a small unique-job canary on one stable ATS. Enforce zero unknown required fields, one submit, known confirmation predicate, per-step and wall timeouts, command cap, host allowlist, worker/browser breakers, and immediate stop on duplicate or false APPLIED. Compare directly with the existing deterministic Playwright adapter.

#### Benchmark

- **Benchmark Design:**

  > Implement the same minimal Greenhouse or Ashby adapter once in Selenium and once in existing Python Playwright, using identical typed answer plans and confirmation predicates. Run three repeats on synthetic variants for waits, widgets, uploads, crashes, auth, hostile text, and confirmation, then unique live canaries. Measure plan-ready coverage, verified completion, command count, wall time, resource use, repair effort, artifact quality, and duplicate/false-positive rate.
- **Recommendation:**

  > Reject a production migration to Selenium solely for cost reduction; retain it as the deterministic Python driver benchmark. ApplyPilot already has Playwright, CDP profiles, traces, fixtures, and deterministic adapter code, so Selenium duplicates work without creating the savings. Canary Selenium only if it proves a specific browser/ATS compatibility, Grid, or standards requirement that existing Python Playwright cannot meet.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `cache_replay_yield`
- `concurrency`
- `confidence_intervals`
- `control_plane_overhead`
- `deterministic_coverage`
- `escape_rate`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `model_cost`
- `projected_cost_per_verified_apply`
- `route_funnel`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`

### WebDriver BiDi and WebdriverIO

Source record: `WebDriver_BiDi_and_WebdriverIO.json`

#### Identity

- **Name:** WebDriver BiDi and WebdriverIO
- **Category:** driver
- **Official Sources:**

  > - https://www.w3.org/TR/webdriver-bidi/<br>- https://wpt.fyi/results/webdriver/tests/bidi<br>- https://webdriver.io/docs/automationProtocols/<br>- https://webdriver.io/docs/capabilities/<br>- https://webdriver.io/docs/api/webdriverBidi/<br>- https://webdriver.io/docs/extension-testing/web-extensions/<br>- https://github.com/webdriverio/webdriverio/releases
- **Maintenance Status:**

  > Active, with material maintenance since January 2025. WebDriver BiDi was published as a W3C Working Draft on 2026-06-01 and remains explicitly under development. WebdriverIO v9 had current releases in 2026, including v9.27.1 on 2026-04-30.
- **License:**

  > The WebDriver BiDi specification is covered by the W3C permissive document license and patent policy. WebdriverIO is MIT licensed. The MIT implementation can be used by ApplyPilot's AGPL-3.0 codebase if copyright and license notices are retained; using it does not relax ApplyPilot's own AGPL obligations.

#### Cost Economics

- **Pricing Model:**

  > No protocol or WebdriverIO license fee for local self-hosting. Cash cost is browser and Node.js compute, storage for artifacts, and engineering. Selenium Grid or commercial browser clouds are optional and introduce their own session-minute, concurrency, and egress pricing.
- **Billing Granularity:**

  > Local execution has no vendor billing unit. Allocate browser runtime by second or attempt internally. A reused idle browser consumes RAM but no protocol fee; cloud grids may round to session minutes and charge parallel slots, video, storage, or egress separately.
- **Model Cost:**

  > Zero for deterministic WebdriverIO scripts. Any semantic planner or answer generator layered above the driver has separate token cost; keep those calls attributable to the same attempt and route rather than assigning them to BiDi.
- **Infrastructure Cost:**

  > Requires a browser, browser driver or BiDi endpoint, a Node.js WebdriverIO process, and artifact storage. Typical sessions are CPU and RAM workloads rather than GPU workloads. Cross-browser matrices multiply browser images and driver-version maintenance; persistent headed sessions also consume desktop resources.

#### Reliability

- **Failure Modes:**

  > Driver or browser version mismatch; BiDi command unsupported by a browser; draft-protocol behavior change; stale element or browsing-context references; frame and window races; navigation or script timeout; driver or WebSocket disconnect; profile lock; headless-only behavior; login or challenge wall; unsupported ATS field; and submit followed by missing confirmation.
- **Crash Recovery:**

  > WebdriverIO exposes command, result, retry, and session lifecycle events and can reload or attach to known sessions. Optional trace tooling records actions, network, screenshots, and accessibility snapshots. It does not provide application-level resume or submission transactions; ApplyPilot must checkpoint before submit, persist the target/session and evidence, and quarantine post-submit disconnects.
- **Exactly Once Submit Safety:**

  > Not built in. Use an ApplyPilot idempotency key where the ATS supports one, persist a pre-submit fingerprint and final irreversible-action boundary, capture the submit network request and response, and classify any post-submit timeout or disconnect as crash_unconfirmed rather than automatically retrying.
- **Verification Evidence Tier:**

  > Can capture strong synchronous evidence: ATS response or identifier through BiDi network events where implemented, known success URL and confirmation DOM, console logs, and screenshots. Email confirmation remains an external delayed tier. Evidence quality depends on explicit ApplyPilot collectors, not on WebdriverIO's test pass status.
- **Verification Latency:**

  > DOM, URL, log, and supported network evidence can be collected in the same session within seconds of submit. Email reconciliation remains asynchronous and should run on ApplyPilot's existing delayed verifier. Missing synchronous evidence must end as ambiguous, not applied.

#### Applypilot Fit

- **Python Integration:**

  > WebDriver BiDi is language-neutral, but WebdriverIO is a Node.js/TypeScript framework with no native Python API. ApplyPilot would need a supervised Node sidecar or subprocess RPC with typed commands, cancellation, logs, and result envelopes, or it would need a different Python BiDi client. This is a material integration penalty versus the current Python Playwright path.
- **Playwright Cdp Reuse:**

  > Partial. Existing Chrome binaries, custom user-data directories, remote-debugging ports, and authenticated profile copies can be reused, and WebdriverIO has attach-to-session and Chrome CDP paths. Playwright BrowserContext, Page objects, locators, and fixtures cannot be reused directly. Native BiDi normally starts through a browser driver, while Chrome attachment may use a CDP-backed path; Chrome 136+ also rejects remote debugging against the default data directory unless a non-standard user-data-dir is used.
- **Migration Effort:**

  > Medium-to-large. Add and supervise a Node runtime, define a Python-to-Node contract, port selectors and waits, recreate fixtures and evidence capture, add profile ownership, and rerun ATS canaries. A narrow comparator can be built without replacing Playwright; a fleet-wide migration is not justified by protocol choice alone.
- **Ats Fit:**

  > Good as a deterministic cross-browser driver for known Ashby, Greenhouse, Lever, Workable, Workday, and long-tail form variants. It supplies navigation, locators, input, script, prompt, screenshot, log, and increasingly network primitives, but it does not understand ATS semantics or decide truthful answers. Workday tenant variation and auth-heavy boards remain policy and adapter problems.
- **Control Plane Overhead:**

  > No model context is required for deterministic actions. Each command and event is serialized over WebDriver HTTP and/or a BiDi WebSocket through a browser driver, then WebdriverIO adds JavaScript promises and hooks. For ApplyPilot, Python-to-Node RPC adds another serialization and process boundary. This is likely more overhead than raw CDP and less than an MCP plus general-agent loop.
- **Observability Joinability:**

  > Strong if instrumented. WebdriverIO exposes request, command, BiDi command, result, retry, log, and network events; optional trace tooling adds action, screenshot, accessibility, console, and HAR-style artifacts. Propagate ApplyPilot attempt_id, queue row, route, worker, browser session, browsing context, request ID, and confirmation evidence into one append-only result envelope.

#### Authentication

- **Persistent Profiles:**

  > Browser-specific capabilities can point Chrome or Firefox at managed profile data, and WebdriverIO can launch separate or copied Chrome profiles. A profile directory must have one owning browser process, be version-compatible, and be cloned only while quiescent. Encrypt profile storage and keep owner-authenticated profiles off untrusted workers.
- **Existing Session Attach:**

  > Possible but conditional. WebdriverIO can attach to a known WebDriver or DevTools session, and its Chrome tooling can attach through CDP. A random already-open browser is not automatically a reusable BiDi session: it must expose the required endpoint and session metadata. Prefer a dedicated debug profile and explicit ownership instead of attaching to the user's default profile.
- **Login And Otp:**

  > Supports headed execution, waits, screenshots, browser events, and detaching or pausing for owner takeover, but provides no login or OTP service. Route login gates to an owner-controlled profile, relay approved email OTP through ApplyPilot's inbox path, and never treat CAPTCHA or an account decision as an automation failure to retry blindly.
- **Extension Support:**

  > Documented for Chrome and Firefox. Chrome can load unpacked directories or CRX data through capabilities; Firefox can use a prepared profile or installAddOn. Safari extension support is not covered by the WebdriverIO guide. Use narrowly permissioned extensions and avoid sharing native-messaging secrets with page code.
- **Session Data Boundary:**

  > With local WebdriverIO and local browsers, cookies, credentials, resumes, screenshots, traces, and network bodies can remain on the worker and ApplyPilot-controlled storage. Browser grids or cloud vendors move those data to the provider and require a separate retention, region, encryption, and access review. Redact authorization headers, cookies, OTPs, and resume contents from routine logs.

#### Quality And Safety

- **Required Field Completeness:**

  > No automatic guarantee. Build an explicit field inventory before filling, map every required control, re-scan after conditional questions appear, and refuse submit when any required field lacks a value or provenance. WebdriverIO locators and BiDi node lookup are mechanisms, not completeness policy.
- **Answer Provenance:**

  > No native answer-governance layer. Every value should carry a source such as profile field, resume span, approved answer ID, or deterministic transform. Block generated claims about experience, salary, authorization, demographics, or credentials unless an approved source permits them.
- **Unsupported Question Fallback:**

  > Stop before submit and return a typed reason with label, control type, options, current answer plan, screenshot, and DOM locator. Route to an approved-answer lookup, bounded semantic helper, or human review; never default-select, fabricate, or silently omit a required answer.
- **Prompt Injection Resistance:**

  > Deterministic WebdriverIO scripts have no model prompt to inject, which is a useful safety property. If an LLM consumes DOM, logs, or screenshots, treat page content as untrusted data, restrict commands and origins, separate secrets from model context, disable arbitrary script generation, and enforce the final submit gate outside the model.
- **Irreversible Action Guard:**

  > Implement outside WebdriverIO as a two-phase ApplyPilot action: prepare and validate a complete answer plan, persist a pre-submit checkpoint and evidence, then allow exactly one named submit command. Disable generic click or script-evaluation methods from reaching submit controls in model-assisted modes.

#### Operations

- **Warm Session Reuse:**

  > Supported through a long-lived browser session, reloadSession, attachToSession, or a controlled persistent profile. Reset tabs, downloads, dialogs, storage policy, event subscribers, and attempt identifiers between jobs. Never run two applications concurrently in one authenticated profile unless the ATS and exactly-once design are proven safe.
- **Tracing And Replay:**

  > WebdriverIO exposes command and BiDi events plus screenshots, browser logs, and network events. Optional @wdio/devtools-service trace mode can write an action timeline, HAR-style network data, screenshots, accessibility snapshots, and a transcript. This supports diagnosis and comparison, but deterministic application replay still requires ApplyPilot checkpoints and idempotency logic.
- **Deployment Modes:**

  > Local Windows or macOS workers, self-hosted Linux or container workers, Selenium Grid, commercial browser grids, and hybrid owner-profile routing. ApplyPilot's lowest-data-exposure path is a local Node sidecar beside the Python worker; cloud sessions need an explicit data-boundary review.
- **Browser Version Matrix:**

  > WebDriver Classic covers Chrome, Chromium/Edge, Firefox, and Safari through their drivers. Current WebdriverIO attempts BiDi by default; recent Chrome, Edge, and Firefox implement many BiDi primitives, while Safari and cloud-provider support remain incomplete for some features. Pin browser and driver versions and consult WPT results for every BiDi primitive used.
- **Canary Stop Loss:**

  > ApplyPilot must supply these controls: eligible-host allowlist, per-ATS daily cap, one-profile concurrency, maximum actions and elapsed time, zero automatic post-submit retry, maximum model and infrastructure spend, and circuit breakers for login gates, unsupported commands, ambiguous confirmations, and verified-completion regression.

#### Benchmark

- **Benchmark Design:**

  > Run matched, randomized control and treatment attempts on the same ATS and host strata. Include Ashby, Greenhouse, Lever, Workable, per-tenant Workday, auth gates, iframes, file uploads, conditional fields, custom widgets, delayed confirmation, and synthetic hostile-page cases. Repeat each form in headed and headless modes and pin browser versions.
- **Route Funnel:**

  > Record eligible, selected_bidi, session_started, profile_ready, form_inventory_complete, plan_complete, submit_armed, submit_sent, synchronous_verified, delayed_verified, escaped, retried_pre_submit, ambiguous_quarantined, and final disposition. Break every stage down by ATS, host, browser, version, worker, and profile mode.
- **Step Context Budget:**

  > Deterministic execution should use zero LLM tokens. Track WebDriver and BiDi command count, event count and bytes, Python-to-Node RPC bytes, DOM or accessibility snapshots, screenshots, network bodies, elapsed time, and any fallback model input/output separately. Large traces should be stored by pointer rather than copied into queue metadata.
- **Recommendation:**

  > WATCH, with a bounded comparator canary rather than adoption. WebDriver BiDi is strategically valuable for standards-based cross-browser control, and WebdriverIO is active and observable, but the draft feature matrix and Node-to-Python integration cost offer no clear advantage over ApplyPilot's current Playwright/CDP assets. Reconsider for cross-browser requirements or if the canary materially lowers runtime failures without reducing positive confirmation quality.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `cache_replay_yield`
- `concurrency`
- `confidence_intervals`
- `deterministic_coverage`
- `escape_rate`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `marginal_cost_per_attempt`
- `projected_cost_per_verified_apply`
- `site_permission_and_terms`
- `startup_latency`
- `verified_completion_rate`

### Apify browser automation

Source record: `Apify_browser_automation.json`

#### Identity

- **Name:** Apify browser automation
- **Category:** hosted_browser
- **Official Sources:**

  > - https://apify.com/pricing<br>- https://docs.apify.com/actors/running/usage-and-resources<br>- https://docs.apify.com/actors/running/runs-and-builds<br>- https://docs.apify.com/platform/actors/development/programming-interface/standby<br>- https://docs.apify.com/sdk/python/<br>- https://docs.apify.com/storage<br>- https://docs.apify.com/platform/security<br>- https://docs.apify.com/legal/privacy-policy<br>- https://github.com/apify/crawlee-python<br>- https://github.com/apify/apify-sdk-python
- **Maintenance Status:**

  > Active. Apify maintains the platform, Actor runtime, JavaScript and Python SDKs, Crawlee, storage, proxy, standby, and security documentation, with current platform and repository activity after January 2025.
- **License:**

  > Managed commercial platform under Apify terms. Apify SDK for Python and Crawlee for Python are Apache-2.0, which is permissive for use by AGPL-3.0 ApplyPilot with notices preserved. Community Store Actors have independent licenses and terms that must be reviewed individually.

#### Cost Economics

- **Pricing Model:**

  > Subscription credits plus usage. Free provides $5/month of prepaid usage; Starter is $29/month with $29 usage; Scale is $199/month with $199 usage; Business is $999/month with $999 usage. Current compute is $0.20/CU on Free/Starter, $0.16 on Scale, and $0.13 on Business; 1 CU equals 1 GB RAM for 1 hour. Transfer, storage operations, residential proxy traffic, rented Actors, and Store event charges can add cost.
- **Billing Granularity:**

  > Compute units are memory multiplied by duration with one-second minimum granularity. Subscription credits expire each billing cycle; paying plans continue into overage up to configured limits. Separate billing can include external/internal transfer, storage GB-hours and operations, residential proxy GB, datacenter IPs, Store rental, or pay-per-event Actor charges.
- **Model Cost:**

  > No model is required for deterministic Playwright/Crawlee Actors. AI Actors, LLM extraction, or ApplyPilot's answerer/verifier add provider-specific token or event costs, and third-party Store Actors may charge per run, result, event, or rental. Meter model usage outside CU.
- **Infrastructure Cost:**

  > Apify hosts Docker Actor builds, CPU/RAM, browser processes, APIs, schedules, queues, datasets, key-value stores, request queues, proxies, logs, and standby instances. Cost is primarily CU plus transfer/proxy/storage; standby keeps capacity warm and therefore continues consuming compute while ready.

#### Reliability

- **Failure Modes:**

  > - Actor build, image, dependency, startup, migration, timeout, abort, or memory failure.<br>- Browser crash, Playwright selector drift, navigation timeout, or page resource overload.<br>- Proxy block, CAPTCHA, authentication, OTP, or target-site policy denial.<br>- Community Actor behavior, pricing, schema, or maintenance can change independently.<br>- Required ATS fields, uploads, custom widgets, and sensitive questions may remain unsupported.<br>- A run can succeed technically while application submission is absent or ambiguous.
- **Exactly Once Submit Safety:**

  > Not native. Actor run IDs and request queues help deduplicate work, but ApplyPilot must prevent duplicate applications with job-level keys, pre-submit checkpoints, one irreversible action, network/DOM confirmation, and crash-uncertain quarantine.
- **Verification Evidence Tier:**

  > Custom Actors can store ATS IDs, response payloads, confirmation DOM, URLs, screenshots, logs, and datasets. Delayed email remains external. Evidence quality depends entirely on Actor implementation; a SUCCEEDED Actor status is not evidence that an application was submitted.

#### Applypilot Fit

- **Python Integration:**

  > Strong. Apify has official Python SDK and API client support, Crawlee for Python, Python Actor templates, and Playwright-based crawlers. ApplyPilot can call Actors over API or package portions of its Python runtime into an Actor.
- **Playwright Cdp Reuse:**

  > Partial. Existing Playwright Python code can run inside an Apify Actor or be adapted to Crawlee, but Apify is not primarily a remote-CDP endpoint for attaching current ApplyPilot adapters to a managed browser. Existing local Chrome ports and profiles do not transfer automatically.
- **Migration Effort:**

  > Medium to large. A custom Actor requires Docker/build configuration, input and output schemas, secrets, storage/retention, run orchestration, cost tags, callbacks or polling, and fleet result integration. Reusing a Store Actor reduces initial work but adds third-party trust, schema, pricing, and maintenance risk.
- **Control Plane Overhead:**

  > Adds Actor build/version selection, run-start API, container startup, input serialization, output storage, status polling/webhooks, and transfer between ApplyPilot and Apify. Batching jobs or Standby can reduce startup overhead but complicates isolation, billing, and exactly-once handling.
- **Observability Joinability:**

  > Good if ApplyPilot stores Actor run ID, build, task, dataset/key-value IDs, usage, worker/route metadata, and result-event keys. APIs expose run status, resource usage, logs, and storage, allowing queue-to-container-to-cost joins; browser network evidence must be instrumented by the Actor.

#### Authentication

- **Existing Session Attach:**

  > No reviewed native path safely attaches an Actor to an already-authenticated local ApplyPilot Chrome tab or CDP port across the network. Use an Actor-owned session or retain the local owner browser route.
- **Session Data Boundary:**

  > Actor inputs, environment variables, browser traffic, cookies, resumes, screenshots, logs, datasets, and key-value records are processed on Apify infrastructure. Encrypted inputs and environment variables are available. Unnamed storage retention depends on plan; named storage is retained indefinitely until deleted. Personal-data processing requires an applicable DPA and explicit retention/deletion controls.

#### Quality And Safety

- **Required Field Completeness:**

  > Not guaranteed by Apify or Crawlee. ApplyPilot-owned Actor logic must enumerate required controls, detect validation messages and conditional fields, and refuse an incomplete plan before submit.
- **Answer Provenance:**

  > Not native. Keep approved profile/resume evidence and source IDs in ApplyPilot, pass only required values to the Actor, and record which approved answer populated each field. Community Actors must not invent candidate claims.
- **Unsupported Question Fallback:**

  > Custom Actor logic should stop and emit a structured unsupported-question event for ApplyPilot's answerer or human queue. A generic retry is unsafe for sensitive or unknown application questions.
- **Prompt Injection Resistance:**

  > Treat page and Store Actor output as untrusted. Constrain domains, inputs, Actor permissions, API tokens, outbound services, secrets, and final-submit commands. Prefer ApplyPilot-owned source over opaque community Actors for candidate credentials and irreversible actions.
- **Site Permission And Terms:**

  > Use only for lawful, permitted automation. Apify provides infrastructure and proxy products, but target ATS terms, access controls, robots/policy, candidate consent, and employer restrictions remain ApplyPilot's responsibility. Proxy rotation is not permission.
- **Irreversible Action Guard:**

  > Must be implemented in the Actor and ApplyPilot controller: complete answer plan, dedup key, policy check, pre-submit snapshot, one final click/request, and authoritative confirmation. Actor retries must never repeat an ambiguous final action automatically.

#### Operations

- **Concurrency:**

  > Current plan limits are 25 concurrent runs on Free, 32 on Starter, 128 on Scale, and 256 on Business, with custom enterprise limits. Actual browser concurrency also depends on RAM per run, account usage, Actor design, proxy capacity, and target-site limits.
- **Warm Session Reuse:**

  > Supported through batching multiple requests in one Actor, request queues, custom persisted state, or Actor Standby. Warm reuse improves resource efficiency but requires strict per-candidate isolation, cookie locking, timeout handling, and cost controls.
- **Tracing And Replay:**

  > Moderate. Apify supplies run logs, status, datasets, key-value storage, screenshots if captured, request queues, and usage details. Full browser video, DOM replay, CDP network archives, and deterministic replay are not universal platform guarantees and must be added to the Actor.
- **Deployment Modes:**

  > Managed Apify cloud Actors, scheduled/tasks/API runs, Standby HTTP Actors, and local development for Apify SDK/Crawlee projects. Crawlee and SDK code can be self-hosted outside Apify, but managed platform capabilities and billing do not transfer automatically.
- **Browser Version Matrix:**

  > Primarily Chromium through Playwright/Puppeteer/Crawlee browser crawlers; Firefox/WebKit availability depends on custom images and framework support rather than a managed matrix. Pin browser and Playwright versions in Actor builds and test ATS compatibility before rollout.
- **Canary Stop Loss:**

  > Use run memory/time limits, account usage caps, abort controls, max retries, queue limits, and billing notifications. ApplyPilot must add per-route CU/proxy/model ceilings, failure breakers, Store Actor version pins, and no-retry rules after ambiguous submit.

#### Benchmark

- **Benchmark Design:**

  > Compare an ApplyPilot-owned Apify Playwright Actor with the current local route on matched ATS-stratified jobs. Hold adapters, answers, and submit policy constant; record CU, RAM, runtime, transfer, proxy bytes, storage operations, cold start, retries, evidence tier, fallback, and human minutes. Evaluate community Actors separately.
- **Route Funnel:**

  > Eligibility and terms check -> deterministic route -> Apify canary gate -> start pinned custom Actor with minimal input -> required-field/provenance validation -> pre-submit checkpoint -> one final action -> structured evidence output -> email reconciliation -> verified, ambiguous quarantine, fallback, or reject.
- **Recommendation:**

  > Watch, with a narrow canary only if the reusable Actor ecosystem or burst orchestration is specifically needed. Apify is a capable general execution platform, but its CU-plus-transfer-plus-proxy economics and weaker direct attach/human-takeover fit make it less compelling than a dedicated low-cost CDP browser for ApplyPilot's primary hosted-browser route.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `cache_replay_yield`
- `confidence_intervals`
- `crash_recovery`
- `deterministic_coverage`
- `escape_rate`
- `extension_support`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `login_and_otp`
- `marginal_cost_per_attempt`
- `persistent_profiles`
- `projected_cost_per_verified_apply`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`

### Bright Data Browser API (formerly Scraping Browser)

Source record: `Bright_Data_Scraping_Browser.json`

#### Identity

- **Name:** Bright Data Browser API (formerly Scraping Browser)
- **Category:** hosted_browser
- **Official Sources:**

  > - https://brightdata.com/pricing/scraping-browser<br>- https://docs.brightdata.com/scraping-automation/scraping-browser/introduction<br>- https://docs.brightdata.com/scraping-automation/scraping-browser/faqs<br>- https://docs.brightdata.com/scraping-automation/scraping-browser/code-examples<br>- https://docs.brightdata.com/scraping-automation/scraping-browser/cdp-functions/custom<br>- https://docs.brightdata.com/api-reference/browser-api/get-sessions<br>- https://docs.brightdata.com/general/security/security-overview<br>- https://brightdata.com/license<br>- https://media.brightdata.com/2023/07/Bright-Data-Data-Protection-Agreement.pdf
- **Maintenance Status:**

  > Active. Bright Data now documents the product as Browser API, with current Playwright/Puppeteer/Selenium, session API, custom CDP, CLI, security, and pricing documentation maintained through 2025-2026.
- **License:**

  > Managed commercial service under Bright Data's license agreement: limited, revocable, non-transferable use for the client's internal business operations, with no right to sublicense. It can be called by AGPL-3.0 ApplyPilot without relicensing ApplyPilot, but service restrictions, compliance review, data terms, and target-site permissions apply.

#### Cost Economics

- **Pricing Model:**

  > Bandwidth-based. Pay as you go is $8/GB. Current monthly options list 71 GB for $499 at $7/GB, 166 GB for $999 at $6/GB, and 399 GB for $1,999 at $5/GB; enterprise is custom. Managed browsers, proxy selection, JavaScript rendering, CAPTCHA solving, and unlocking are presented as included features.
- **Model Cost:**

  > Browser API controlled directly by Playwright, Puppeteer, or Selenium requires no LLM. Any ApplyPilot semantic planner, answerer, verifier, or separate Bright Data agent/MCP feature adds model cost outside the Browser API bandwidth price.
- **Infrastructure Cost:**

  > Bright Data supplies remote GUI Chromium, proxy networks, fingerprint/header/session management, CAPTCHA detection/solving, retries, scaling, and unblocking. These are bundled into bandwidth pricing; ApplyPilot still pays its controller, model, evidence storage, and operator costs.

#### Reliability

- **Failure Modes:**

  > - Expired, duplicate, ineligible, or policy-denied jobs remain invalid work.<br>- Compliance review, KYC, target restrictions, or account suspension can prevent service use.<br>- WebSocket/CDP/HTTPS disconnect, hosted browser failure, timeout, or retry exhaustion can interrupt a run.<br>- One initial navigation per Browser API session can conflict with cross-origin or multi-site application flows.<br>- Login, MFA, OTP, account ownership, or unsupported CAPTCHA can still block completion.<br>- Unsupported fields, uploads, dialogs, or ambiguous final confirmation can produce false success.
- **Exactly Once Submit Safety:**

  > Not native. ApplyPilot must preserve dedup keys, pre-submit checkpoints, one final action, response/DOM capture, no blind retry after ambiguity, and delayed email/ATS reconciliation. Session metadata is useful evidence but not a transaction log.
- **Verification Evidence Tier:**

  > Browser control can capture ATS IDs or response data when exposed, confirmation DOM/URL, screenshots, cookies, network information, and session metadata including status, duration, CAPTCHA, bandwidth, and errors. Email remains external; session success alone is not verified application evidence.

#### Applypilot Fit

- **Python Integration:**

  > Good. Official examples support Python Playwright and Selenium. Python Playwright connects with chromium.connect_over_cdp to wss://brd.superproxy.io:9222; Selenium uses an HTTPS remote endpoint on port 9515.
- **Playwright Cdp Reuse:**

  > Good for existing Playwright action logic through connect_over_cdp. It does not reuse ApplyPilot's local CDP port or local Chrome profile in place. Bright Data requires its zone credentials and warns against adding unsupported connection options.
- **Migration Effort:**

  > Medium. A basic provider launcher is small, but production integration needs zone credentials, bandwidth/cost telemetry, resource blocking, one-navigation flow testing, owner-auth fallback, compliance approval, session/error joins, and evidence capture before exactly-once submission.
- **Control Plane Overhead:**

  > Adds a remote CDP or Selenium connection, zone authentication, cross-network action round trips, managed retry latency, and session bandwidth accounting. Browser caching within a session can reduce repeated downloads; each session's navigation constraints must be reflected in adapter flow design.
- **Observability Joinability:**

  > Moderate to good if ApplyPilot stores Bright Data session ID and zone with job, attempt, worker, and route IDs. The sessions API returns target/end URL, navigations, timestamps, duration, CAPTCHA state, bandwidth, status, and errors. Full DOM/network replay must be captured by ApplyPilot tooling.

#### Authentication

- **Existing Session Attach:**

  > Automation attaches to Bright Data-created cloud browsers by WebSocket/HTTPS credentials. It cannot safely attach to an existing authenticated local ApplyPilot Chrome tab/profile; retain the owner/local route for that requirement.
- **Session Data Boundary:**

  > Page traffic, credentials entered into pages, cookies, resumes, screenshots, and session metadata traverse Bright Data-managed browsers and proxy infrastructure. The security platform lists ISO 27001/27017/27018 and SOC reporting, but the current license states Bright Data may retain client-collected data and use it for its own purposes in its sole discretion. This is a material concern for candidate PII and requires legal/DPA clarification before production use.

#### Quality And Safety

- **Required Field Completeness:**

  > Not native. ApplyPilot adapters must enumerate required controls, validation messages, conditional fields, and upload completion, then refuse incomplete plans before final submit.
- **Answer Provenance:**

  > Not native. ApplyPilot must derive answers only from approved resume/profile/saved-answer evidence and log source IDs. Managed browser/unblocking features do not validate candidate claims.
- **Unsupported Question Fallback:**

  > Unknown, sensitive, or free-text questions should stop the route and return a structured fallback to ApplyPilot's verified answerer or human queue. Automated unblocking must never imply permission to answer or submit.
- **Prompt Injection Resistance:**

  > Bright Data advises treating web content as untrusted, validating/filtering it before LLM use, preferring structured extraction, storing credentials securely, and scoping API permissions. ApplyPilot must additionally enforce domain allowlists, secret isolation, limited tools, hostile-page tests, and a hard submit boundary.
- **Site Permission And Terms:**

  > High scrutiny required. Bright Data requires lawful use, acceptable-use compliance, and sometimes KYC/compliance review, and may suspend uses that create security, liability, or blocking risk. ApplyPilot must separately establish target ATS permission and must not use unblocking, residential proxies, fingerprinting, or CAPTCHA solving to evade access controls.
- **Irreversible Action Guard:**

  > No application-domain guard is built in. ApplyPilot must require complete approved answers, dedup and policy checks, a pre-submit checkpoint, exactly one final action, and authoritative confirmation; retries after an ambiguous result must quarantine rather than resubmit.

#### Operations

- **Warm Session Reuse:**

  > Partial. Browser caching can accelerate repeated same-domain activity during a session, CLI named sessions keep a connection open, and Proxy.useSession can preserve proxy-peer continuity. The FAQ says each Browser API session supports only one initial navigation, although interactions and resulting navigations can continue; durable browser-profile reuse is not established.
- **Deployment Modes:**

  > Managed Bright Data Browser API accessed by WebSocket for Playwright/Puppeteer or HTTPS for Selenium, plus local client scripts and Bright Data CLI. No self-hosted Browser API deployment was identified.
- **Browser Version Matrix:**

  > Managed GUI Chromium/Chrome optimized for scraping, with Playwright, Puppeteer, and Selenium compatibility. No Firefox, WebKit, or WebDriver BiDi support was confirmed in reviewed primary docs.
- **Canary Stop Loss:**

  > Use zone/account spend controls, explicit browser close, bandwidth reporting per session, resource blocking, max duration, max retries, and concurrency caps. ApplyPilot must add route allowlists, cost-per-attempt ceilings, failure breakers, and immediate stop on policy/compliance or ambiguous-submit signals.

#### Benchmark

- **Benchmark Design:**

  > Use only approved targets. Compare the current local route with Bright Data on a matched protected-host slice, not the full fleet. Record provider session ID, bytes, plan rate, target/end URL, duration, CAPTCHA, retries, evidence tier, completion, fallback, and human minutes; include a no-images/ad-block arm to estimate bandwidth sensitivity.
- **Route Funnel:**

  > Eligibility, permission, and privacy review -> deterministic local route first -> Bright Data canary only for approved access-failure bucket -> minimal-data session -> required-field/provenance checks -> pre-submit guard -> one submit -> DOM/network evidence -> email reconciliation -> verified, ambiguous quarantine, fallback, or reject.
- **Recommendation:**

  > Reject as a general ApplyPilot browser route; consider a tightly controlled watchlist experiment only for lawful, approved access/challenge failures. Bandwidth pricing is materially higher than browser-hour competitors for media-heavy application pages, and the license's broad collected-data retention/use clause is a serious candidate-PII concern requiring legal clarification before any real-profile test.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `billing_granularity`
- `cache_replay_yield`
- `concurrency`
- `confidence_intervals`
- `crash_recovery`
- `deterministic_coverage`
- `escape_rate`
- `extension_support`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `login_and_otp`
- `marginal_cost_per_attempt`
- `persistent_profiles`
- `projected_cost_per_verified_apply`
- `startup_latency`
- `step_context_budget`
- `tracing_and_replay`
- `verification_latency`
- `verified_completion_rate`

### Browserbase

Source record: `Browserbase.json`

#### Identity

- **Name:** Browserbase
- **Category:** hosted_browser
- **Official Sources:**

  > - Browserbase pricing: https://www.browserbase.com/pricing<br>- Browserbase billing plans: https://docs.browserbase.com/account/billing/plans<br>- Browserbase changelog: https://www.browserbase.com/changelog<br>- Browserbase Python SDK docs: https://docs.browserbase.com/reference/sdk/python<br>- Browserbase Playwright quickstart: https://docs.browserbase.com/welcome/quickstarts/playwright<br>- Browserbase create session API: https://docs.browserbase.com/reference/api/create-a-session<br>- Browserbase contexts: https://docs.browserbase.com/platform/browser/core-features/contexts<br>- Browserbase observability: https://docs.browserbase.com/platform/browser/observability/observability<br>- Browserbase session recording: https://docs.browserbase.com/platform/browser/observability/session-recording<br>- Browserbase session live view: https://docs.browserbase.com/platform/browser/observability/session-live-view<br>- Browserbase cost optimization: https://docs.browserbase.com/optimizations/cost/cost-optimization<br>- Browserbase usage tracking: https://docs.browserbase.com/optimizations/cost/measuring-usage<br>- Browserbase identity and authentication: https://docs.browserbase.com/platform/identity/authentication<br>- Browserbase allowed domains: https://docs.browserbase.com/platform/identity/allowed-domains<br>- Browserbase enterprise security: https://docs.browserbase.com/account/enterprise/security<br>- Browserbase terms of service: https://www.browserbase.com/terms-of-service<br>- Browserbase privacy policy: https://www.browserbase.com/privacy-policy<br>- Browserbase Python SDK repository: https://github.com/browserbase/sdk-python
- **Maintenance Status:**

  > Active. Browserbase pricing, billing, SDK, session, observability, identity, and runtime docs are live; the changelog shows 2026 product work including rebuilt session recordings and Functions updates, and the current Python SDK repository is maintained separately from the deprecated older python-sdk repository.
- **License:**

  > Managed commercial service under Browserbase terms. The current browserbase/sdk-python repository is Apache-2.0, which is permissive and compatible to call from an AGPL-3.0 ApplyPilot codebase if notices are preserved; service use still depends on Browserbase account terms, pricing, privacy, and target-site permissions.

#### Cost Economics

- **Pricing Model:**

  > Hybrid subscription plus metered usage. Free is $0 with 3 concurrent browsers and 1 browser hour; Developer is $20/month with 25 concurrent browsers, 100 included browser hours, then $0.12/browser-hour; Startup is $99/month with 100 concurrent browsers, 500 included browser hours, then $0.10/browser-hour; Scale is custom with 250+ concurrent browsers and usage-based terms. Proxy bandwidth, Search, Fetch, Extract, Agent runs, and model gateway usage have separate included allocations or overage rates.
- **Billing Granularity:**

  > Browser time is billed by the minute with a one-minute minimum per session; proxy bandwidth is billed by MB with a one-MB minimum per session. Browser hours and proxy bandwidth are allocations rather than hard caps. Plan capacity also limits concurrent browsers, maximum session duration, and session creation rate per minute. Keep-alive sessions must be explicitly released or they can continue billing until timeout.
- **Infrastructure Cost:**

  > Browserbase replaces local/container browser CPU, RAM, storage, browser fleet management, and observability infrastructure with hosted browser-hour billing. Over included allocation, listed browser infrastructure overage is $0.12/browser-hour on Developer and $0.10/browser-hour on Startup, plus proxy bandwidth if enabled. Scale pricing and Verified identity are custom.

#### Reliability

- **Failure Modes:**

  > - Expired job or closed ATS posting remains an application-level failure.<br>- Target-site policy or access restriction can still block automation.<br>- Authentication, MFA, OTP, passkey, or delegated credential flow may require human control.<br>- CAPTCHA or bot-protection failure can occur unless suitable Browserbase identity/proxy/Verified configuration is available and appropriate.<br>- Browserbase session creation, connection timeout, rate-limit, browser crash, or timeout can interrupt the run.<br>- Unsupported ATS fields, custom widgets, native dialogs, or file-download/upload misconfiguration can fail the adapter.<br>- Confirmation evidence may be absent or ambiguous even if the browser reached a final page.
- **Exactly Once Submit Safety:**

  > Not native to Browserbase. ApplyPilot must own pre-submit checkpoints, final-submit confirmation boundaries, duplicate detection, ambiguous-result quarantine, and reconciliation against email or ATS evidence. Browserbase artifacts can support audits but should not be treated as a submit transaction log.
- **Verification Evidence Tier:**

  > Browserbase can provide screenshot/video recording, session replay, CDP events, network requests/responses, console logs, session metadata, and downloaded files. Stronger ApplyPilot evidence such as ATS application IDs, confirmation DOM, HTTP response semantics, and email confirmations still must be captured by adapters or downstream reconciliation.

#### Applypilot Fit

- **Python Integration:**

  > Good. Browserbase has an official Python SDK, supports Python 3.9+, and provides examples for creating sessions and connecting Playwright through chromium.connect_over_cdp(session.connect_url). Selenium remote URLs are also documented. This matches ApplyPilot's Python runtime better than a Node-only hosted-browser provider.
- **Playwright Cdp Reuse:**

  > Good for Playwright/CDP adapter reuse: existing Playwright flows can switch from launching local Chromium to connecting over Browserbase's CDP URL and using the default context/page. It does not reuse existing local Chrome profiles or CDP ports directly; Browserbase Contexts are the hosted replacement for persistent profile state.
- **Migration Effort:**

  > Medium. Minimal proof-of-concept is a session-create wrapper plus Playwright connect-over-CDP. Production migration needs Browserbase project configuration, per-worker API secrets, context lifecycle, artifact retention controls, cost accounting, proxy/region settings, timeout/release cleanup, and canary routing in ApplyPilot.
- **Control Plane Overhead:**

  > Adds a Browserbase Sessions API call, remote CDP/WebSocket connection, cloud browser startup, cross-network Playwright round trips, artifact upload/retention, and session release handling. Keep-alive can reduce repeated startup and minimum-billing overhead for short bursts, but idle keep-alive time must be charged and controlled.
- **Observability Joinability:**

  > Strong if ApplyPilot records Browserbase session_id and userMetadata with attempt/job/worker IDs. Browserbase exposes status, duration, region, proxy bandwidth, settings, user metadata, extension ID, expiration, CDP events, network logs, console logs, recordings, replay API, and project usage API, making queue-to-browser-to-cost joins practical.

#### Authentication

- **Persistent Profiles:**

  > Supported through Browserbase Contexts. Contexts persist cookies, localStorage, IndexedDB, sessionStorage, service workers, form data, preferences, and related Chromium user-data state; contexts are encrypted at rest, live until deleted or invalidated, and should generally not be used simultaneously for the same login. The HTTP cache is not persisted.
- **Existing Session Attach:**

  > Can reconnect to an existing Browserbase keep-alive session using the same connect URL and sessionId-style workflow, but cannot safely attach to an already-authenticated local user Chrome tab/profile. For ApplyPilot, existing local session reuse would require migration into Browserbase Contexts or a separate local route.
- **Login And Otp:**

  > Browserbase supports a practical human-in-the-loop path: create a session with Contexts, proxies/fingerprinting as appropriate, use Live View for manual login or 2FA, persist the context, and reuse it later. OTP relay, mailbox polling, and account-owner takeover policy remain ApplyPilot responsibilities.
- **Extension Support:**

  > Supported. Browserbase can upload a zipped Chrome extension with manifest.json at the root, with documented SDK/API flows; current docs list a 100 MB maximum extension zip. This is suitable for narrowly permissioned content scripts if ApplyPilot controls secrets and extension IDs per environment.
- **Session Data Boundary:**

  > Cookies, tokens, localStorage, IndexedDB, form data, screenshots, recordings, network logs, console logs, downloads, and metadata can be transmitted to or retained in Browserbase-managed infrastructure when features are enabled. Default plan data retention is 7 days on Free/Developer and 30 days on Startup, with 30+ days on Scale. Enterprise docs state logging and recording can be disabled for zero data retention, but ApplyPilot must configure this explicitly and avoid recording sensitive flows without notices/consents.

#### Quality And Safety

- **Required Field Completeness:**

  > Browserbase alone does not identify all required ATS fields or validate answer completeness. It can expose DOM, screenshots, logs, and remote control surfaces that ApplyPilot or a higher-level agent can inspect, but refusal on incomplete plans must be implemented in ApplyPilot's adapter/agent layer.
- **Answer Provenance:**

  > Not native to hosted-browser infrastructure. ApplyPilot must constrain generated answers to approved profile, resume, and saved-answer evidence before filling fields. Browserbase recordings and logs can audit what was typed, but they do not prove answer provenance.
- **Unsupported Question Fallback:**

  > Must be implemented by ApplyPilot. Browserbase Live View gives a human takeover path for unknown or sensitive questions, but routing rules, redaction, pause/quarantine, and retry behavior belong in the ApplyPilot controller.
- **Prompt Injection Resistance:**

  > Browserbase can help with browser-level boundaries such as allowedDomains, isolated contexts, and disabling logs/recordings, but prompt injection resistance for page text, hidden instructions, tool access, and secrets must be handled by ApplyPilot's model/tool policy. Allowed Domains is documented as experimental, so it should be treated as defense-in-depth rather than the only control.
- **Site Permission And Terms:**

  > Browserbase terms require lawful, non-abusive use and place responsibility on the customer for privacy/security practices and notices/consents when using session recording. ApplyPilot still needs target-site and ATS-specific permission review. Browserbase Verified is positioned as partner-recognized automation rather than stealth bypass, but Scale/Verified terms should be reviewed before use.
- **Irreversible Action Guard:**

  > Browserbase has no application-specific final-submit guard. ApplyPilot should keep its own irreversible-action boundary: plan review, required-field check, screenshot/DOM checkpoint, duplicate check, user or policy approval where required, and post-submit evidence capture.

#### Operations

- **Concurrency:**

  > Current published plan limits: Free 3 concurrent browsers; Developer 25; Startup 100; Scale 250+. Session creation limits are Free 5/min, Developer 25/min, Startup 50/min, Scale 150+/min. Browserbase assigns concurrency at the organization level and distributes it across projects.
- **Warm Session Reuse:**

  > Supported through keepAlive sessions, reconnecting to the same Browserbase session, and Contexts for persistent login/application state. KeepAlive is paid-plan only and must be released to avoid idle browser-minute charges. Context reuse is suitable for authentication persistence, while keepAlive is suitable for short bursts and recovery.
- **Tracing And Replay:**

  > Strong managed observability. Browserbase provides Live View, automatic video recordings, HLS session replay API, status metadata, CDP events, network logs, console logs, session logs API, screenshots, downloads API, and dashboard/project usage analytics. rrweb DOM replay is being deprecated in favor of video recordings, so do not depend on rrweb for long-term ApplyPilot replay.
- **Deployment Modes:**

  > Managed cloud browser service with API/SDK control, plus Browserbase Functions for hosted serverless execution and a Browse CLI for terminal/browser workflows. No self-hosted Browserbase deployment was identified from current primary docs. ApplyPilot can run locally or on fleet workers while using Browserbase as a remote browser runtime.
- **Canary Stop Loss:**

  > Browserbase provides usage tracking, session metadata, project usage API, status/error breakdowns, and rate-limit signals that ApplyPilot can join into canary dashboards. Per-route dollar caps, timeout caps, failure-rate circuit breakers, and automatic rollback must be implemented in ApplyPilot's cost-quality router.

#### Benchmark

- **Benchmark Design:**

  > Run an ApplyPilot canary with matched ATS-stratified queues: Ashby, Greenhouse, Lever, Workable, Workday, and long-tail. For each job, run the current local route and Browserbase route where legally appropriate, recording Browserbase session_id, context_id, proxy usage, browser minutes, adapter route, model usage, final evidence tier, auth challenges, retries, and human takeover minutes.
- **Route Funnel:**

  > Recommended funnel: eligibility and policy check; deterministic adapter route selection; create Browserbase session with metadata and allowed domains where appropriate; restore context or request login takeover; execute adapter/agent; pause at pre-submit guard; submit only after ApplyPilot checks pass; capture Browserbase artifact URLs plus ATS/email evidence; reconcile final status; route failures to retry, human, or reject buckets.
- **Step Context Budget:**

  > For plain Playwright/CDP, context budget is mostly browser actions, DOM locators, screenshots, and captured artifacts rather than LLM tokens. If Stagehand/Agent is added, budget must include observations/screenshots, extraction schemas, model token usage, and action logs. Browserbase observability can record token usage for Stagehand calls.
- **Recommendation:**

  > Canary, not default-adopt. Browserbase is a strong comparator for managed browser sessions, persistent contexts, Live View, and observability, and it may be valuable for hard-to-debug ATS routes or remote-worker browser instability. It should not be adopted as a primary cost reducer until an ApplyPilot benchmark proves improved verified completion or lower recovery cost after browser-hour, proxy, model, and human time are included.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `browser_version_matrix`
- `cache_replay_yield`
- `confidence_intervals`
- `crash_recovery`
- `deterministic_coverage`
- `escape_rate`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `marginal_cost_per_attempt`
- `model_cost`
- `projected_cost_per_verified_apply`
- `startup_latency`
- `verification_latency`
- `verified_completion_rate`

### Browserless

Source record: `Browserless.json`

#### Identity

- **Name:** Browserless
- **Category:** hosted_browser
- **Official Sources:**

  > - https://www.browserless.io/<br>- https://www.browserless.io/pricing<br>- https://docs.browserless.io/overview/unit-consumption<br>- https://docs.browserless.io/baas/quick-start<br>- https://docs.browserless.io/baas/start<br>- https://docs.browserless.io/baas/session-management<br>- https://docs.browserless.io/baas/monitor-sessions/hybrid-automation<br>- https://docs.browserless.io/baas/monitor-sessions/screen-recording<br>- https://docs.browserless.io/baas/monitor-sessions/session-replay<br>- https://docs.browserless.io/enterprise/open-source<br>- https://docs.browserless.io/enterprise/long-queues<br>- https://docs.browserless.io/enterprise/changelog<br>- https://docs.browserless.io/baas/versions<br>- https://github.com/browserless/browserless<br>- https://github.com/browserless/browserless/blob/main/CHANGELOG.md<br>- https://www.browserless.io/terms-of-service
- **Maintenance Status:**

  > Active. Browserless maintains current v2 BaaS, Enterprise, and version-compatibility documentation; the open-source changelog lists current v2 releases and the Enterprise changelog lists ongoing v2.10x releases with Playwright, browser, credential, replay, and file-transfer updates. The public repository and container line have material maintenance after January 2025.
- **License:**

  > The Browserless repository states SPDX-License-Identifier: SSPL-1.0 OR Browserless Commercial License. For ApplyPilot's AGPL-3.0 codebase, using Browserless as a separate hosted service is operationally simpler than embedding or redistributing the SSPL-licensed server; commercial self-hosting should be treated as a license/procurement item.

#### Cost Economics

- **Pricing Model:**

  > Usage-based hosted plans plus self-hosting. Public annual-billed plans list Free at 1k units/month and 2 concurrent browsers, Prototyping at $25/month for 20k units and 10 concurrent browsers, Starter at $140/month for 180k units and 40 concurrent browsers, Scale at $350/month for 500k units and 100 concurrent browsers, and Enterprise custom. One unit is up to 30 seconds of browser time per browser connection; overages are $0.0020/unit on Prototyping, $0.0017/unit on Starter, and $0.0015/unit on Scale. Proxy traffic and CAPTCHA solving consume extra units.

#### Reliability

- **Crash Recovery:**

  > Browserless provides remote browser isolation, automatic session timeout, queueing, health/pressure endpoints, paid recording/replay, LiveURL handoff, and persistent-session APIs. It can preserve state for reconnectable or persisted sessions, but ApplyPilot still needs attempt-level checkpoints, idempotency keys, and ambiguous-result quarantine after a browser or connection failure.
- **Exactly Once Submit Safety:**

  > Browserless does not provide exactly-once application submission semantics. It can keep or replay browser evidence, but duplicate detection, pre-submit checkpoints, post-submit verification, and quarantine of unknown outcomes must remain in ApplyPilot's queue and verifier layers.
- **Verification Evidence Tier:**

  > Browserless can supply browser-side evidence such as DOM state, screenshots, WebM screen recordings, session replay with DOM mutations, console logs, and network requests. It does not inherently provide ATS-side application IDs, email confirmations, or first-party submission receipts unless ApplyPilot captures those through the page, network, or mailbox.

#### Applypilot Fit

- **Python Integration:**

  > Good. Browserless documents Playwright Python via chromium.connect_over_cdp to a Browserless WebSocket endpoint, and any CDP-compatible Python library can connect to the remote browser. This fits ApplyPilot's Python runtime if browser launch is abstracted behind a provider.
- **Playwright Cdp Reuse:**

  > Good for remote CDP reuse. Existing Playwright code can replace local launch with connect_over_cdp to Browserless, and official docs state minimal code changes for Puppeteer or Playwright. It does not reuse an already authenticated local Chrome profile directly; it reuses Browserless-hosted sessions, profiles, or user data directories.
- **Observability Joinability:**

  > Moderate to good if instrumented. Browserless supports session IDs, live viewing, screen recording, session replay, queue/health surfaces, webhooks in Enterprise materials, and OpenTelemetry in Enterprise Docker. ApplyPilot still needs to attach queue item ID, attempt ID, Browserless session URL, network trace, screenshot/video artifact, and email/ATS evidence into one attempt record.

#### Authentication

- **Persistent Profiles:**

  > Supported but operationally constrained. Browserless session persistence can preserve cookies, localStorage, cache, and authentication in isolated userDataDir-backed sessions for days, and launch parameters can load saved profiles. User data directories are single-browser locked and, on dedicated or self-hosted multi-worker fleets, stored locally per worker, so profile routing must keep each profile on the worker where it was created.
- **Existing Session Attach:**

  > Browserless can reconnect to Browserless-managed sessions and persisted profiles, but it is not a safe direct attach path to the user's existing local authenticated Chrome tab. For ApplyPilot accounts already logged into local Chrome, Browserless requires a separate authenticated Browserless profile or a supported human login handoff.
- **Login And Otp:**

  > Supported as a human-in-the-loop pattern on paid plans through Hybrid Automation LiveURL. Browserless can create a short-lived live URL without exposing the API token, allow a human to click/type/scroll, and resume automation after Browserless.liveComplete or a programmatic close. ApplyPilot must still own OTP routing, owner notification, and sensitive-step policy.
- **Extension Support:**

  > Browserless pricing and launch docs list Chrome Extensions and ad-block extension support. For ApplyPilot, extension use should be narrowly permissioned and tested because extensions can increase startup cost, affect page behavior, and widen the session-data boundary.
- **Session Data Boundary:**

  > With hosted Browserless, browser traffic, cookies, localStorage, screenshots, recordings, replay data, downloaded files, and traces may be processed or retained by Browserless according to plan storage windows and account settings. Self-hosting narrows that boundary to ApplyPilot-controlled infrastructure, but SSPL/commercial licensing and operations then matter. LiveURL links avoid embedding the API token, but they expose an interactive session during their short lifetime.

#### Quality And Safety

- **Required Field Completeness:**

  > Browserless as BaaS does not identify required application fields or refuse incomplete application plans. Required-field completeness must come from ApplyPilot's ATS adapters, DOM parser, validation rules, or higher-level agent.
- **Answer Provenance:**

  > Browserless does not constrain answers to resume/profile evidence. It only executes browser actions and captures evidence. ApplyPilot must enforce answer provenance before fields are filled and again before irreversible submit.
- **Unsupported Question Fallback:**

  > Browserless can hand control to a human through LiveURL, but it does not decide whether a question is unsupported, sensitive, or unsafe. ApplyPilot must classify unsupported questions and choose human fallback, skip, or quarantine.
- **Prompt Injection Resistance:**

  > Browserless BaaS is not an agent policy layer. It can help isolate browser processes and tokens, but prompt-injection resistance depends on ApplyPilot tool restrictions, domain allowlists, secret boundaries, content filtering, and hostile-page regression tests. Do not expose Browserless API tokens or ApplyPilot secrets to page scripts.
- **Site Permission And Terms:**

  > Use requires both Browserless terms and target-site permissions. Browserless offers automation and scraping-oriented products, but its terms include broad prohibited-use language around unlawful use, false or misleading information, collecting/tracking personal information, spam/phish/pretext/spider/crawl/scrape, and circumventing security features. For job applications, operate only where ApplyPilot has authorization and do not use Browserless stealth, proxy, or CAPTCHA features to bypass access controls.
- **Irreversible Action Guard:**

  > Browserless has no built-in final-submit policy boundary. ApplyPilot should keep the final submit action behind route-specific guards: required-field check, answer-provenance check, duplicate check, user/account policy check, screenshot/network checkpoint, and post-submit verifier registration.

#### Operations

- **Concurrency:**

  > Public cloud plans list 2, 10, 40, and 100 max concurrent browsers for Free, Prototyping, Starter, and Scale, with Enterprise offering hundreds or thousands by custom agreement. Self-hosted Docker exposes CONCURRENT for maximum concurrent sessions and QUEUED for queue size; defaults are documented as 10 concurrent and 10 queued requests.
- **Warm Session Reuse:**

  > Supported through Browserless reconnectable sessions, Session API persisted state, userDataDir/profile options, and Enterprise/private deployment features such as keeping browsers warm. Reconnects may incur new unit charges and session lifetimes are plan-bound, so warm reuse must be balanced against idle billing and profile-lock risk.
- **Tracing And Replay:**

  > Good on paid or Enterprise paths. Browserless supports screenshots/PDF/content APIs, LiveURL monitoring, WebM screen recording, dashboard session replay with DOM mutations, mouse/keyboard/scroll events, console logs and network requests, and Enterprise OpenTelemetry/log integrations. Deterministic replay of ApplyPilot decisions still needs ApplyPilot-side action logs.
- **Deployment Modes:**

  > Hosted multi-region cloud, public open-source Docker images on GHCR, Enterprise Docker for self-hosting, private managed deployments, and hybrid cloud-to-self-hosted patterns are documented. Open-source Docker covers core browser automation; Enterprise adds production features such as session recording, live debugging, webhooks, OpenTelemetry, support, and commercial licensing.
- **Browser Version Matrix:**

  > Browserless v2 supports Chromium, Chrome, Firefox, WebKit, and Edge paths. BaaS docs list Chromium, Chrome, stealth, Firefox Playwright, WebKit Playwright, and Edge Playwright endpoints; open-source Docker images include chromium, chrome, firefox, webkit, edge, and multi images. Chrome and Edge have ARM limitations in open-source Docker.
- **Canary Stop Loss:**

  > Browserless provides timeouts, max session duration, concurrency limits, queue limits, health checks, and usage/billing limits by plan, but ApplyPilot still needs per-route canary limits, max dollars per route, max duplicate-risk attempts, failure-rate breakers, and automatic disabling when verified cost or ambiguity exceeds thresholds.

#### Benchmark

- **Benchmark Design:**

  > Run Browserless as an infrastructure comparator against local Playwright CDP on matched ATS-stratified samples: Ashby, Greenhouse, Lever, Workable, Workday, and long-tail hosts. Record unit consumption, session duration, retry count, profile/login state, required-field completeness, final-submit guard status, confirmation evidence tier, and delayed verifier outcome.
- **Route Funnel:**

  > Eligible if the route already uses deterministic Playwright or a safe browser-agent fallback and target-site/account policy allows a hosted browser. Selected route opens a Browserless session, fills the application under existing ApplyPilot guards, captures pre-submit evidence, submits only after irreversible-action checks, then reconciles DOM/network/email evidence. Failures go to retry, human LiveURL, local-browser fallback, or ambiguous-result quarantine.
- **Recommendation:**

  > Canary, not default adoption. Browserless is a strong hosted/self-hosted CDP-compatible browser infrastructure comparator because it fits Playwright with minimal code changes, provides concurrency and session tooling, and has useful human recovery and observability features. Do not use it as a quality router by itself; gate it behind explicit allowed-site policy, session-data review, exactly-once submit guards, cost caps, and an ATS-stratified benchmark proving lower all-in cost per positively verified application.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `billing_granularity`
- `cache_replay_yield`
- `confidence_intervals`
- `control_plane_overhead`
- `deterministic_coverage`
- `escape_rate`
- `failure_modes`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `model_cost`
- `projected_cost_per_verified_apply`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`

### Cloudflare Browser Run (formerly Browser Rendering)

Source record: `Cloudflare_Browser_Rendering_and_Browser_Run.json`

#### Identity

- **Name:** Cloudflare Browser Run (formerly Browser Rendering)
- **Category:** hosted_browser
- **Official Sources:**

  > - https://developers.cloudflare.com/browser-run/<br>- https://developers.cloudflare.com/browser-run/pricing/<br>- https://developers.cloudflare.com/browser-run/limits/<br>- https://developers.cloudflare.com/browser-run/cdp/playwright/<br>- https://developers.cloudflare.com/browser-run/features/reuse-sessions/<br>- https://developers.cloudflare.com/browser-run/features/human-in-the-loop/<br>- https://developers.cloudflare.com/browser-run/features/session-recording/<br>- https://developers.cloudflare.com/browser-run/faq/<br>- https://developers.cloudflare.com/browser-run/changelog/
- **Maintenance Status:**

  > Active. Cloudflare renamed Browser Rendering to Browser Run in April 2026 and added standard CDP, external Playwright/Puppeteer connectivity, Live View, human takeover, session recording, WebMCP, and higher limits during 2025-2026.
- **License:**

  > Managed Cloudflare service governed by Cloudflare service terms. Client libraries have their own open-source licenses, but using the hosted API from ApplyPilot does not alter ApplyPilot's AGPL-3.0 license; account terms, data-processing terms, and target-site permissions still apply.

#### Cost Economics

- **Pricing Model:**

  > Workers Free includes 10 browser minutes per day and 3 concurrent Browser Sessions. Workers Paid includes 10 browser hours per month and 10 average concurrent browsers, then charges $0.09 per additional browser hour and $2 per additional average concurrent browser per month. Quick Actions incur browser-hour charges only; Browser Sessions using Playwright, Puppeteer, or CDP incur browser-hour and concurrency charges.
- **Billing Granularity:**

  > Cloudflare totals browser use daily in seconds, then totals the cycle and rounds monthly browser hours to the nearest whole hour. Quick Action timeout failures described in the pricing FAQ are not charged. Browser Sessions can continue billing until explicitly closed or their inactivity timeout fires. Concurrency is the monthly average of each day's peak, with 10 included on Workers Paid.
- **Model Cost:**

  > Browser Run used through Playwright, Puppeteer, or CDP has no required model cost. Stagehand, Workers AI extraction, MCP-agent use, or an ApplyPilot answerer/verifier adds separate model cost that must be metered by route.
- **Infrastructure Cost:**

  > Cloudflare supplies managed headless Chrome, global capacity, browser lifecycle, CDP endpoints, dashboard runs, and optional recordings. Cash infrastructure overage is $0.09/browser-hour plus concurrency above the included monthly average; Workers, Durable Objects, Queues, storage, and outbound services may add separate Cloudflare charges.

#### Reliability

- **Failure Modes:**

  > - Expired, duplicate, ineligible, or policy-denied job remains an application-level failure.<br>- Bot-identifying Cloudflare headers or IP ranges can trigger target-site denial.<br>- Login, MFA, OTP, CAPTCHA, or account ownership can require human takeover.<br>- Remote CDP disconnect, browser rollout, inactivity timeout, rate limit, or browser crash can interrupt a run.<br>- Unsupported required fields, file controls, dialogs, or cross-origin widgets can defeat an adapter.<br>- Final submission can remain ambiguous without ATS, network, DOM, or email confirmation.
- **Exactly Once Submit Safety:**

  > Not native. ApplyPilot must own deduplication, a pre-submit checkpoint, an irreversible-action guard, final-response capture, ambiguous-result quarantine, and delayed email or ATS reconciliation. A Browser Run session ID or recording is evidence, not a transaction guarantee.
- **Verification Evidence Tier:**

  > Browser Run can expose confirmation DOM, URL, screenshots, CDP network and console events, Live View, run metadata, and opt-in rrweb recordings. The strongest tiers remain an ATS application ID or authoritative response, followed by confirmation DOM and email; screenshots alone are weaker.

#### Applypilot Fit

- **Python Integration:**

  > Partial to good. ApplyPilot Python can use Playwright's chromium.connect_over_cdp against Cloudflare's WebSocket endpoint even though Cloudflare's first-party Browser Run packages and examples are primarily TypeScript/Node. Python must manage authorization headers, reconnects, and cleanup directly.
- **Playwright Cdp Reuse:**

  > Good for Playwright/CDP script reuse through the external WebSocket endpoint. It cannot attach to ApplyPilot's existing local Chrome port or move an existing local profile in place; remote session state and contexts must be managed separately.
- **Migration Effort:**

  > Medium. A proof of concept only needs a provider launcher and connect-over-CDP path. Production use needs token distribution, session and keep-alive policy, local-auth fallback, recording/redaction policy, cost accounting, route metadata, release discipline, and matched canaries.
- **Control Plane Overhead:**

  > Adds API authentication, remote WebSocket/CDP round trips, browser acquisition, and explicit close/reconnect handling. Reusing sessions and tabs reduces cold starts but increases isolation and idle-billing risks. Node-first Cloudflare wrappers may add a bridge if ApplyPilot does not connect directly from Python.
- **Observability Joinability:**

  > Good if ApplyPilot persists Cloudflare session/run IDs with attempt, job, worker, route, and cost metadata. Dashboard Runs, close reasons, Live View, CDP events, screenshots, and recording API data can then join browser evidence to queue and email outcomes.

#### Authentication

- **Existing Session Attach:**

  > Supports reconnecting to a still-running Cloudflare session and attaching external CDP clients. It does not attach to an existing authenticated local Chrome tab/profile; keep the owner/local browser route for that requirement.
- **Login And Otp:**

  > Human in the Loop can expose a five-minute Live View URL so an operator can complete login, MFA, CAPTCHA, or sensitive entry and return control. OTP mailbox relay, account ownership, session persistence, notifications, and final policy remain ApplyPilot responsibilities.
- **Session Data Boundary:**

  > For Quick Actions except crawl, Puppeteer, Playwright, and CDP, Cloudflare says submitted HTML and generated output are processed ephemerally and discarded after response. Crawl results are retained 14 days. Opt-in recordings retain structured DOM/events for 30 days, mask input values by default, and can omit cross-origin frames/canvas; credentials, cookies, resumes, and screenshots still transit Cloudflare infrastructure during execution.

#### Quality And Safety

- **Required Field Completeness:**

  > Not provided by the hosted browser. ApplyPilot must enumerate required controls, detect unmapped or hidden validation errors, and refuse incomplete plans before final submit.
- **Answer Provenance:**

  > Not native. ApplyPilot must bind every answer to approved profile, resume, or saved-answer evidence and log source IDs. Browser recordings can audit interactions but do not prove factual provenance.
- **Unsupported Question Fallback:**

  > ApplyPilot should pause or route unknown, sensitive, and free-text questions to its bounded answerer or human review. Live View supplies a takeover surface but not the decision policy.
- **Prompt Injection Resistance:**

  > Browser Run does not neutralize hostile page content. ApplyPilot must enforce domain allowlists, untrusted-DOM handling, restricted tools, secret isolation, bounded extraction, and a hard final-submit boundary. Cloudflare's identifiable browser headers are useful for transparency, not injection defense.
- **Site Permission And Terms:**

  > Use only for permitted automation. Cloudflare explicitly identifies Browser Run traffic as bot traffic and provides Web Bot Auth signals; target ATS terms, robots/access rules, candidate consent, and employer account policy remain ApplyPilot's responsibility.
- **Irreversible Action Guard:**

  > No ATS-specific guard is built in. Require ApplyPilot's complete plan, dedup check, policy check, pre-submit evidence checkpoint, optional user approval, one final action, and authoritative post-submit capture.

#### Operations

- **Concurrency:**

  > Workers Free allows 3 concurrent browsers. Workers Paid defaults to 120 concurrent browsers per account and one new browser instance per second; higher limits can be requested. Billing includes 10 average concurrent browsers and charges $2/month for each additional monthly average concurrent browser.
- **Warm Session Reuse:**

  > Supported. Sessions can remain active indefinitely if commands arrive within the inactivity window; default inactivity timeout is 60 seconds and keep_alive can extend it to 10 minutes. Reuse sessions or multiple tabs carefully and isolate jobs with incognito contexts.
- **Tracing And Replay:**

  > Good but still maturing. Live View shows page, DOM, console, and network activity. Opt-in beta recordings capture rrweb DOM/events rather than pixels, are available after close, retain 30 days, and exclude canvas, cross-origin iframe content, media pixels, and WebGL.
- **Deployment Modes:**

  > Managed Cloudflare edge browser through Workers bindings, Quick Actions REST, external CDP from local/CI/server environments, and local development through Wrangler/Vite. No self-hosted Browser Run service is offered.
- **Browser Version Matrix:**

  > Hosted Chrome/Chromium with Puppeteer, Playwright, CDP, Stagehand, and MCP integrations. Cloudflare's Playwright package tracked Playwright 1.58.2 in March 2026. No Firefox, WebKit, or WebDriver BiDi route was confirmed.
- **Canary Stop Loss:**

  > Use Cloudflare usage headers for Quick Actions, dashboard usage, session close reasons, explicit close, keep-alive limits, and account concurrency. ApplyPilot must add per-route browser-minute and dollar caps, retry caps, failure-rate breakers, and automatic rollback.

#### Benchmark

- **Benchmark Design:**

  > Run matched ATS-stratified local-versus-Cloudflare trials on Ashby, Greenhouse, Lever, Workable, Workday, and long-tail hosts. Hold job sample, adapter, model, profile state, and submit policy constant; record session ID, browser seconds, concurrency allocation, bot denial, retries, evidence tier, fallback, and human minutes.
- **Route Funnel:**

  > Eligibility and terms check -> deterministic route selection -> Cloudflare canary gate -> acquire or reuse isolated session -> required-field and provenance checks -> pre-submit guard -> one submit -> DOM/network evidence -> delayed email reconciliation -> verified, ambiguous quarantine, fallback, or reject.
- **Recommendation:**

  > Canary. Cloudflare is the strongest low-browser-hour-price burst comparator, but identifiable bot traffic, uncertain durable-profile support, and Node-first product ergonomics make it unsuitable as an immediate default. Test a small stateless or low-auth Playwright/CDP slice with strict cost and evidence joins before any expansion.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `cache_replay_yield`
- `confidence_intervals`
- `crash_recovery`
- `deterministic_coverage`
- `escape_rate`
- `extension_support`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `marginal_cost_per_attempt`
- `persistent_profiles`
- `projected_cost_per_verified_apply`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`

### Steel

Source record: `Steel.json`

#### Identity

- **Name:** Steel
- **Category:** hosted_browser
- **Official Sources:**

  > - https://steel.dev/<br>- https://steel.dev/blog/pricing-update<br>- https://docs.steel.dev/<br>- https://docs.steel.dev/overview/pricinglimits<br>- https://docs.steel.dev/overview/sessions-api/session-lifecycle<br>- https://docs.steel.dev/overview/profiles-api/overview<br>- https://docs.steel.dev/overview/sessions-api/reusing-auth-context<br>- https://docs.steel.dev/overview/sessions-api/human-in-the-loop<br>- https://docs.steel.dev/overview/agent-traces/overview<br>- https://docs.steel.dev/integrations/playwright<br>- https://docs.steel.dev/integrations/selenium<br>- https://docs.steel.dev/overview/self-hosting/docker<br>- https://docs.steel.dev/overview/self-hosting/steel-local-vs-steel-cloud<br>- https://docs.steel.dev/overview/legal<br>- https://github.com/steel-dev/steel-browser<br>- https://github.com/steel-dev/steel-browser/releases
- **Maintenance Status:**

  > Active. The public steel-dev/steel-browser repository was created in November 2024, is Apache-2.0 licensed, had about 7.3k stars, was pushed on 2026-07-06, and its latest listed release was v0.5.3-beta on 2026-04-24. Recent commits in June and July 2026 include network instrumentation, CDP/fingerprint fallback fixes, request-header cleanup, and log durability fixes.
- **License:**

  > Apache-2.0 for the open-source steel-browser repository. This permissive license is compatible with ApplyPilot's AGPL-3.0 runtime as an external dependency or self-hosted service, but Steel Cloud usage is also governed by Steel's hosted service terms and privacy policy.

#### Cost Economics

- **Model Cost:**

  > Steel itself is browser infrastructure, not the answering model. Model cost remains whatever ApplyPilot uses for the actor, answerer, verifier, or fallback agent. If Steel is used only as a remote CDP browser for current Playwright/MCP or Selenium flows, expected model spend is unchanged; only lower failure/retry rates or deterministic adapter routing reduce model cost.

#### Reliability

- **Failure Modes:**

  > Expected failure modes include provider session creation failure, timeout or idle leakage, cloud/browser crash, CDP or WebDriver connection loss, profile upload or persistence failure, auth gate, OTP wait, captcha or challenge, proxy/network reputation mismatch, unsupported required fields, agent no-result, ambiguous submit state, missing confirmation evidence, and site policy restrictions. Steel does not by itself solve ApplyPilot's answer-quality or exactly-once problems.
- **Crash Recovery:**

  > Steel sessions have Live, Released, and Failed states; failed sessions are automatically cleaned up. Explicit release and releaseAll are available, and Agent Traces expose a timeline, video sync, details, and markdown/JSON/ZIP export for debugging. This is good for post-mortem and replay-as-code workflows, but deterministic resume after a mid-form crash still has to be implemented by ApplyPilot.
- **Exactly Once Submit Safety:**

  > Weak by default. Steel provides browser execution and evidence capture, not application-level duplicate detection. ApplyPilot must keep its existing dedup_key/applied_set guards, pre-submit checkpoints, final-action boundary, positive confirmation requirement, and crash_unconfirmed quarantine. Steel traces can help adjudicate ambiguous outcomes but should not be the source of truth for marking applied.
- **Verification Evidence Tier:**

  > Potential evidence tiers are confirmation DOM and URL, screenshots, video, console/network logs, Agent Trace timeline, and exported trace JSON; delayed email confirmation remains outside Steel and must be joined through ApplyPilot's email/outcome reconciliation. ATS application IDs are available only if the ATS exposes them in page, network, or email evidence.

#### Applypilot Fit

- **Python Integration:**

  > Good. The docs show a Python SDK using `from steel import Steel`, Playwright Python via `chromium.connect_over_cdp`, and Selenium 4 Python via a Steel WebDriver endpoint with per-request API-key and session headers. This is compatible with ApplyPilot's Python runtime, though async/sync Playwright choices and lifecycle cleanup must be wired carefully.
- **Playwright Cdp Reuse:**

  > Partial. Existing Playwright logic can connect to a Steel session over CDP, but the endpoint is Steel's cloud WSS URL rather than ApplyPilot's local Chrome CDP port. Local Chrome profiles are not reused in place; Steel provides session context capture and Profiles API for portable state, with separate storage, size, and retention constraints.
- **Migration Effort:**

  > Medium. A narrow canary can be added as a remote browser provider behind the existing Playwright/launcher path, recording Steel session ID, debug URL, trace URL, browser provider, and per-session cost. Broad adoption requires profile migration, data-boundary review, timeout/release discipline, hosted-service cost caps, and comparison against local Chrome failure modes.
- **Control Plane Overhead:**

  > Steel adds session create/release API calls, remote CDP or WebDriver round trips, and cloud trace/profile serialization. It removes local Chrome launch/display management and can make debugging easier through live viewer and traces. Selenium over Steel adds HTTP round trips per WebDriver command; Playwright over CDP should be preferred for existing ApplyPilot-style action density.
- **Observability Joinability:**

  > Strong if instrumented. Steel session IDs, debug URLs, traces, video, network/console logs, profile IDs, and API costs can be joined to ApplyPilot's apply_queue, apply_result_events, llm_usage, worker_id, route, and email evidence. ApplyPilot must persist those IDs explicitly; otherwise Steel's dashboard evidence will not be queryable with queue outcomes.

#### Authentication

- **Persistent Profiles:**

  > Steel Profiles API can persist auth, cookies, extensions, credentials, and browser settings across sessions by saving the browser user data directory. The docs note a 300 MB profile size limit, FAILED state if upload fails, and automatic deletion after 30 days of non-use. Persisting updates is not automatic unless sessions are started with the right persistProfile/profileId behavior.
- **Existing Session Attach:**

  > Steel supports attaching automation and viewers to Steel-created sessions through WebSocket/CDP, WebDriver, sessionViewerUrl, and debugUrl. It is not a safe direct attach mechanism for the user's already-authenticated local Chrome tab or profile; ApplyPilot would need to create or import managed Steel profile state instead.
- **Extension Support:**

  > Supported but beta in Steel Cloud through the Extensions API. Extensions can be uploaded as .zip/.crx or from Chrome Web Store and attached to sessions; Steel Local can load extensions from the local extension folder. This is promising for narrowly permissioned ApplyPilot instrumentation, but beta status requires canary isolation.

#### Quality And Safety

- **Required Field Completeness:**

  > Steel does not provide required-field completeness by itself. ApplyPilot adapters, DOM inspectors, answer planners, and verifiers must enumerate required fields, detect unmapped controls, and refuse incomplete plans before final submit. Steel's traces help audit failures after the fact.
- **Answer Provenance:**

  > Steel has no built-in guarantee that answers come from the user's resume, profile, or approved-answer corpus. ApplyPilot must keep answer provenance constraints in the answerer/verifier layer and should log answer source IDs alongside any Steel session evidence.
- **Unsupported Question Fallback:**

  > Unsupported, sensitive, or free-text questions should fall back to ApplyPilot's verified answerer, premium agent, or human review. Steel can provide an interactive browser for human correction, but the policy for refusing or escalating unsupported answers remains entirely ApplyPilot-owned.
- **Prompt Injection Resistance:**

  > Steel is a browser control layer, so prompt-injection resistance depends on the agent and tool policy wrapped around it. ApplyPilot should keep domain allowlists, no-secret-in-DOM rules, restricted extensions, minimal tool permissions, hostile-page tests, and a hard boundary around credentials and final submit actions.
- **Irreversible Action Guard:**

  > Steel does not supply an application-domain submit guard. ApplyPilot must enforce pre-submit checkpoints, complete answer plan validation, deduplication, route policy, canary caps, and positive confirmation capture. Human takeover should be available for uncertain flows, and crash_unconfirmed rows should stay quarantined.

#### Operations

- **Warm Session Reuse:**

  > Supported through session context capture and Profiles API. Context transfer can preserve cookies/local storage across sessions, while Profiles persist the broader browser user data directory. Sessions should still be short-lived and explicitly released; idle waiting can be controlled with inactivityTimeout.
- **Tracing And Replay:**

  > Good for observability. Agent Traces provide a timeline of activities with page URL and timestamp, video sync, details, and export as markdown, JSON, or ZIP with screenshots. This supports debugging, audit, and reproduction-as-code, but it is not a deterministic replay engine and needs ApplyPilot-side evidence joins.
- **Deployment Modes:**

  > Managed Steel Cloud, Docker self-hosting, one-click/hosted templates such as Railway or Render, local Docker Compose, and enterprise/custom cloud arrangements. Self-hosted mode can expose API and CDP ports locally; Steel Cloud provides higher concurrency, multi-region, managed proxies, credentials, files, and advanced cloud features.
- **Browser Version Matrix:**

  > Primarily Chrome/Chromium. Steel supports Playwright and Puppeteer through CDP and Selenium through a W3C WebDriver-compatible endpoint for Steel sessions. It is not a Firefox or WebKit route for ApplyPilot and should be compared as a Chromium provider.
- **Canary Stop Loss:**

  > Steel provides timeout, inactivityTimeout, session release, plan limits, and concurrency controls. ApplyPilot still needs per-route budget caps, max session minutes, max failed attempts, host-policy parking, canary remaining counts, no-submit-on-uncertain-result, and automatic halt on trace/session creation failures.

#### Benchmark

- **Benchmark Design:**

  > Run a matched ATS-stratified benchmark against the current local Chrome route: Greenhouse, Ashby, Lever, Workable, Workday tenants, and long-tail hosts. Hold model, prompt, queue sample, profile state, and submit policy constant; compare local Playwright/CDP versus Steel Playwright/CDP and, optionally, Steel Selenium. Include synthetic forms for required fields, uploads, confirmation pages, login gates, and crash recovery.
- **Route Funnel:**

  > Eligible job -> host policy -> local deterministic adapter or Steel canary route -> profile/context selection -> session create and attach -> pre-submit plan completeness check -> final submit or refusal -> confirmation evidence capture -> email reconciliation when needed -> final disposition. Any auth gate, unsupported required field, missing confirmation, or session crash should park, fallback, or quarantine rather than blind retry.
- **Recommendation:**

  > Canary, not broad adoption. Use Steel as a hosted-browser comparator and possible failover for remote browser stability, observability, and human takeover; do not expect it to beat deterministic ATS adapters on cost. The first production candidate is a small Steel Playwright/CDP canary on a stable ATS slice with strict cost caps, trace joins, and no relaxation of ApplyPilot's exactly-once submit safeguards.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `billing_granularity`
- `cache_replay_yield`
- `concurrency`
- `confidence_intervals`
- `deterministic_coverage`
- `escape_rate`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `login_and_otp`
- `marginal_cost_per_attempt`
- `pricing_model`
- `projected_cost_per_verified_apply`
- `session_data_boundary`
- `site_permission_and_terms`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`

### AgentQL semantic locator layer

Source record: `AgentQL_semantic_locator_layer.json`

#### Identity

- **Name:** AgentQL semantic locator layer
- **Category:** model_assisted
- **Official Sources:**

  > - https://github.com/tinyfish-io/agentql<br>- https://docs.agentql.com/release-notes<br>- https://www.agentql.com/pricing<br>- https://docs.agentql.com/concepts/under-the-hood<br>- https://docs.agentql.com/python-sdk/api-references/agentql-page<br>- https://docs.agentql.com/automation/elements<br>- https://docs.agentql.com/automation/submit-form<br>- https://docs.agentql.com/browser/access-open-tab<br>- https://docs.agentql.com/browser/remote-browser<br>- https://docs.agentql.com/logging-into-sites/caching-user-credentials<br>- https://www.agentql.com/legal/terms-of-service<br>- https://www.agentql.com/legal/privacy-policy
- **Maintenance Status:**

  > Actively maintained. Current official release notes list SDK version 1.19.0 with recent remote-browser, session-reuse, proxy, iframe, and security dependency work. The repository was updated in May 2026, although GitHub releases are not used; version history is maintained in the documentation.
- **License:**

  > The public Python/JavaScript SDK and examples repository is MIT licensed, which is permissive and compatible with ApplyPilot's AGPL-3.0 distribution. The hosted AgentQL API, models, and Tetra browser remain proprietary services governed by the April 21, 2026 Terms; the service grant is limited to internal business use and cannot be sublicensed or resold.

#### Cost Economics

- **Pricing Model:**

  > Starter is $0/month with 50 API calls/month included, then $0.02 per AgentQL API call, 10 calls/minute, 10 remote-browser hours included, then $0.12/browser-hour, and 5 concurrent remote sessions. Professional is $99/month with 10,000 calls included, then $0.015/call, 50 calls/minute, 500 remote-browser hours included, then $0.10/browser-hour, and 100 concurrent remote sessions. The free trial includes 300 API calls and one remote-browser hour. Enterprise is custom and advertises dedicated cloud and on-premises deployment.
- **Billing Granularity:**

  > Each AgentQL query is one API call regardless of how many terms are batched into that query, subject to plan-included calls and per-minute rate limits. Remote browsers are billed by browser hour after included hours; public pricing does not state rounding interval, idle rounding, data egress, screenshot, proxy, storage, or failed-call credit policy. Local browser use avoids remote-browser time but not AgentQL query charges.
- **Model Cost:**

  > The hosted model cost is bundled per AgentQL API call rather than exposed as tokens. Official architecture says AgentQL selects among public GPT-4, Llama, Gemini, and proprietary models based on task complexity. Fast and Standard modes have the same public per-call price but trade speed for accuracy. Enterprise on-premises model and token economics are custom.

#### Reliability

- **Failure Modes:**

  > Element terms can resolve to `None`, resolve to the wrong similar control, miss hidden or conditional required fields, or become stale after navigation. Additional failures include fast-mode accuracy loss, backend timeout or 500, accessibility-tree generation defects, iframe or Unicode edge cases, Playwright actionability timeout, page crash, API-key/rate-limit failure, network loss, and a semantic match to the wrong irreversible button. Release notes show prior fixes in several of these areas.
- **Exactly Once Submit Safety:**

  > AgentQL is only a locator and does not click unless caller code invokes Playwright, which makes a strong separation possible. It has no native idempotency or duplicate-application protocol. ApplyPilot should permit semantic lookup and form fill, then require a deterministic complete-plan checkpoint and a separately authorized single submit click; post-click uncertainty must remain `crash_unconfirmed`.

#### Applypilot Fit

- **Python Integration:**

  > Strong. The maintained Python SDK wraps synchronous or asynchronous Playwright `Page` objects and supports Python >=3.9, compatible with ApplyPilot's Python >=3.11 runtime. Returned values are normal Playwright Locators, so existing fill, select, upload, wait, network, trace, and confirmation code can remain in Python.
- **Playwright Cdp Reuse:**

  > Strong. Official docs show `wrap`/`wrap_async` over an existing Playwright page, Chrome CDP attachment to an open browser for preserved cookies/session state, Playwright `storage_state` reuse, and remote CDP sessions. This is a direct fit for ApplyPilot's existing Chrome profiles, CDP ports, Playwright fixtures, and deterministic final-submit code.
- **Observability Joinability:**

  > Strong with a small adapter. AgentQL exposes backend request IDs and last query/response/accessibility-tree data; debug logging and screenshots are available, while existing Playwright supplies trace, DOM, network, screenshot, and video. Persist ApplyPilot attempt ID, AgentQL request ID, query version/hash, mode, returned element metadata, action evidence, route, fallback reason, and confirmation tier in the same result event.

#### Authentication

- **Persistent Profiles:**

  > Local profile persistence uses normal Playwright mechanisms: save `storage_state` and load it into a new context, or attach over CDP to an existing Chrome profile. Tetra remote sessions can remain alive across disconnects until an inactivity timeout, but its `BrowserProfile` names such as LIGHT/STEALTH/TF_BROWSER describe runtime configuration, not documented durable authenticated storage snapshots. Keep durable candidate profiles under ApplyPilot control.
- **Existing Session Attach:**

  > Strong. Official examples connect over CDP to an open Chrome browser, select its existing context, create or use a page, then wrap it with AgentQL. This preserves cookies and login state. ApplyPilot must keep its existing port ownership, profile lock, and one-account concurrency controls.
- **Login And Otp:**

  > AgentQL can operate behind authentication and documents login plus Playwright storage-state caching, but it does not provide a first-class OTP relay, password vault, or owner-takeover workflow. Reuse ApplyPilot's current profile, inbox/OTP, and supervised-auth lanes; AgentQL should only locate controls after authorization state is established.
- **Session Data Boundary:**

  > Critical review item. Official architecture says AgentQL uses page HTML and accessibility trees and invokes public GPT-4, Llama, Gemini, and proprietary models; therefore page text and form context leave the local browser for hosted queries. Terms permit Tiny Fish to host, copy, transmit, process, and use Customer Data to provide, secure, maintain, and improve the service, plus retain aggregated/de-identified data. Tetra additionally hosts the browser. Cookies need not be sent explicitly, but rendered page content can contain candidate PII or filled values; query before injecting secrets where possible and require contractual privacy review for production.

#### Quality And Safety

- **Answer Provenance:**

  > Strong if scope remains locator-only. AgentQL need not generate any answer; ApplyPilot can resolve all values through its resume/profile/approved-answer evidence pipeline and use returned Locators only for placement. Do not send candidate free-text generation to AgentQL or let semantic field descriptions authorize inferred answers.
- **Unsupported Question Fallback:**

  > Define deterministic behavior: if any term is missing, ambiguous, sensitive, unknown, or not present in the approved answer plan, do not query more broadly and do not submit. Record the exact label/context and route to fixed selector repair, approved-answer lookup, premium agent, or owner review. This policy is caller-owned, not built into AgentQL.
- **Site Permission And Terms:**

  > AgentQL's April 2026 Terms require lawful use, permit only internal business use, prohibit interference with security features, and require use of Tiny Fish programmatic interfaces. They do not grant permission to automate third-party ATS sites. ApplyPilot must separately honor each ATS/employer terms and access controls, use reasonable rates, and route challenge pages to supervised/denied states rather than bypass them.
- **Irreversible Action Guard:**

  > Strong architectural fit because AgentQL returns a Locator and Playwright performs the click. Keep the submit locator lookup separate from execution, verify it is inside the expected form and has the expected role/name, require complete approved answers and a durable submit intent, click once, then capture network/DOM confirmation. Never expose a generic `get_by_prompt('submit')` action directly to an autonomous loop.

#### Operations

- **Concurrency:**

  > Starter allows 10 calls/minute and 5 concurrent Tetra sessions; Professional allows 50 calls/minute and 100 concurrent sessions. Local-browser concurrency is bounded by ApplyPilot workers/profiles rather than AgentQL session caps, but API calls remain rate-limited. Per-account ATS sessions should remain single-concurrency even if the vendor allows more.
- **Warm Session Reuse:**

  > Strong. ApplyPilot can keep the existing Chrome/CDP session and wrapped page, reuse Playwright storage state, or reconnect to a Tetra session configured to survive disconnects until inactivity timeout. Re-query after material DOM changes; do not reuse stale Locators across navigation.
- **Tracing And Replay:**

  > Good tracing through composition. AgentQL exposes request ID, query, response, accessibility tree, debug logs, and screenshots; Playwright adds trace, console, network, DOM, screenshots, and video. AgentQL does not provide deterministic semantic replay, so store query versions and resolved element evidence, and implement a local validated-selector cache for no-API replay experiments.
- **Deployment Modes:**

  > Local Python or JavaScript SDK plus hosted AgentQL API, direct REST API, AgentQL-hosted Tetra Chrome, and custom Enterprise deployment are listed. The pricing page advertises Enterprise on-premises deployment. For initial ApplyPilot use, local Chrome plus hosted query API minimizes browser migration but has the broadest candidate-page data boundary.
- **Canary Stop Loss:**

  > AgentQL offers per-call billing, rate limits, API timeouts, and remote-session inactivity shutdown, but no ApplyPilot route circuit breaker. Enforce a maximum of five semantic calls per attempt, a wall-clock timeout, one Standard-mode retry only before form touch, no retry after submit intent, daily dollar cap, host failure-rate breaker, and immediate quarantine on ambiguous post-click state.

#### Benchmark

- **Benchmark Design:**

  > Use a matched shadow set of at least 600 forms stratified by Ashby, Greenhouse, Lever, Workable, Workday tenant, and long tail. Compare fixed selectors, AgentQL Fast, AgentQL Standard, and the current premium agent on identical pre-submit snapshots where possible. Measure required-control recall, correct-locator precision, answer-placement accuracy, query count, API/browser cost, latency, fallback, and confirmation evidence. After shadow success, canary at most 20 guarded submits per host class with no generic autonomous submit prompt.
- **Route Funnel:**

  > Record preflight eligible -> deterministic selector/cache hit -> AgentQL selected -> query succeeded -> all required fields mapped -> answers provenance-approved -> fields filled -> local validation passed -> submit authorized -> submit locator validated -> one click -> synchronous confirmation -> delayed email confirmation -> verified applied, fallback, failed, blocked, or crash_unconfirmed. Preserve AgentQL request IDs and each fallback reason.
- **Step Context Budget:**

  > Budget at most five AgentQL calls per attempt: one form inventory query, up to two conditional-page queries, one submit-control query, and one post-submit confirmation query. Batch related terms into each query, use Fast first and Standard only for a pre-submit ambiguous result, and record HTML/accessibility payload bytes, query terms, mode, latency, and returned matches. This budget is a canary guard, not evidence of sufficiency.
- **Recommendation:**

  > CANARY as the preferred semantic bridge between deterministic selectors and a full browser agent. Keep the existing Playwright browser, profiles, answerer, queue, traces, and submit guard; use AgentQL only to return structured Locators. Start with shadow form inventory and fill, add a validated local selector cache, cap calls, and require positive confirmation. Production use needs candidate-data/privacy review because page HTML and accessibility content are processed by hosted models.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `browser_version_matrix`
- `cache_replay_yield`
- `confidence_intervals`
- `control_plane_overhead`
- `crash_recovery`
- `deterministic_coverage`
- `escape_rate`
- `extension_support`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `projected_cost_per_verified_apply`
- `prompt_injection_resistance`
- `required_field_completeness`
- `startup_latency`
- `verification_evidence_tier`
- `verification_latency`
- `verified_completion_rate`

### Browser Use

Source record: `Browser_Use.json`

#### Identity

- **Name:** Browser Use
- **Category:** model_assisted
- **Official Sources:**

  > - https://github.com/browser-use/browser-use<br>- https://docs.browser-use.com/open-source/customize/browser/all-parameters<br>- https://docs.browser-use.com/open-source/customize/browser/authentication<br>- https://docs.browser-use.com/open-source/customize/agent/all-parameters<br>- https://docs.browser-use.com/open-source/development/monitoring/costs<br>- https://docs.browser-use.com/cloud/api-v3/sessions/create-session<br>- https://pypi.org/project/browser-use/<br>- https://browser-use.com/pricing<br>- https://browser-use.com/legal/terms-of-service
- **Maintenance Status:**

  > Active. The repository was pushed on 2026-07-09, release 0.13.3 was published on 2026-07-01, and PyPI reported browser-use 0.13.3 requiring Python 3.11+ on 2026-07-09. Material 2025-2026 work includes persistent CLI sessions, cloud API v3, model and cost tracking, profiles, script caching, structured output, CDP reuse, tracing, and security controls.
- **License:**

  > MIT for the open-source Python framework, compatible with ApplyPilot's AGPL-3.0 codebase when the notice is preserved. Browser Use Cloud, hosted models, profiles, proxies, recordings, skills, and data services are commercial and subject to Browser Use terms and privacy policy.

#### Cost Economics

- **Pricing Model:**

  > Open-source Browser Use has no framework charge and uses a selected local/hosted model plus browser compute. Current Cloud pricing offers Pay As You Go at $0/month, $0.06/browser-hour, up to 25 concurrent sessions, $10/GB proxy, $0.01 task initialization, and LLM cost from $0.002 per V2 agent step. Paid plans include credits, higher concurrency, step discounts, and $0.03/browser-hour on Business/Scaleup; API v3 models are token priced.
- **Billing Granularity:**

  > Cloud browser sessions are billed per minute with a one-minute minimum and unused prepaid session time refunded proportionally; sessions can run up to four hours. Agent tasks add $0.01 initialization and V2 step or V3 token charges. Proxy bytes and optional skills are separate. Local open-source mode has model-token and compute granularity only.

#### Reliability

- **Failure Modes:**

  > Relevant failures include model planning error, wrong indexed element after page change, conditional required fields, repeated step retries, max-step exhaustion, model/rate-limit failure, browser launch or CDP loss, profile lock, cloud timeout, proxy/network failure, file upload and cross-origin iframe issues, auth/OTP/challenge walls, cloud profile state not saved after an unclean stop, and false done output without positive submission evidence.
- **Crash Recovery:**

  > Browser keep_alive and CLI daemons preserve warm state, storage_state can be saved, cloud sessions can remain alive, and agent history/conversation, HAR, trace, video, recordings, and streamed messages aid diagnosis. Cloud cacheScript can replay a saved workflow from a known start. There is no transactional mid-submit resume; ApplyPilot must quarantine after any submit-touching crash and reconcile evidence before retry.
- **Exactly Once Submit Safety:**

  > No built-in job dedup or exactly-once application transaction exists. max_steps, max_failures, callbacks, structured output, and maxCostUsd are limits, not submit idempotency. Remove generic autonomous submit authority and expose one deterministic ApplyPilot submit tool that validates lease, plan hash, prior attempts, host policy, and a one-time token, then records response and ambiguity.
- **Verification Evidence Tier:**

  > Browser Use can save HAR and traces, provide direct CDP access to custom tools, capture DOM state/screenshots/video, stream tool messages, return structured output, and expose cloud recordings/costs. A custom verifier can reach the highest tier by parsing an ATS/application ID or explicit response; agent success text, judge verdict, DOM message, or video alone should not mark applied.

#### Applypilot Fit

- **Python Integration:**

  > Excellent. Browser Use is a Python-first async framework, currently requires Python 3.11+, exposes Agent, Browser/BrowserSession, Tools, lifecycle hooks, structured Pydantic output, cost tracking, and direct CDP injection. This matches ApplyPilot's runtime, though dependency and event-loop integration should be isolated and version pinned.
- **Playwright Cdp Reuse:**

  > Strong CDP reuse. Browser accepts cdp_url for an existing local or remote browser, from_system_chrome detects the installed Chrome/profile, storage_state uses Playwright-compatible format, and custom tools can receive a CDP client or browser session. ApplyPilot still needs exclusive tab/profile ownership and must not let two drivers control the same page concurrently.
- **Observability Joinability:**

  > Strong if configured. calculate_cost exposes token/cost summaries; history and saved conversations expose actions; browser options support HAR, traces, and video; cloud streams messages with screenshots and returns session/task cost, profile, recording, live URL, and status. Put ApplyPilot attempt_id in cloud metadata and persist artifact hashes, tool inputs/outputs, model/version, and evidence IDs in apply_result_events.

#### Authentication

- **Persistent Profiles:**

  > Supports local user_data_dir/profile_directory, system Chrome profiles, Playwright storage_state with automatic load/save, and managed cloud profiles that persist cookies/localStorage/passwords after sessions are stopped cleanly. Enforce exclusive profile locks, encrypted storage, explicit cloud session stop in finally blocks, per-account concurrency, and profile migration tests across browser versions.
- **Existing Session Attach:**

  > First-class through cdp_url, CLI --cdp-url, CLI connect, and Browser.from_system_chrome. Cloud browsers and persistent profiles are also supported. Attach only to a dedicated ApplyPilot profile/tab; a debugging connection can access authenticated data, and system Chrome may need to be fully closed before the framework relaunches it in debug mode.
- **Login And Otp:**

  > Broad support: real browser profiles, storage state, cloud profiles, TOTP, documented email/SMS approaches, cloud 1Password/TOTP integration, sensitive placeholders, and custom ask-human tools. ApplyPilot should still keep account creation, unexpected consent, and OTP in the owner/home lane, use single-use codes, and prove authenticated state before resuming.
- **Extension Support:**

  > Local browser settings load default automation extensions and accept custom launch args; existing system Chrome profiles can retain installed extensions, and allowed_domains can include chrome-extension URLs. For ApplyPilot, disable default extensions unless needed, load only a reviewed narrowly permissioned extension, and keep extension secrets and native messaging outside model-visible page context.
- **Session Data Boundary:**

  > Local mode can keep browser/profile/HAR/trace data on the machine, but selected model inputs leave the machine unless using a local model. Browser Use Cloud stores profiles and receives cookies, localStorage, files, screenshots, messages, recordings, and network/proxy traffic; Scaleup advertises zero data retention, while lower-tier retention terms require current review. Open-source observability docs say cloud sync is enabled by default, so ApplyPilot should explicitly disable sync/telemetry unless approved.

#### Quality And Safety

- **Required Field Completeness:**

  > The agent can fill multiple fields and structured output can validate its final report, but neither guarantees all visible and conditionally revealed required controls are complete. Add a custom deterministic form-inventory tool before planning and again before submit, reject unknown required controls, call browser validity APIs, and bind filled values to the signed answer plan.
- **Answer Provenance:**

  > sensitive_data placeholders prevent raw secrets from entering model text and can scope credentials by domain, but normal answer provenance is not enforced. Provide custom lookup/fill tools that return only profile, resume, approved-answer, and job-derived values with source IDs; prohibit invented facts and keep demographic/sensitive answers owner-controlled.
- **Unsupported Question Fallback:**

  > Browser Use supports custom ask-human tools and ActionResult states, so unknown questions can exit cleanly. Classify sensitive, legal, demographic, compensation, work-authorization, and unsupported free text before filling; persist needs_review and stop the agent without exposing submit. Do not let final_response_after_failure convert incomplete work into applied.
- **Irreversible Action Guard:**

  > Generic Agent tools can click submit, so safety must be redesigned at the tool registry. Remove or intercept generic click on recognized submit elements, expose a deterministic one-time submit tool only after plan/provenance/policy/dedup validation, set a lifecycle hook before and after the action, and quarantine any missing positive response. Start with submit disabled.

#### Operations

- **Warm Session Reuse:**

  > Strong. Browser keep_alive, CLI background daemon and named sessions, Cloud keepAlive, persistent profiles, and follow-up tasks preserve state. Reuse must include a clean-tab reset, profile lock, idle timeout, health probe, cost meter, and explicit stop so one application's page or files do not leak into another.
- **Tracing And Replay:**

  > Supports saved conversation history, agent action history, HAR with embedded or attached content, browser traces, local video/GIF, lifecycle hooks, Laminar tracing, cloud messages/screenshots, and cloud recordings. Cloud v3 cacheScript provides deterministic script replay with $0 LLM cost after a first run. Never replay across a changed form signature or after ambiguous submit.
- **Deployment Modes:**

  > Local open-source Python, local CLI/MCP with persistent Chromium or real Chrome, Docker/self-hosted workers, any local/remote CDP browser, Browser Use Cloud browser, fully hosted Cloud agent/API/MCP, cloud persistent profiles and workspaces, and hybrid local agent with managed browser.
- **Browser Version Matrix:**

  > Documented execution is Chromium-family over CDP, including managed Chromium, Chrome channels, system Chrome, and Microsoft Edge channel selection. Firefox, WebKit, Safari, and WebDriver BiDi are not documented as supported execution engines. Pin browser-use and Chrome/Chromium and run synthetic ATS compatibility tests before rollout.
- **Canary Stop Loss:**

  > Use Agent max_steps, max_failures, step/LLM timeouts, restricted tools/domains/files, calculate_cost, and Cloud maxCostUsd. Initial policy should stop before submit at 20 steps or $0.10, stop a route on any duplicate/provenance violation, quarantine ambiguity, and trip ATS/host breakers when p95 cost or failure rate exceeds the current control.

#### Benchmark

- **Benchmark Design:**

  > Compare current Playwright MCP, deterministic adapters, Browser Use local with a cheap model, and Browser Use Cloud. Repeated synthetic ATS forms should cover conditional fields, uploads, iframes, hostile text, auth, crashes, and cache invalidation; unique randomized live jobs should cover ATS strata without duplicate submissions. Capture step/model/browser/proxy/human costs and evidence tier.
- **Route Funnel:**

  > Track eligibility, host policy, browser/profile acquired, auth valid, agent initialized, deterministic preflight, steps/actions, required plan complete, provenance complete, submit guard approved, submit tool called, network/DOM confirmation, fallback before submit, human/OTP recovery, ambiguous quarantine, delayed email reconciliation, and final disposition.
- **Recommendation:**

  > CANARY. Use Browser Use as a Python-native, tightly budgeted long-tail fallback and as a source of reusable deterministic tools/scripts, not as the first route for stable ATSs. Keep Ashby/Greenhouse adapter-first, disable autonomous submit, restrict domains and data sync, and compare local versus Cloud economics. Promote only signatures that beat all-in cost while preserving answer provenance and positive confirmation.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `cache_replay_yield`
- `concurrency`
- `confidence_intervals`
- `control_plane_overhead`
- `deterministic_coverage`
- `escape_rate`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `model_cost`
- `projected_cost_per_verified_apply`
- `prompt_injection_resistance`
- `site_permission_and_terms`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`

### Skyvern

Source record: `Skyvern.json`

#### Identity

- **Name:** Skyvern
- **Category:** model_assisted
- **Official Sources:**

  > - https://github.com/skyvern-ai/skyvern<br>- https://github.com/skyvern-ai/skyvern/releases<br>- https://www.skyvern.com/pricing/<br>- https://www.skyvern.com/blog/launch-week-day-5-simpler-pricing-model/<br>- https://www.skyvern.com/docs/developers/getting-started/core-concepts<br>- https://www.skyvern.com/docs/developers/optimization/browser-profiles<br>- https://www.skyvern.com/docs/developers/optimization/browser-tunneling<br>- https://www.skyvern.com/docs/developers/credentials/store-credentials<br>- https://www.skyvern.com/docs/cookbooks/job-application-filler<br>- https://www.skyvern.com/docs/changelog
- **Maintenance Status:**

  > Actively maintained. The public repository showed release v1.0.46 on 2026-07-08, after many material 2025-2026 releases covering adaptive caching, browser profiles, CDP browser control, OpenTelemetry, authentication, security hardening, and workflow reliability.
- **License:**

  > The core repository is AGPL-3.0, matching ApplyPilot's AGPL-3.0 license family. Modified or network-deployed derivative code must preserve AGPL source-availability obligations. A separately deployed Skyvern service with an API boundary is operationally cleaner, and managed-cloud-only anti-bot features are not part of the open-source grant.

#### Reliability

- **Failure Modes:**

  > Important failure classes are stale or expired postings, policy denial, wrong element or option selection, incomplete required questions, unsupported custom widgets, fabricated or weak free text, login or OTP waits, CAPTCHA or challenge states, stale/corrupt profiles, browser or CDP tunnel loss, model timeout, cached-script drift, workflow retry duplication, and `completed` status without a confirmed submit. Skyvern's release history also documents fixes for iframe trees, dropdowns, profile corruption, final-status races, browser timeouts, and script-cache regressions, which should become canary fixtures.

#### Applypilot Fit

- **Python Integration:**

  > Strong. The current SDK supports Python 3.11-3.13, compatible with ApplyPilot's Python >=3.11 runtime, and exposes Python workflow APIs plus AI-enhanced Playwright page methods such as `act`, `extract`, and `validate`. A REST API and webhooks are also available if Skyvern is isolated as a service.
- **Playwright Cdp Reuse:**

  > Moderate to strong. The SDK layers AI methods on Playwright, cloud sessions expose a Playwright page over CDP, self-hosted documentation references CDP connect, and `skyvern browser serve` can launch Chrome with an existing profile directory, cookies, extensions, and passwords. Lifecycle and port ownership still need an adapter so Skyvern does not conflict with ApplyPilot's current Chrome/CDP process.
- **Observability Joinability:**

  > Strong if integrated deliberately. Skyvern exposes run and browser-session IDs, statuses, failure reasons, action timelines, recordings, screenshots, HAR files, downloaded-file checksums, step counts, webhooks, and OpenTelemetry in current releases. ApplyPilot must persist its attempt ID as a workflow parameter/tag and map every Skyvern run to `apply_result_events`, cost rows, browser artifacts, and later email evidence.

#### Authentication

- **Persistent Profiles:**

  > Strong. Browser sessions preserve live state for 5 minutes to 24 hours, and browser profiles archive cookies, localStorage, session files, and auth tokens for later reuse. Profile creation is asynchronous, tokens can expire, profiles can corrupt, and current docs recommend periodic refresh/testing and provide reset/delete paths. Profile locking and one-account concurrency remain ApplyPilot responsibilities.
- **Existing Session Attach:**

  > Supported with boundaries. `skyvern browser serve` launches or exposes local Chrome through a secured connection and can point at a profile directory; self-hosted CDP connect is documented. The local tunnel must use API-key authentication and preferably VPN/mTLS/IP controls because access grants full browser control. Direct attachment to an arbitrary already-owned ApplyPilot tab needs a proof-of-concept.
- **Login And Otp:**

  > Strong feature coverage: stored credentials, dedicated login actions, password-manager or vault backends, TOTP, email/SMS 2FA flows, notifications, webhooks, and human interaction are documented. Pro is the first public plan listing 2FA/TOTP; Enterprise advertises human-in-the-loop. ApplyPilot should still route owner decisions and account recovery to its supervised auth lane.
- **Extension Support:**

  > Local browser serving explicitly reuses installed extensions. Skyvern does not document a per-workflow extension permission allowlist or native-messaging policy. Use a dedicated cloned profile with only approved extensions and never expose a general personal profile through an unauthenticated tunnel.
- **Session Data Boundary:**

  > Managed Cloud may process and retain prompts, URLs, page content, screenshots, files, workflow definitions, run history, browser/session state, and connected credentials as needed for the service. Credential docs state secrets are fetched just in time, replaced with placeholders for LLMs, masked in artifacts, and can remain in external vaults. Self-hosting keeps browser and application data in ApplyPilot-controlled infrastructure, but any configured external model still receives non-secret page context; telemetry is enabled by default in the open-source repo unless disabled.

#### Operations

- **Concurrency:**

  > Managed public plans list 1 concurrent run on Free, 10 on Hobby, 25 on Pro, and custom/unlimited on Enterprise. Self-hosted concurrency is limited by browser, model, database, queue, and worker resources. ApplyPilot should also enforce per-account and per-profile concurrency of one where an ATS session can be invalidated or duplicated.
- **Warm Session Reuse:**

  > Strong. Live browser sessions preserve tabs, cookies, storage, and page context for chained operations, profiles restore archived authentication state across runs, local browser serve reuses a profile directory, and adaptive caching can reuse generated scripts. Use bounded lifetimes, health checks, and profile locks to avoid stale or cross-attempt state.
- **Tracing And Replay:**

  > Strong tracing, partial replay. Native artifacts include action and reasoning logs, screenshots, video, HAR, structured run data, downloaded-file checksums, and OpenTelemetry support. Adaptive cached scripts can replay learned actions, but deterministic time-travel replay of arbitrary model runs is not promised. Retain Playwright/HAR evidence around submit and version every workflow/cache key.
- **Deployment Modes:**

  > Managed cloud, Python or TypeScript SDK against cloud, REST/MCP, Docker Compose self-hosting, on-premises cloud infrastructure, and a hybrid local-browser tunnel are documented. The safest ApplyPilot experiment is an isolated self-host or dedicated cloud workspace with a narrow client boundary and separate browser profile.
- **Browser Version Matrix:**

  > Skyvern documentation consistently describes real Chromium/Chrome plus CDP and Playwright. Local Windows, macOS, and Linux Chrome paths are documented. Firefox, WebKit, and WebDriver BiDi support are not documented, so treat this as Chromium-only for planning.

#### Benchmark

- **Benchmark Design:**

  > Run Skyvern only after deterministic adapters and the AgentQL-style semantic route decline. Use at least 300 shadow attempts stratified across Ashby, Greenhouse, Lever, Workable, Workday tenant, and long-tail hosts, with the same jobs mirrored to the current agent where safe. Then canary no more than 20 submit-authorized attempts per host class. Record required-field recall, answer provenance, actions, credits, wall time, fallback, human minutes, confirmation tier, duplicates, and positive verified submits. Keep challenge and login-wall cases supervised.
- **Route Funnel:**

  > For every eligible job record: preflight disposition -> deterministic adapter eligibility -> semantic-locator eligibility -> Skyvern shadow selection -> workflow started -> form discovered -> required plan complete -> auth/challenge state -> submit authorized -> click observed -> synchronous confirmation -> delayed email reconciliation -> verified applied, failed, blocked, or crash_unconfirmed. Preserve both selected route and every fallback reason.
- **Recommendation:**

  > CANARY, not broad adoption. Use Skyvern as the final model-assisted route for approved long-tail forms after deterministic adapters, HTTP preflight, and a semantic-locator layer fail. Start in shadow mode, keep ApplyPilot's answer provenance and exactly-once submit guard, require positive confirmation evidence, and promote per host only when the upper-bound quality risk is no worse and all-in cost per verified apply is below the current route. Do not use managed anti-bot or CAPTCHA capabilities to bypass access controls.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `answer_provenance`
- `ats_fit`
- `billing_granularity`
- `cache_replay_yield`
- `canary_stop_loss`
- `confidence_intervals`
- `control_plane_overhead`
- `crash_recovery`
- `deterministic_coverage`
- `escape_rate`
- `exactly_once_submit_safety`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `irreversible_action_guard`
- `marginal_cost_per_attempt`
- `migration_effort`
- `model_cost`
- `pricing_model`
- `projected_cost_per_verified_apply`
- `prompt_injection_resistance`
- `required_field_completeness`
- `site_permission_and_terms`
- `startup_latency`
- `step_context_budget`
- `unsupported_question_fallback`
- `verification_evidence_tier`
- `verification_latency`
- `verified_completion_rate`

### Stagehand

Source record: `Stagehand.json`

#### Identity

- **Name:** Stagehand
- **Category:** model_assisted
- **Official Sources:**

  > - https://github.com/browserbase/stagehand<br>- https://github.com/browserbase/stagehand-python<br>- https://docs.stagehand.dev/v3/sdk/python<br>- https://docs.stagehand.dev/v3/configuration/browser<br>- https://docs.stagehand.dev/v3/configuration/models<br>- https://docs.stagehand.dev/v3/best-practices/caching<br>- https://docs.stagehand.dev/v3/configuration/observability<br>- https://www.npmjs.com/package/@browserbasehq/stagehand<br>- https://pypi.org/project/stagehand/<br>- https://www.browserbase.com/pricing
- **Maintenance Status:**

  > Active. The TypeScript monorepo was pushed on 2026-07-09 and npm reported @browserbasehq/stagehand 3.6.0; the Python repository released 3.21.0 on 2026-05-29 and was pushed on 2026-06-09. Stagehand v3 documentation is current and covers local/Browserbase execution, Python, caching, self-healing, structured observe/act/extract operations, and agent execution.
- **License:**

  > MIT for the TypeScript and Python SDKs, compatible with ApplyPilot's AGPL-3.0 codebase when notices are retained. Browserbase cloud, Model Gateway, proxies, recordings, and managed caches are commercial services governed separately; the open-source license does not grant those services or override site terms.

#### Cost Economics

- **Pricing Model:**

  > Open-source local Stagehand has no framework fee and uses a selected model plus local compute. Browserbase plans currently include Free at $0 with 1 browser hour and 3 agent runs, Developer at $20/month with 100 browser hours then $0.12/hour, and Startup at $99/month with 500 browser hours then $0.10/hour. Model Gateway is pay-as-you-go at upstream market token prices, and proxy overage is separately metered.
- **Billing Granularity:**

  > Browserbase includes monthly browser-hour allowances, then meters browser hours; current plan pages do not state a smaller production unit than browser time. Model Gateway passes through market token prices without markup. Developer proxy overage is $12/GB and Startup overage is $10/GB. Idle keep-alive sessions consume browser time, and cache hits remove Stagehand model inference but not browser or proxy cost.

#### Reliability

- **Failure Modes:**

  > Expected failures include stale or unstable accessibility trees causing cache misses, self-heal choosing the wrong element, conditional fields appearing after an action, model schema errors, unsupported file or rich-text widgets, browser or CDP loss, Stagehand API/model rate limits, Browserbase session timeout, profile/context mismatch, cache replay after a meaningful DOM change, login/OTP/challenge walls, and an agent claiming success without durable submit evidence.
- **Crash Recovery:**

  > keepAlive can leave local or Browserbase sessions running and Browserbase sessions can be reattached by ID. Local action/agent caches can replay known actions, while structured logs, CDP events, and Browserbase recordings support diagnosis. There is no job-level transactional resume; after any submit-touching action, ApplyPilot must inspect evidence and quarantine ambiguity rather than replaying the cached sequence.
- **Exactly Once Submit Safety:**

  > Stagehand does not supply application deduplication, a submission ledger, or an atomic submit/confirm transaction. observe can preview an action before act, which helps planning but is not an idempotency guarantee. Keep final submit in a narrow ApplyPilot function with lease validation, plan hash, one-time token, network/DOM verification, and crash_unconfirmed quarantine.
- **Verification Evidence Tier:**

  > Stagehand can extract typed confirmation data, use a Playwright/custom page, expose CDP-based page responses, and rely on Browserbase network monitoring, console logs, screenshots, and session replay. It reaches a high evidence tier only when an ApplyPilot verifier records an ATS/application ID or explicit successful response; model output or an extracted success message alone is lower-confidence and a recording is supporting evidence, not proof.

#### Applypilot Fit

- **Python Integration:**

  > Good and current. The official stagehand Python package requires Python 3.9+ and supports synchronous and asynchronous clients, local mode, remote sessions, and Bring Your Own Browser patterns with Playwright or other drivers. ApplyPilot can remain Python-first, although the TypeScript implementation has the broadest direct local-library surface and Python v3 API semantics should be pinned and tested.
- **Observability Joinability:**

  > Strong. Stagehand can write structured Stagehand, LLM, and CDP event logs per session; Browserbase adds session IDs, recordings, network requests, console logs, CPU/RAM, duration, proxy bytes, and cost data. Store attempt_id in session metadata and persist session ID, cache status, model, token usage, action/result hashes, recording URL or local artifact paths, and final evidence in apply_result_events.

#### Authentication

- **Persistent Profiles:**

  > Local launch options support userDataDir and preservation, while Browserbase offers persistent contexts and existing session IDs. keepAlive can retain browser state after process close. ApplyPilot needs one account/profile owner, exclusive locks, explicit context persistence on clean shutdown, encrypted storage, and canaries across Chrome upgrades.
- **Login And Otp:**

  > Persistent contexts, supplied cookies, variables that keep secret values out of prompts, Browserbase live visibility, and keepAlive support owner-assisted login. There is no built-in ApplyPilot inbox polling, OTP matching, or single-use code ledger. Route login and OTP to the existing owner/home recovery lane and resume only after deterministic authentication checks.
- **Session Data Boundary:**

  > With local library mode and Stagehand API disabled, browser execution stays local, but page context still goes to the configured model unless it is local. Browserbase mode sends cookies, page data, resumes/uploads, network traffic, recordings, and managed cache inputs to Browserbase and model inputs to Model Gateway/provider. Current Browserbase pricing states 7-day retention for Free and Developer, 30 days for Startup, and 30+ days for Scale; local cache/log files may also contain sensitive action data and must be protected.

#### Quality And Safety

- **Required Field Completeness:**

  > observe can discover structured candidate actions and extract can validate typed data, but neither proves every current and conditionally revealed required field is answered. ApplyPilot must build a deterministic form inventory, re-run it after conditional actions, reject unmapped controls, compare the final browser state with the signed answer plan, and only then expose submit.
- **Answer Provenance:**

  > Variables can keep raw secrets out of prompts and caches, but Stagehand does not constrain ordinary answers to ApplyPilot evidence. Resolve every answer before act from profile, resume, approved-answer, or job evidence; attach source IDs/hashes; disallow model-authored facts; and send unknown free text to review.
- **Unsupported Question Fallback:**

  > Use observe/extract only to classify the question, then return a durable needs_review state for unknown, sensitive, legal, demographic, compensation, work-authorization, or unsupported free-text prompts. Stagehand can hand control back to Playwright or an owner, but the fallback boundary and no-submit guarantee must be implemented by ApplyPilot.
- **Irreversible Action Guard:**

  > No Stagehand primitive makes submit transactionally safe. Use observe for a preview, but execute final submit only through a separate deterministic function after ApplyPilot checks required-field completeness, provenance, host policy, current lease, dedup, and a one-time approval token. Disable autonomous agent submit during shadow and early canary phases.

#### Operations

- **Warm Session Reuse:**

  > Strong. keepAlive retains local or Browserbase sessions, browserbaseSessionID reconnects to managed sessions, persistent contexts preserve auth, and local/server caches reuse actions. ApplyPilot must add idle TTLs, health checks, profile locks, page reset, and explicit session termination so reuse does not leak one job's state into another.
- **Tracing And Replay:**

  > Local structured logs can include LLM requests/responses, CDP calls/events, and Stagehand operations. Browserbase adds screen recording/replay, network timing, console logs, CPU/RAM, and cost/utilization. Local cache can replay act and agent actions without LLM calls, but replay must be invalidated on page/form changes and prohibited after an ambiguous submit.
- **Deployment Modes:**

  > TypeScript or Python locally with Chrome/Chromium, local execution with direct model keys, Browserbase-hosted browser plus Stagehand API, Browserbase browser with API mode disabled, managed Model Gateway, custom/OpenAI-compatible model endpoints, local Ollama, containers, and hybrid BYOB/CDP use.
- **Browser Version Matrix:**

  > The documented browser path is Chrome/Chromium over CDP. Stagehand can interoperate with Playwright, Puppeteer, and Patchright Page objects, but this does not establish Firefox, WebKit, WebDriver BiDi, or Safari support. Pin Chrome and Stagehand versions and run the synthetic ATS suite before fleet rollout.
- **Canary Stop Loss:**

  > Set per-attempt model, token, action, time, and browser-session budgets outside Stagehand; limit agent max steps, disable submit in shadow mode, and stop on the first duplicate or ambiguous outcome. Suggested promotion gates are equal positive-confirmation quality, lower bootstrap cost per verified apply, no provenance violations, and route-specific failure rate below the control.

#### Benchmark

- **Benchmark Design:**

  > Compare deterministic adapter, current Playwright MCP agent, Stagehand uncached, and Stagehand warm-cache routes. Use repeated synthetic ATS forms for cache invalidation, conditional fields, uploads, hostile text, and crash points; use unique randomized live jobs in ATS strata to avoid duplicate applications. Record cache hit/miss, self-heal, tokens, browser time, fallback, human minutes, and evidence tier.
- **Route Funnel:**

  > Track eligible, policy-allowed, Stagehand session started, profile attached, cache lookup, cache hit/miss, form inventory complete, required plan complete, provenance verified, submit token issued, action executed, positive network/DOM confirmation, fallback before submit, human recovery, ambiguous quarantine, and final outcome.
- **Recommendation:**

  > CANARY. Test Stagehand as a semantic layer for long-tail forms and as a cacheable bridge from model-assisted discovery to no-model replay. Keep deterministic Ashby/Greenhouse adapters first, keep final submit behind ApplyPilot's guarded function, and compare local Python/BYOB against Browserbase economics. Promote only ATS/form signatures with stable cache validity and positive confirmation.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `cache_replay_yield`
- `concurrency`
- `confidence_intervals`
- `control_plane_overhead`
- `deterministic_coverage`
- `escape_rate`
- `existing_session_attach`
- `extension_support`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `model_cost`
- `playwright_cdp_reuse`
- `projected_cost_per_verified_apply`
- `prompt_injection_resistance`
- `site_permission_and_terms`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`

### Crawlee AdaptivePlaywrightCrawler

Source record: `Crawlee_AdaptivePlaywrightCrawler.json`

#### Identity

- **Name:** Crawlee AdaptivePlaywrightCrawler
- **Category:** preflight
- **Official Sources:**

  > - https://github.com/apify/crawlee-python<br>- https://github.com/apify/crawlee-python/releases<br>- https://github.com/apify/crawlee-python/blob/master/LICENSE<br>- https://crawlee.dev/python/docs/guides/adaptive-playwright-crawler<br>- https://crawlee.dev/python/api/class/AdaptivePlaywrightCrawler<br>- https://crawlee.dev/python/api/class/PlaywrightCrawler<br>- https://crawlee.dev/python/docs/guides/error-handling<br>- https://crawlee.dev/python/docs/guides/session-management<br>- https://crawlee.dev/python/docs/guides/trace-and-monitor-crawlers<br>- https://crawlee.dev/python/docs/guides/security-of-web-scraping<br>- https://crawlee.dev/python/docs/examples/using_browser_profile<br>- https://crawlee.dev/python/docs/guides/storages
- **Maintenance Status:**

  > Actively maintained. Crawlee for Python released 1.8.1 on 2026-07-08, its adaptive crawler documentation was updated on 2026-07-09, and 2025-2026 releases materially improved persisted rendering predictions, browser-context reuse, state, statistics, storage, tracing, and adaptive-crawler correctness.
- **License:**

  > Apache-2.0, including its patent grant and NOTICE obligations. This permissive license is compatible with ApplyPilot's AGPL-3.0 codebase; retain required copyright, license, and NOTICE material when distributing. Optional Apify managed services are commercial and separate from the library license.

#### Cost Economics

- **Pricing Model:**

  > The Crawlee library is free and open source with no per-request, model, or browser fee. Self-hosted cost is local HTTP, CPU, memory, storage, network, and occasional Playwright browser time. Optional deployment on Apify or another cloud is billed under that platform's compute, proxy, storage, and network prices rather than by AdaptivePlaywrightCrawler itself.
- **Billing Granularity:**

  > Self-hosted operation has no Crawlee billing unit, idle fee, cold-start fee, concurrency surcharge, or included quota. Costs accrue through the host, network/proxy, storage, and browser infrastructure. HTTP and browser paths should be accounted separately because the rendering predictor can occasionally run both to test its recommendation.
- **Model Cost:**

  > Zero by default. AdaptivePlaywrightCrawler uses a deterministic RenderingTypePredictor, HTTP parser, selectors, and Playwright; it does not require an LLM. Any optional classifier added by ApplyPilot should be a separate, explicitly costed route and must not be conflated with Crawlee.
- **Human Recovery Cost:**

  > Clean deterministic classifications need no operator time. Ambiguous, authenticated, challenged, or policy-sensitive pages must fall through to the existing route rather than ask a human during preflight. Human cost is therefore indirect and should be measured as false-positive review minutes plus any owner escalation caused by an ambiguous classification.

#### Reliability

- **Verified Completion Rate:**

  > Not applicable as a direct output: the route intentionally performs zero applications. Its quality metrics are expired/ineligible precision and recall, false-denial rate, browser-escalation rate, downstream verified-apply preservation, and avoided cost. It must never be credited with a verified application.
- **Failure Modes:**

  > False `expired` or `ineligible` classifications can suppress valid jobs; false negatives waste downstream spend. Causes include stale cached pages, redirects, locale variants, login walls, soft-404 pages, client-rendered status, unexpected status codes, selector drift, rendering-predictor error, parser differences, timeouts, rate limits, proxy/session rotation, and dual-run result mismatch. Retries can amplify load unless bounded.
- **Crash Recovery:**

  > Strong for read-only preflight. Crawlee provides persistent request queues, request deduplication, state persistence, configurable retries and session rotations, pre-retry error handlers, exhausted-request handlers, stop/abort controls, and final statistics. Because requests are GET/HEAD-only and have no external mutation, replay is naturally idempotent; persist classification evidence and retry count with the ApplyPilot attempt.
- **Exactly Once Submit Safety:**

  > Strong by scope separation: this component must never locate, fill, or click submit. Enforce allowed methods GET/HEAD, deny form POST and non-HTTP schemes, emit only a classification/evidence object, and leave all irreversible work to downstream routes. It can reduce exactly-once risk by filtering known duplicate or already-applied pages before browser execution.
- **Verification Evidence Tier:**

  > Preflight evidence is not submission evidence. It can persist HTTP status, redirect chain/final URL, response headers, body hash, matched expired/policy markers, static parsed fields, browser-escalation reason, rendered DOM marker, screenshot, and timestamp. Downstream verified applies still require ATS ID/response, confirmation DOM/URL, or confirmation email.

#### Applypilot Fit

- **Python Integration:**

  > Strong. Crawlee for Python is asyncio-based, typed, available from PyPI, and integrates directly into an application without a separate launcher. ApplyPilot already requires Python >=3.11 and Playwright; the current Crawlee package supports that runtime and can be called as a pre-agent module.
- **Control Plane Overhead:**

  > Low. One async request, parser pass, deterministic marker checks, and a compact evidence record are sufficient for the common path; no screenshot or model context is required. Adaptive learning can occasionally execute both HTTP and Playwright and compare handler outputs, so record dual-run overhead and keep the handler interface minimal.
- **Observability Joinability:**

  > Strong. Crawlee exposes request URLs/unique keys, RequestQueue state, retry/session data, final statistics, HTTP-only handler runs, browser handler runs, rendering-type mispredictions, logs, datasets/key-value storage, and OpenTelemetry instrumentation. Use ApplyPilot job/attempt ID as request unique key and write the final classification into `apply_result_events` with route, evidence hash, latency, and downstream disposition.

#### Authentication

- **Persistent Profiles:**

  > Available but usually unnecessary. PlaywrightCrawler supports persistent contexts via `user_data_dir`, normal cookies/session pools, and a documented approach that copies a local Chrome/Firefox profile because Chrome should not automate the main profile directly. For public preflight, use an isolated empty profile; authenticated candidate profiles belong in ApplyPilot's auth lane.
- **Login And Otp:**

  > Crawlee supports sessions, cookies, authentication initialization, and browser profiles, but AdaptivePlaywrightCrawler provides no OTP relay, credential vault, or human takeover product. Preflight should detect auth walls and return `auth_required` or `ambiguous`, never enter credentials or consume owner OTPs.
- **Session Data Boundary:**

  > Self-hosted Crawlee keeps responses, cookies, headers, screenshots, queues, and datasets in ApplyPilot-controlled memory/storage unless an external proxy, telemetry exporter, cloud storage client, or Apify deployment is configured. Official security guidance warns that proxies can observe destinations and potentially credentials and that browser crawlers execute untrusted code. Use HTTPS verification, trusted proxies only, browser isolation, retention limits, and no candidate secrets in public preflight.

#### Quality And Safety

- **Required Field Completeness:**

  > Not applicable to this route. It must not inspect an application form deeply enough to claim an answer plan is complete. It may detect whether a job remains open and unattended-apply eligible; required-field enumeration remains the deterministic adapter or semantic-locator route's responsibility.
- **Answer Provenance:**

  > Not applicable. The component generates no application answers and should receive no resume/profile values. It may evaluate only job metadata and approved policy facts such as excluded company, geography, duplicate, or previously applied status.
- **Unsupported Question Fallback:**

  > Not applicable for answers. If a page cannot be confidently classified from allowed status/policy evidence, return `unknown` with evidence and continue to the existing route. Never convert parsing uncertainty into an ineligibility denial.
- **Prompt Injection Resistance:**

  > No LLM is required, so page text cannot issue model instructions. Hostile content remains untrusted input and can exploit browsers, redirect to internal hosts, create crawler traps, or poison extracted values. Enforce scheme/domain allowlists, private-network denial, response-size and decompression limits, browser sandboxing, strict parsers, output schemas, and no dynamic execution of extracted code.
- **Site Permission And Terms:**

  > Crawlee provides `respect_robots_txt_file`, rate/concurrency controls, and official security guidance, but use of the library does not grant permission to fetch or automate any ATS. Restrict this route to normal public GET/HEAD behavior, honor relevant site terms and rate limits, identify or park challenge responses, and never use retry or proxy features to bypass access controls.
- **Irreversible Action Guard:**

  > Excellent when implemented as designed: compile-time/API separation should expose no `Page` or form action methods to classification code and reject all non-GET/HEAD requests. The output is advisory evidence only. Downstream submit ownership, consent, idempotency, and confirmation remain unchanged.

#### Operations

- **Concurrency:**

  > Crawlee uses AutoscaledPool and configurable min/max concurrency, tasks per minute, session pool size, maximum open pages, and system resource snapshots. HTTP paths can run at substantially higher concurrency than browsers, but host-specific rate limits and fleet backpressure should dominate. Start with low per-host concurrency and separate HTTP/browser semaphores.
- **Warm Session Reuse:**

  > Strong. BrowserPool reuses browsers/contexts, PlaywrightCrawler defaults to persistent contexts in current major versions, SessionPool retains cookies and headers, request/storage state persists, and DefaultRenderingTypePredictor state is persisted and learns from prior pages. Isolate sessions by host/account and avoid carrying authenticated state into public preflight.
- **Tracing And Replay:**

  > Good for deterministic preflight. Logs, request queues, datasets, key-value stores, statistics, predictor run counts/mispredictions, and OpenTelemetry request traces are available. Playwright traces/screenshots/HAR can be added on browser escalations. Replaying a saved GET plus body fixture is deterministic; replay live pages only with timestamp and content hash because content can change.
- **Deployment Modes:**

  > Local Python process, embedded ApplyPilot module, worker/container, generic self-hosted cloud, and Apify platform deployment are supported. Storage backends can be in-memory, filesystem, SQL, Redis, or managed alternatives depending configuration. The lowest-risk path is an embedded local module with the existing fleet database as the durable result sink.
- **Browser Version Matrix:**

  > The HTTP path is browser-independent. Current PlaywrightCrawler supports Playwright-managed Chromium, Firefox, WebKit, Edge, and locally installed Chrome according to current docs, with headed/headless and persistent contexts. CDP is Chromium-specific; WebDriver BiDi is not the control interface.
- **Canary Stop Loss:**

  > Use `max_requests_per_crawl`, max crawl depth, navigation and handler timeouts, retry/session-rotation caps, `abort_on_error`, `stop`, robots handling, response-size guards, per-host concurrency, and separate browser escalation caps. Add ApplyPilot-specific daily request/browser/spend budgets and a circuit breaker on false-denial audit failures or predictor misprediction rate.

#### Benchmark

- **Benchmark Design:**

  > Build a labeled corpus of at least 2,000 historical URLs, stratified by Ashby, Greenhouse, Lever, Workable, Workday tenant, long tail, and the local historical states applied/failed/blocked. Freeze response fixtures where permitted and also run a live matched sample. Label open, expired, redirected, duplicate, excluded, geography-ineligible, auth wall, challenge, and unknown. Compare no-preflight baseline, plain HTTP classifier, and adaptive HTTP/Playwright on precision/recall, false denial, escalation, latency, requests, browser seconds, and avoided downstream cost.
- **Route Funnel:**

  > Record URL accepted -> host/policy allowlist -> cache/freshness check -> HTTP selected -> static result valid or invalid -> optional dual-run -> Playwright escalation -> classification open/expired/deny/park/unknown -> downstream route selected -> application attempted -> positive verification or terminal failure. Join the initial evidence and final outcome so a false preflight decision can be audited.
- **Step Context Budget:**

  > Use no model tokens or screenshots on the normal path. Budget one GET, at most one redirect chain, a bounded response size, one parser pass, and one compact evidence row. Allow at most one Playwright escalation and one selector wait; dual-run sampling should be capped by predictor confidence and canary policy. Record bytes, redirects, parse milliseconds, browser seconds, selectors, and evidence size.
- **Recommendation:**

  > ADOPT for an immediate read-only shadow canary, then enforce only high-precision denials. It is Python-native, Apache-2.0, model-free, cheap, observable, and directly targets known preflight waste without touching submit safety. Start with explicit expired/dead/duplicate/policy signatures, return `unknown` on ambiguity, cap browser escalation, and promote each denial rule only after audited false-denial evidence. Do not use it as an application actor.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `cache_replay_yield`
- `confidence_intervals`
- `deterministic_coverage`
- `escape_rate`
- `existing_session_attach`
- `extension_support`
- `headless_headed_parity`
- `historical_bucket_impact`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `playwright_cdp_reuse`
- `projected_cost_per_verified_apply`
- `startup_latency`
- `verification_latency`

### Amazon Nova Act

Source record: `Amazon_Nova_Act.json`

#### Identity

- **Name:** Amazon Nova Act
- **Category:** premium_agent
- **Official Sources:**

  > - https://aws.amazon.com/nova/act/<br>- https://aws.amazon.com/nova/pricing/<br>- https://docs.aws.amazon.com/nova-act/<br>- https://docs.aws.amazon.com/ai/responsible-ai/nova-act/overview.html<br>- https://docs.aws.amazon.com/nova-act/latest/userguide/bedrock-agentcore.html<br>- https://docs.aws.amazon.com/nova-act/latest/userguide/load-balancer-limits.html<br>- https://docs.aws.amazon.com/nova-act/latest/userguide/data-encryption.html<br>- https://pypi.org/project/nova-act/
- **Maintenance Status:**

  > Active production AWS service. Current documentation describes the Nova Act AWS service, Python SDK, CLI, IDE extension, AgentCore integration, CloudWatch metrics, IAM controls, and a custom Nova 2 Lite computer-use model. PyPI published SDK 3.4.187.0 on 2026-04-30, and AWS documentation and pricing were current in July 2026.
- **License:**

  > The Python SDK declares Apache-2.0, which is compatible with ApplyPilot's AGPL-3.0 codebase when notices are preserved. The managed service remains commercial and subject to AWS Service Terms or the nova.amazon.com Terms of Use, depending on IAM versus API-key authentication.

#### Cost Economics

- **Pricing Model:**

  > Managed Nova Act workflows cost $4.75 per agent-hour based on real elapsed working time. Each parallel agent bills independently. Time waiting for a human in a Human-in-the-Loop state is excluded. API-key local prototyping is free with daily limits; production IAM use is billed.

#### Reliability

- **Failure Modes:**

  > Dynamic or missing elements, model planning errors, ActError, timeout, step exhaustion, IAM denial, throttling, network/browser failure, auth and OTP, CAPTCHA requiring takeover, unsupported or sensitive fields, prompt injection, page-policy refusal, and false completion without durable ATS evidence. A crash after submit can leave an ambiguous result.
- **Crash Recovery:**

  > Workflow runs, sessions, Acts, statuses, trace locations, logs, CloudWatch metrics, and caught ActError exceptions support diagnosis and controlled retry. Human-pending and client-action states allow pauses. There is no documented transactional resume or exactly-once recovery across an interrupted submit; ApplyPilot must quarantine and reconcile before retry.
- **Exactly Once Submit Safety:**

  > CreateAct accepts a clientToken for API-request idempotency, but that does not make a third-party job submission exactly once. ApplyPilot must retain its lease, job/account dedup key, plan hash, pre-submit checkpoint, one-time submit authorization, network/DOM evidence, and ambiguous-result quarantine. Nova Act should not receive unrestricted submit authority in the first canary.
- **Verification Evidence Tier:**

  > Nova Act can expose the Playwright page, structured act_get results, logs, screenshots/trajectory data, workflow traces, and external tools. Highest-confidence verification still requires a deterministic ApplyPilot tool to capture an ATS/application ID or successful submit response; confirmation DOM is secondary, email is delayed corroboration, and agent narration alone is insufficient.

#### Applypilot Fit

- **Python Integration:**

  > Strong. Nova Act provides a Python 3.10+ SDK with synchronous and asyncio interfaces, Pydantic schemas through act_get, external Python tools, direct Playwright page access during unlocked tool contexts, CLI deployment, and AWS IAM authentication. ApplyPilot should isolate and pin the SDK and map each Act to attempt_id.
- **Observability Joinability:**

  > Good with explicit instrumentation. Workflow, workflow-run, session, and Act IDs; status/timestamps; traceLocation; console logs; CloudWatch Invocations, Latency, UserErrors, SystemErrors, and Throttles; and AgentCore OpenTelemetry can join to ApplyPilot attempt_id. Persist submit evidence and cost seconds in apply_result_events because AWS metrics alone do not prove application completion.

#### Authentication

- **Login And Otp:**

  > Strong HITL primitives. Local headed mode can pause for a person, custom unlocked tools can collect credentials without sending them through the model prompt, and AgentCore Browser Tool provides live takeover for CAPTCHA or sensitive data. ApplyPilot should keep OTP and unexpected consent owner-controlled and verify authenticated state before resuming.
- **Session Data Boundary:**

  > The service receives prompts and screenshots and temporarily stores agent trajectory data, including prompt, screenshots, and agent response, encrypted in AWS-owned-key DynamoDB/S3. Optional indefinite traces can be written to a customer S3 bucket. TLS 1.2+ protects transit; customer-managed KMS keys and PrivateLink are not currently supported. Local API-key use is also subject to nova.amazon.com terms and collection disclosures.

#### Quality And Safety

- **Required Field Completeness:**

  > Nova Act supports structured Pydantic extraction and can be prompted to identify or fill forms, but no native guarantee covers all visible, conditional, or iframe-required controls. ApplyPilot needs a deterministic pre-submit inventory, browser validity checks, conditional-field rescan, and refusal when the answer plan is incomplete.
- **Answer Provenance:**

  > External Python tools and structured schemas can restrict values to ApplyPilot profile, resume, approved-answer, and job evidence. Nova Act does not inherently prove provenance for free-form generated answers. Pass source IDs through deterministic lookup/fill tools, prohibit fabrication, and keep demographic or sensitive responses owner-controlled.
- **Unsupported Question Fallback:**

  > Use a bounded Act to classify the field, then return PENDING_HUMAN_ACTION or invoke a customer tool for unknown, sensitive, legal, demographic, compensation, work-authorization, or unsupported free-text questions. Persist needs_review and stop before submit rather than guessing.
- **Irreversible Action Guard:**

  > The service supports requested human oversight and PENDING_HUMAN_ACTION states. For ApplyPilot, split final submit into a deterministic one-time tool requiring valid lease, allowed host, complete sourced plan, dedup check, explicit submit policy, and fresh page signature. Start canaries with submit disabled or human-approved.

#### Operations

- **Concurrency:**

  > AWS states that Nova Act can run thousands of workflows in parallel, subject to account quotas and regional adjustments. Default data-plane quotas include 100 TPS for workflow/session/Act creation and 5 TPS for InvokeActStep, all adjustable except resource limits such as 200 steps per Act. Cost scales linearly per concurrently working agent.
- **Warm Session Reuse:**

  > A workflow run can contain sessions and multiple Acts, and local interactive mode can keep the SDK-owned browser open for successive act calls. Reuse should be bounded by account/profile lock, clean-tab reset, idle timeout, health probe, and evidence separation between jobs.
- **Tracing And Replay:**

  > Nova Act exposes workflow traces, trajectory data, logs, traceLocation, CloudWatch metrics, and AgentCore OpenTelemetry. These support diagnosis but do not document deterministic browser-action replay or transactional mid-Act resume. Save customer-controlled trace artifacts and convert repeated stable signatures into existing deterministic Playwright adapters.
- **Deployment Modes:**

  > Local Python SDK with API key or IAM, async and synchronous execution, IDE extension, online playground, CLI-packaged deployment to AWS, managed Nova Act workflow service, Bedrock AgentCore Runtime and Browser Tool, and hybrid workflows that invoke external local or remote tools.
- **Canary Stop Loss:**

  > Use per-route wall-time and dollar budgets derived from $4.75/hour, a small Act/step cap well below the 200-step service maximum, IAM/domain/file restrictions, CloudWatch error/throttle alarms, and a zero-tolerance breaker for duplicate submit or provenance violations. Quarantine any submit ambiguity and stop an ATS route when p95 cost or verified completion misses the control.

#### Benchmark

- **Benchmark Design:**

  > Run matched ATS-stratified comparisons against deterministic adapters and the current fallback. Include repeated synthetic forms with conditional controls, uploads, iframes, hostile text, auth, takeover, crash, and ambiguous confirmation; use unique randomized live jobs to avoid duplicates. Capture active billed seconds, steps, human minutes, route fallback, and evidence tier.
- **Route Funnel:**

  > Track eligibility, selected route, AWS/session created, authenticated state, deterministic preflight, Act started, step/tool counts, HITL requested and resolved, required plan complete, provenance complete, submit guard approved, submit tool called, network/DOM confirmation, fallback before submit, ambiguous quarantine, delayed email reconciliation, and final disposition.
- **Recommendation:**

  > CANARY as a premium long-tail and human-takeover comparator, not the default route. Keep Ashby and Greenhouse deterministic-first, exclude Workday initially, enforce deterministic provenance and submit/verification tools, cap active time near five minutes, and promote only ATS signatures whose all-in cost per positive apply beats the current fallback without weakening safety.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `billing_granularity`
- `browser_version_matrix`
- `cache_replay_yield`
- `confidence_intervals`
- `control_plane_overhead`
- `deterministic_coverage`
- `escape_rate`
- `existing_session_attach`
- `extension_support`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `model_cost`
- `persistent_profiles`
- `playwright_cdp_reuse`
- `projected_cost_per_verified_apply`
- `prompt_injection_resistance`
- `site_permission_and_terms`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`

### BrowserGym and AgentLab evaluation harness

Source record: `BrowserGym_and_AgentLab_evaluation_harness.json`

#### Identity

- **Name:** BrowserGym and AgentLab evaluation harness
- **Category:** verification
- **Official Sources:**

  > - BrowserGym repository and documentation: https://github.com/ServiceNow/BrowserGym<br>- BrowserGym releases: https://github.com/ServiceNow/BrowserGym/releases<br>- BrowserGym core environment implementation: https://github.com/ServiceNow/BrowserGym/blob/main/browsergym/core/src/browsergym/core/env.py<br>- BrowserGym action-space documentation: https://browsergym.readthedocs.io/latest/core/action_space.html<br>- AgentLab repository and documentation: https://github.com/ServiceNow/AgentLab<br>- AgentLab releases: https://github.com/ServiceNow/AgentLab/releases<br>- BrowserGym Ecosystem paper, TMLR 2025: https://openreview.net/forum?id=5298fKGmv3<br>- BrowserGym Ecosystem paper preprint: https://arxiv.org/abs/2412.05467<br>- BrowserGym Apache-2.0 license: https://github.com/ServiceNow/BrowserGym/blob/main/LICENSE<br>- AgentLab Apache-2.0 license: https://github.com/ServiceNow/AgentLab/blob/main/LICENSE
- **Maintenance Status:**

  > Actively maintained since January 2025. BrowserGym published v0.14.3 on 2026-01-20 after multiple 2025 releases, and AgentLab published v0.4.2 on 2026-01-20. Recent BrowserGym work added WebArena Lite, WebArena Verified, OpenApps integration, fixes, and AgentLab API preparation. Both repositories also have active issue and pull-request queues.
- **License:**

  > BrowserGym and AgentLab are Apache-2.0 licensed. That permissive license is compatible with use and modification inside ApplyPilot's AGPL-3.0 codebase, subject to preserving the Apache license, notices, attribution, and any notices for separately installed benchmark dependencies. Individual benchmark websites, datasets, and containers can have separate licenses and terms.

#### Cost Economics

- **Pricing Model:**

  > Open-source, self-hosted evaluation software with no BrowserGym or AgentLab usage fee. Costs are local or cloud browser compute, benchmark hosting and reset infrastructure, model tokens, trace storage, engineering for custom ATS tasks and graders, and human review of ambiguous traces. AgentLab supports OpenAI, Azure, OpenRouter, and self-hosted TGI, so model billing follows the selected provider.
- **Billing Granularity:**

  > There is no BrowserGym or AgentLab billing unit. Browser and Ray workers consume resources for each job, idle warm workers consume RAM, traces consume storage, and benchmark services may remain running between studies. Model providers bill by their own token, request, subscription, or hosted-endpoint units. WebArena resets can add roughly five minutes before evaluation according to AgentLab documentation.

#### Reliability

- **Failure Modes:**

  > - Synthetic pages or graders do not represent real ATS widgets, conditional logic, uploads, redirects, or confirmation evidence<br>- A task reward reports success even though required fields, answer provenance, or exactly-once submission constraints were violated<br>- BrowserGym, Playwright, browser, model, benchmark container, or live benchmark changes reduce reproducibility<br>- Ray jobs hang, time out, exhaust RAM, or leave benchmark state contaminated between tasks<br>- Shared WebArena-style state creates ordering dependencies or requires slow instance resets<br>- Dynamic live-web tasks vary by geography, language, account state, or site changes<br>- Full DOM, accessibility trees, screenshots, or traces expose candidate data or secrets<br>- An unrestricted Python action or hostile page content causes prompt injection, arbitrary navigation, or unsafe tool use<br>- Headless and headed modes or different viewport settings produce materially different task behavior<br>- A benchmark score is overfit and fails to predict cost, quality, or verified completion in live canaries
- **Crash Recovery:**

  > AgentLab can load a saved Study, find incomplete or errored tasks, and relaunch them; its Ray backend can terminate jobs that exceed a timeout. ExpResult and study directories preserve per-job results for diagnosis. This is appropriate before irreversible actions. A restarted episode is not transaction replay, so any synthetic or live task that might have crossed submit must persist submit_started and quarantine rather than automatically rerun.
- **Exactly Once Submit Safety:**

  > Not built in as a cross-task guarantee. BrowserGym executes actions and task-specific validation, while AgentLab schedules jobs and retries incomplete work. ApplyPilot must create a grader and state machine that records a durable pre-submit checkpoint, allows exactly one submit action, detects duplicate requests, refuses automatic retry after an ambiguous click, and treats duplicate or false-positive success as a zero-tolerance benchmark failure.
- **Verification Evidence Tier:**

  > Custom tasks can grade exact synthetic server state or response IDs as the strongest evidence, then known confirmation DOM and URL, with screenshots and action traces as supporting artifacts. Built-in scalar reward alone is insufficient for ApplyPilot. The ATS suite should emit the same evidence tiers used by production: submission ID or response, confirmation DOM, email fixture, screenshot, and explicit inference-only status.

#### Applypilot Fit

- **Python Integration:**

  > Strong. BrowserGym exposes Gymnasium environments and BrowserGym/AgentLab are Python packages; AgentLab requires Python 3.11 or 3.12 and provides Python APIs for studies, custom agents, results, and analysis. ApplyPilot can wrap its existing Python deterministic adapters and agent controller behind an AgentArgs implementation and export results into its database.
- **Playwright Cdp Reuse:**

  > Partial. BrowserGym itself uses Playwright and accepts Chromium and BrowserContext keyword arguments, so selectors, page concepts, screenshots, and some fixture utilities are reusable. Its core BrowserEnv launches a new Chromium browser and creates a new context on reset; it does not natively attach to ApplyPilot's already-running CDP browser/profile. Production CDP reuse needs a custom environment or should remain outside the synthetic harness.
- **Ats Fit:**

  > Strong as an extensible framework for synthetic Ashby, Greenhouse, Lever, Workable, Workday, and long-tail form replicas because new benchmarks can inherit AbstractBrowserTask and define setup and validation. It has no bundled ATS benchmark, application schema, resume handling, login workflow, or confirmation-email grader. Fit therefore depends entirely on ApplyPilot-authored fixtures and task validity.
- **Control Plane Overhead:**

  > Moderate for agent evaluations: BrowserGym extracts screenshot, DOM snapshot, merged accessibility tree, element properties, URL, page list, last action, errors, and elapsed time after steps; AgentLab serializes experiment configuration and results and may use Ray workers. Deterministic adapters can bypass most model context but still incur environment extraction unless a lean custom observation path is used.
- **Observability Joinability:**

  > Good after an ApplyPilot exporter is added. AgentLab study directories, ExpResult, summary dataframes, screenshots, actions, step information, seeds, agent arguments, benchmark names, and reproducibility metadata can be joined to attempt_id, route, ATS, fixture version, adapter commit, model, token usage, and production canary cohort. There is no native ApplyPilot database join key.

#### Authentication

- **Persistent Profiles:**

  > Weak by default for production authentication. BrowserGym creates a fresh browser and context during reset and closes them afterward, although Playwright context options can seed controlled storage state. Use synthetic accounts and fixture storage states in benchmarks. Do not point it at a user's default Chrome profile; production profile locking, cloning, versioning, and ownership remain ApplyPilot responsibilities.
- **Login And Otp:**

  > Use seeded synthetic accounts, deterministic OTP fixtures, and explicit owner-takeover simulation to test state transitions without real credentials. BrowserGym can pause for a user message in interactive mode, but AgentLab does not provide an ApplyPilot inbox relay or secure OTP service. Real login, passkey, CAPTCHA, or suspicious-auth prompts must remain supervised and outside unattended benchmark automation.
- **Session Data Boundary:**

  > Self-hosting keeps browser state, screenshots, traces, prompts, and results on ApplyPilot-controlled machines except model requests sent to the configured provider and traffic to any live benchmark site. AgentLab results default under AGENTLAB_EXP_ROOT. Synthetic tasks should use fake candidate data; redact cookies, CSRF tokens, resumes, emails, and screenshots before external model calls or retained artifacts.

#### Quality And Safety

- **Required Field Completeness:**

  > Implement this as a first-class task invariant, not merely a final reward. Each synthetic form should expose a server-side expected required-field set, conditional branches, hidden and custom controls, and post-fill validation. The grader must fail if the route submits with any missing, stale, duplicated, or invalid required value and should report unmapped_required separately.
- **Answer Provenance:**

  > BrowserGym does not enforce provenance. The ApplyPilot agent wrapper and task server should tag every answer with profile, resume, approved-answer, policy, verifier, or human source; reject generated claims without an approved source; and grade both value correctness and provenance. Hostile page text must never become an authoritative answer source.
- **Unsupported Question Fallback:**

  > Create benchmark cases for novel free text, sensitive questions, contradictory prompts, and missing evidence. Passing behavior is a structured unresolved-field disposition or approved human/verifier handoff before submit, with no guessed answer. Record fallback class, cost, latency, and whether previous deterministic work is safely reused.
- **Prompt Injection Resistance:**

  > BrowserGym includes broad action primitives and its documented Python action can execute code with the active Playwright page, which the docs label unsafe. Use a restricted HighLevelActionSet, ATS origin allowlists, no arbitrary Python or shell action for model-controlled agents, fake secrets, egress controls, and DoomArena-style hostile-page fixtures. Grade secret disclosure, off-domain navigation, instruction override, and submit-boundary violations.
- **Site Permission And Terms:**

  > Synthetic locally hosted ATS replicas are the preferred use because they avoid live-site side effects and can be licensed and rate-controlled by ApplyPilot. Built-in live or self-hosted benchmarks have their own setup and terms. BrowserGym's repository warns that it is research software rather than a consumer product; capability does not authorize unattended applications or bypass of login, CAPTCHA, access controls, or site restrictions.
- **Irreversible Action Guard:**

  > Represent submit as a separately permissioned action available only after a complete typed plan and durable checkpoint. Synthetic tasks should count server-side submit calls, inject crashes before and after acceptance, and fail on more than one call, fallback after ambiguous acceptance, or success without strong evidence. Live canaries must retain ApplyPilot's independent enablement gates and stop-loss controls.

#### Operations

- **Concurrency:**

  > AgentLab uses Ray for parallel experiments and documents that 10-50 jobs can often run on one computer depending on RAM. WebArena-style state dependencies reduce safe concurrency, and AgentLab does not currently support evaluation across multiple benchmark instances. Synthetic ATS fixtures should isolate state per attempt and begin with a conservative worker count based on measured browser memory.
- **Warm Session Reuse:**

  > AgentLab can keep parallel workers active across jobs, but BrowserGym's standard reset tears down and recreates task, context, chat, and browser. Reuse model clients, fixture services, downloaded browser binaries, task catalogs, and analysis caches; do not assume authenticated browser or tab reuse. A lean custom environment may reuse a browser while creating isolated contexts if profiling justifies it.
- **Tracing And Replay:**

  > Strong experiment inspection but not deterministic transaction replay. AgentLab ExpResult exposes per-step screenshots, actions, and step information; AgentXray visualizes ongoing or completed traces, and studies can reload incomplete results. BrowserGym can record viewport video. Add Playwright traces, network and console capture, fixture server logs, and model token records. Replay only against synthetic fixtures and never blindly replay submit on a live ATS.
- **Deployment Modes:**

  > Local Python environment, self-hosted workstation or fleet machine, containerized benchmark services, and Ray-backed parallel execution are supported patterns. Built-in benchmarks range from static files and datasets to self-hosted Docker services, hosted demo instances, and live web. There is no required managed BrowserGym cloud. A CI image with pinned browsers and local synthetic ATS services is the recommended ApplyPilot mode.
- **Canary Stop Loss:**

  > Gate progression from unit fixtures to synthetic BrowserGym studies, then shadow on real pages without submit, then a tiny unique-job live canary. Enforce per-step, episode, token, dollar, and wall-clock caps; zero duplicate submissions, false APPLIED states, secret leaks, wrong answers, or challenge interaction; and pause a route on regression versus its pinned baseline or on any ambiguous submit.

#### Benchmark

- **Benchmark Design:**

  > Build an ApplyPilot benchmark package with matched ATS-stratified synthetic tasks for Ashby, Greenhouse, Lever, Workable, Workday, and long-tail patterns. Cover standard and conditional required fields, custom selects, dates, uploads, consent, iframes, redirects, validation errors, expired jobs, auth and OTP handoff, hostile instructions, delayed confirmation, duplicate clicks, and crashes around submit. Run deterministic, semantic-agent, vision-agent, and human-fallback routes on identical seeded cases with at least three repeats per case/model configuration. Hold out fixture variants, pin all software and task versions, and correlate synthetic metrics with later route-tagged live canaries.
- **Route Funnel:**

  > Export discovered -> eligible -> assigned route -> environment ready -> plan complete -> fallback requested -> required fields complete -> pre-submit checkpoint -> submit attempted -> server accepted -> DOM confirmed -> delayed evidence reconciled -> final disposition. Separate task coverage from completion among attempted tasks, and retain failure class, evidence tier, model and token cost, action count, elapsed time, and synthetic/live cohort on every row.
- **Recommendation:**

  > Adopt for synthetic and shadow evaluation, not as a production browser route. Implement a small ApplyPilot-specific BrowserGym package and use AgentLab for seeded parallel studies, timeout handling, trace analysis, and matched route comparisons. Start with Greenhouse, Ashby, and Workable-like fixtures plus submit-crash and hostile-page cases. Keep existing Playwright/CDP profiles and deterministic adapters in production, export benchmark results into ApplyPilot attempt IDs and route metadata, and require demonstrated correlation with live canary outcomes before using synthetic scores as promotion gates.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `browser_version_matrix`
- `cache_replay_yield`
- `confidence_intervals`
- `deterministic_coverage`
- `escape_rate`
- `existing_session_attach`
- `extension_support`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `model_cost`
- `projected_cost_per_verified_apply`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`

### Independent deterministic submission verifier

Source record: `Independent_deterministic_submission_verifier.json`

#### Identity

- **Name:** Independent deterministic submission verifier
- **Category:** verification
- **Official Sources:**

  > - ApplyPilot cost-quality router design: C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot/.worktrees/codex-apply-cost-quality-router-phase1/docs/superpowers/specs/2026-07-06-apply-cost-quality-router-design.md<br>- ApplyPilot durable result-event schema: C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot/.worktrees/codex-apply-cost-quality-router-phase1/src/applypilot/fleet/schema_v3.sql<br>- ApplyPilot launcher result-source handling: C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot/.worktrees/codex-apply-cost-quality-router-phase1/src/applypilot/apply/launcher.py<br>- HTTP Semantics, idempotent methods: https://www.rfc-editor.org/rfc/rfc9110.html#name-idempotent-methods<br>- Greenhouse Job Board API: https://developers.greenhouse.io/job-board.html
- **Maintenance Status:**

  > Architecture proposal, not a separately released product. Its prerequisites are active in the current worktree: append-only apply_result_events, route/failure metadata, durable transcript and job-log references, and final_result_source. A dedicated verifier service and evidence-state machine are not yet implemented.
- **License:**

  > ApplyPilot-owned implementation would remain AGPL-3.0. Referenced web standards and ATS documentation are specifications, not bundled runtime dependencies; no new copyleft compatibility issue is introduced.

#### Cost Economics

- **Pricing Model:**

  > Self-hosted per-attempt verification pipeline. Primary cost is deterministic network/DOM checks, local artifact storage, delayed email reconciliation, and rare human review; no model is required for normal evidence classification.
- **Billing Granularity:**

  > Local compute and storage have no API minimum. Email providers may have quota units; browser or inbox polls should be bounded and batched. Human review is charged only for unresolved evidence.
- **Model Cost:**

  > Zero for rule-based evidence extraction and state transitions. A model may summarize artifacts for an operator but must not decide positive confirmation without deterministic evidence.

#### Reliability

- **Failure Modes:**

  > - Actor closes the page or process before evidence is durably captured<br>- Submit request is opaque, redirected, or returns a generic success code<br>- Confirmation DOM or URL changes and rule becomes stale<br>- Email is delayed, missing, duplicated, or cannot be matched to the attempt<br>- Verifier reads mutable actor narration instead of independent artifacts<br>- Posting accepts duplicate submissions without exposing an application ID<br>- Evidence contains secrets or candidate data that is retained too long
- **Crash Recovery:**

  > Persist submit_started, request IDs, payload digest, URL, actor artifacts, and evidence checks before terminal classification. Verifier restarts are safe because checks are read-only; actor crashes after submit remain quarantined until evidence resolves and must never trigger an automatic resubmit.
- **Exactly Once Submit Safety:**

  > Core value. Separate actor and verifier roles, permit only the actor to issue the irreversible request once, persist a durable submission token/state, and let the verifier transition only to confirmed, definite_failure, or ambiguous. Any submit_started attempt blocks all fallback submitters.
- **Verification Evidence Tier:**

  > Tier 1: ATS application ID or explicit accepted response tied to request/payload. Tier 2: allowlisted success URL plus confirmation DOM. Tier 3: matched confirmation email. Screenshot/trace support higher tiers but are insufficient alone; actor text or button disappearance is inference only.

#### Applypilot Fit

- **Python Integration:**

  > Excellent. Implement as typed Python evidence collectors and a small state machine beside the current launcher, queue writer, repair report, and email pipeline. It can run synchronously after action and asynchronously for delayed reconciliation.
- **Playwright Cdp Reuse:**

  > Excellent. Reuse current Playwright pages, CDP network events, dedicated profiles, screenshots, traces, and fixtures without changing the actor. The verifier consumes copied immutable artifacts or read-only page state rather than sharing submit authority.
- **Control Plane Overhead:**

  > Low: a bounded set of network events, current URL, allowlisted DOM markers, artifact hashes, and optional inbox queries. Avoid sending full traces or page text to a model.
- **Observability Joinability:**

  > Excellent. Use apply_result_events as the attempt anchor and join queue name/URL, worker, route, request ID, payload digest, submit timestamp, evidence tier, confirmation method, application ID, email event, artifact hashes, verifier version, and final disposition.

#### Authentication

- **Persistent Profiles:**

  > Verifier can reuse the actor's dedicated profile for immediate read-only checks but should not own profile lifecycle. Delayed reconciliation should prefer stored evidence and email so a profile lock or browser shutdown does not block classification.
- **Existing Session Attach:**

  > Supported through current Playwright/CDP attachment for immediate checks. Validate attempt/tab identity and copy evidence before the actor closes the page; never attach to an unrelated user tab or click anything during verification.
- **Login And Otp:**

  > Verifier does not perform login or OTP. It may classify an auth wall, observe a verified session, and consume matched inbox evidence. Owner-supervised auth remains a separate route.
- **Extension Support:**

  > No extension required. A narrowly permissioned content script could capture immutable confirmation metadata in an owner-controlled tab, but CDP/network and DOM collectors are preferable to reduce permissions.
- **Session Data Boundary:**

  > Keep artifacts local to the worker/fleet database and dedicated inbox. Store hashes and redacted extracts by default; encrypt application IDs, screenshots, response bodies, cookies, resumes, and email content with short retention and strict operator access.

#### Quality And Safety

- **Required Field Completeness:**

  > Outside the verifier's action role, but it should require the actor's pre-submit completeness attestation and schema digest as evidence. Missing attestation is a policy failure before submit, not a post-submit success signal.
- **Answer Provenance:**

  > Read and retain the actor's field-level provenance digest without inventing answers. Confirmation proves acceptance, not answer truth; quality audits must remain separately queryable.
- **Unsupported Question Fallback:**

  > Before submit, route to semantic or human resolution. After submit_started, unsupported or unclear evidence routes only to delayed reconciliation or human review, never another actor.
- **Prompt Injection Resistance:**

  > Strong if evidence rules use allowlisted hosts, status/headers, typed response schemas, exact URL patterns, fixed DOM selectors, and matched email metadata. Treat page and email text as untrusted data and never execute instructions found in evidence.
- **Irreversible Action Guard:**

  > The verifier must be technically incapable of clicking submit or sending application requests. It validates the actor's durable submit token and can only record evidence, schedule read-only reconciliation, or quarantine.

#### Operations

- **Concurrency:**

  > High for read-only checks, bounded by inbox/API quotas and artifact I/O. Serialize checks per attempt and use a unique attempt/submission key so two verifiers cannot produce conflicting terminal states.
- **Warm Session Reuse:**

  > Excellent. Reuse event subscriptions and inbox connections while isolating evidence buffers by attempt. Clear page-specific state after durable write and never carry confirmation markers between jobs.
- **Tracing And Replay:**

  > Store immutable event timelines, request/response metadata, DOM marker results, screenshots, and email-match decisions. Replay the classification rules offline against saved redacted fixtures; never replay the submit request.
- **Deployment Modes:**

  > In-process post-submit collector, local sidecar, fleet reconciliation worker, or hybrid immediate-plus-delayed service. Keep the authoritative state in fleet Postgres and deploy owner/home schema changes before remote read-only workers.
- **Browser Version Matrix:**

  > Browser-neutral at the state-machine level. Immediate collectors need certification for ApplyPilot's Chromium/CDP and Playwright versions; email and offline artifact reconciliation do not depend on browser version.
- **Canary Stop Loss:**

  > Shadow-classify existing outcomes first. Stop on any verifier-caused action, duplicate terminal transition, false positive, cross-attempt evidence match, secret leak, or retry after submit_started. Promote only with zero false confirmations and at least 95% agreement on adjudicated fixtures.

#### Benchmark

- **Benchmark Design:**

  > Build synthetic ATS fixtures for accepted response with ID, redirect success, DOM-only success, validation failure, timeout before request, crash after request, duplicate email, delayed email, stale confirmation marker, and hostile text. Run matched shadow classification on at least 100 historical/live attempts stratified by ATS before changing retry policy.
- **Route Funnel:**

  > Record actor eligible, pre-submit attested, submit token committed, request started, response observed, actor ended, immediate evidence tier, delayed checks scheduled, email matched, human adjudication, confirmed/definite_failure/ambiguous, and retry suppression outcome.
- **Step Context Budget:**

  > One attempt metadata record, bounded network events for the submit window, current URL, a small allowlisted DOM result, one screenshot hash, and zero model tokens. Delayed checks query only attempt-correlated inbox metadata before retrieving content.
- **Recommendation:**

  > ADOPT incrementally. Build this before expanding low-cost submit routes because it separates action from truth, improves exactly-once safety, and makes cost per positively verified application measurable. Begin with shadow classification and offline replay, then make ambiguous quarantine authoritative only after zero false positives on adjudicated canaries.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `cache_replay_yield`
- `confidence_intervals`
- `deterministic_coverage`
- `escape_rate`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `projected_cost_per_verified_apply`
- `site_permission_and_terms`
- `startup_latency`
- `verification_latency`
- `verified_completion_rate`

### Magnitude

Source record: `Magnitude.json`

#### Identity

- **Name:** Magnitude
- **Category:** vision_agent
- **Official Sources:**

  > - https://github.com/magnitudedev/browser-agent<br>- https://docs.magnitude.run/getting-started/introduction<br>- https://docs.magnitude.run/core-concepts/playwright<br>- https://docs.magnitude.run/core-concepts/agent-options<br>- https://docs.magnitude.run/core-concepts/compatible-llms<br>- https://docs.magnitude.run/advanced/memory<br>- https://docs.magnitude.run/testing/building-test-cases<br>- https://docs.magnitude.run/testing/running-tests
- **Maintenance Status:**

  > Maintained but lower-velocity as of 2026-07-09. The public TypeScript repository is not archived, has about 604 commits and 4,100 stars, and was last pushed on 2026-02-08. Material post-January-2025 work includes Playwright/CDP access, visual assertions, provider configuration, prompt caching, parallel workers, and a February 2026 token-accounting fix.
- **License:**

  > Apache-2.0 for the open-source framework, compatible with ApplyPilot's AGPL-3.0 codebase when license and notice obligations are preserved. Selected model providers and any enterprise support remain subject to separate terms.

#### Cost Economics

- **Pricing Model:**

  > No Magnitude framework fee for self-hosting. Marginal cost is the chosen visually grounded LLM's token pricing plus local or hosted browser compute. Magnitude defaults to Claude Sonnet 4 when an Anthropic key is present and also supports Qwen2.5-VL through OpenAI-compatible providers, Anthropic, OpenAI, Google, Bedrock, Azure, and local endpoints.

#### Reliability

- **Failure Modes:**

  > Visual grounding misses, wrong coordinates after layout shift, stale screenshot, planning/instruction failure, context growth, token/rate limit, Playwright or CDP disconnect, profile lock, popup/tab confusion, cross-origin iframe, file upload, auth/OTP/challenge, conditional required fields, visual assertion false positives, unsupported question, and ambiguous submit after crash.
- **Crash Recovery:**

  > Playwright artifacts, narration, token events, screenshots, video, and direct Page/Context hooks can aid diagnosis. Tests can fail fast or continue. Magnitude does not document transactional action replay or mid-submit resume; restart only from a pre-submit checkpoint and quarantine any crash after a submit-touching action.
- **Exactly Once Submit Safety:**

  > No built-in job deduplication or exactly-once third-party submit primitive exists. Magnitude's act and visual checks are not idempotency controls. ApplyPilot must intercept or remove generic submit clicks and expose one deterministic submit command with lease, job/account dedup, plan hash, one-time authorization, response capture, and ambiguity quarantine.
- **Verification Evidence Tier:**

  > Magnitude provides natural-language visual checks plus direct Playwright Page/Context access for DOM, network, cookies, and browser instrumentation. Use deterministic network/DOM extraction to capture ATS/application ID or successful response as primary evidence; visual checks and screenshots are secondary and agent/test pass state alone is insufficient.

#### Applypilot Fit

- **Python Integration:**

  > Weak to moderate. Magnitude is TypeScript/Node-first, whereas ApplyPilot is Python. Integration requires a sidecar process, JSON-RPC/HTTP/CLI adapter, or reimplementation of route contracts. Direct in-process Python support is not documented.
- **Playwright Cdp Reuse:**

  > Strong on the Node side. startBrowserAgent accepts browser.cdp to connect to an open CDP-enabled browser, launchOptions can enable a debugging port, and agent.page/context expose the underlying Playwright objects. ApplyPilot needs exclusive tab/profile ownership and must avoid simultaneous Python and Magnitude control.
- **Observability Joinability:**

  > Moderate. Narration, tokensUsed telemetry, test duration/token telemetry, Playwright Page/Context instrumentation, screenshots, optional video, and provider billing can be joined through a sidecar-supplied attempt_id. Magnitude does not provide ApplyPilot-native queue, email, or result-event joins; build a stable event envelope and hash artifacts.

#### Authentication

- **Existing Session Attach:**

  > Documented first-class CDP attachment through browser.cdp, plus direct access to the active Page and BrowserContext. Attach only to a dedicated ApplyPilot profile/tab after a health probe; do not allow concurrent controllers or attach to a user's general browsing profile.
- **Session Data Boundary:**

  > Self-hosted browser state, screenshots, and artifacts can remain on the worker, but screenshots, prompts, history, and data sent to a remote model leave the machine under that provider's terms. The test runner collects basic anonymized duration and token telemetry by default unless telemetry:false. A local compatible model avoids model egress but adds GPU operations.

#### Quality And Safety

- **Required Field Completeness:**

  > Visual acts and checks can reason about rendered forms, but they do not guarantee a complete inventory of visible, conditional, off-screen, or iframe required controls. Add deterministic DOM/accessibility and validity scans before planning and submit, trigger conditional sections, and refuse unknown required fields.
- **Answer Provenance:**

  > Magnitude accepts structured data passed to act and custom prompts, but does not enforce source provenance. Use a signed ApplyPilot answer plan and deterministic lookup/fill bridge carrying source IDs; prohibit the model from inventing employment, demographic, authorization, or compensation facts.
- **Unsupported Question Fallback:**

  > Wrap each act in an ApplyPilot policy layer that classifies unknown, sensitive, demographic, legal, work-authorization, compensation, and unsupported free text. Stop the sidecar with needs_review and hand the dedicated browser to an owner; never let a visual model guess and continue to submit.
- **Irreversible Action Guard:**

  > Magnitude can click any visible control through act, so the sidecar must block generic interaction with recognized submit elements. Only a deterministic ApplyPilot submit RPC may cross the boundary after lease, provenance, completeness, dedup, host policy, and fresh page-signature checks. Start in no-submit observation mode.

#### Operations

- **Warm Session Reuse:**

  > A BrowserAgent can retain its Page/Context across multiple acts, and CDP attachment can reuse an existing browser. Safely reuse only after clean-tab reset, profile lock, storage/download cleanup, health probe, model-cost meter reset, and an idle timeout to prevent cross-job leakage.
- **Tracing And Replay:**

  > Playwright access supports screenshots, network listeners, routes, and configured recordVideo; narration and test results retain semantic steps. Native deterministic caching was described as in progress, so no production replay guarantee exists. Capture Playwright trace/HAR in the wrapper and promote stable sequences to deterministic ApplyPilot code.
- **Deployment Modes:**

  > Local Node/TypeScript library and test runner, CI anywhere Playwright runs, container or self-hosted sidecar, local or remote LLM providers including OpenAI-compatible endpoints, local or remote CDP browser, and hybrid deterministic Playwright plus model-assisted acts. No Magnitude-managed browser cloud is documented.
- **Canary Stop Loss:**

  > Enforce limits in the ApplyPilot sidecar: domain allowlist, maximum acts/model calls/tokens/wall time/dollars, one pre-submit retry, no post-submit retry, profile lock, and artifact capture. Trip immediately on duplicate submit, provenance violation, or secret exposure; quarantine missing confirmation and stop an ATS route when p95 cost or completion misses control.

#### Benchmark

- **Benchmark Design:**

  > Compare deterministic adapters, current fallback, Magnitude with Sonnet, and Magnitude with a cheaper/local grounded model on matched ATS-stratified tasks. Include synthetic conditional fields, responsive layouts, visual-only controls, uploads, iframes, hostile content, auth, crash, and confirmation. Use repeated runs and unique live jobs with positive evidence.
- **Route Funnel:**

  > Track eligibility, sidecar started, CDP/profile acquired, auth valid, deterministic inventory, visual route selected, acts/model calls/tokens, plan complete, provenance complete, submit guard approved, submit RPC called, network/DOM evidence, fallback before submit, human recovery, ambiguous quarantine, delayed email reconciliation, and final disposition.
- **Recommendation:**

  > WATCH, then run a small no-submit comparator canary. Magnitude's visual assertions, direct Playwright/CDP access, and provider flexibility are useful research controls, but the TypeScript boundary, unproven ATS completion, February 2026 last push, and missing packaged safety/HITL controls make it weaker than Python-native candidates for immediate production routing.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `billing_granularity`
- `browser_version_matrix`
- `cache_replay_yield`
- `concurrency`
- `confidence_intervals`
- `control_plane_overhead`
- `deterministic_coverage`
- `escape_rate`
- `extension_support`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `login_and_otp`
- `marginal_cost_per_attempt`
- `migration_effort`
- `model_cost`
- `persistent_profiles`
- `projected_cost_per_verified_apply`
- `prompt_injection_resistance`
- `site_permission_and_terms`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`

### Microsoft Fara and local small computer-use models

Source record: `Microsoft_Fara_and_local_small_computeruse_models.json`

#### Identity

- **Name:** Microsoft Fara and local small computer-use models
- **Category:** vision_agent
- **Official Sources:**

  > - https://github.com/microsoft/fara<br>- https://huggingface.co/microsoft/Fara-7B<br>- https://www.microsoft.com/en-us/research/articles/fara1-5-computer-use-agent/<br>- https://github.com/microsoft/magentic-ui<br>- https://huggingface.co/microsoft/Fara-7B-onnx<br>- https://huggingface.co/datasets/microsoft/WebTailBench

#### Reliability

- **Failure Modes:**

  > Coordinate grounding error, stale screenshot, planning loop failure, local model hallucination, critical-point stop, verifier error, browser/CDP crash, model-server OOM or timeout, GPU driver/runtime incompatibility, profile lock, popup/tab confusion, auth/OTP/challenge, unsupported fields, upload/iframe failure, prompt injection, and false success without network/DOM confirmation.
- **Crash Recovery:**

  > The open harness emits action histories and evaluation trajectories, and the repository includes verifier/error-taxonomy work, retry caps, and Playwright automation. There is no documented transactional resume or exactly-once submit recovery. Save pre-submit state and artifacts, restart only before submit, and quarantine any crash after an irreversible action.
- **Exactly Once Submit Safety:**

  > Fara's critical-point training is a useful stop boundary: job application submission should require permission or sensitive data. It is not third-party transaction idempotency. Keep submit outside the model action set and expose a deterministic one-time ApplyPilot tool with lease, dedup key, plan hash, policy approval, response capture, and ambiguous-result quarantine.
- **Verification Evidence Tier:**

  > The harness and CUAVerifierBench support screenshot/action-trajectory verification, and direct Playwright code can capture DOM/network evidence. For production, require ATS/application ID or successful submit response first, explicit confirmation DOM second, delayed email corroboration third; model/verifier judgment or screenshot alone must not mark applied.

#### Applypilot Fit

- **Python Integration:**

  > Strong at the language boundary. The Fara repository installs as a Python package, exposes fara-cli, uses Playwright, and serves models through OpenAI-compatible vLLM/SGLang endpoints. ApplyPilot can integrate in Python, but model serving and the experimental harness should run as supervised processes with pinned versions.
- **Observability Joinability:**

  > Moderate to strong if built into the wrapper. The open harness produces trajectories and action history; local model servers expose request metrics; Playwright can record network, DOM, screenshots, video, and traces. Attach ApplyPilot attempt_id to every model request and artifact and persist model hash, quantization, hardware, critical-point state, evidence IDs, and energy/cost.

#### Authentication

- **Login And Otp:**

  > Fara's model card explicitly treats signing in, entering personal information, and submitting applications as critical points where the agent should stop. Magentic-UI offers a human-in-the-loop experience. ApplyPilot should collect OTP/sensitive values outside model context, use a deterministic fill step, obtain permission, and verify login before resuming.
- **Session Data Boundary:**

  > Self-hosted Fara can keep screenshots, cookies, resumes, prompts, traces, and model inference on owned hardware, which is its main privacy advantage. Downloads of weights/code and optional telemetry/provider services still have their own boundaries. Restrict model-server network access, encrypt profiles/artifacts, and prevent page content from invoking filesystem or shell capabilities.

#### Quality And Safety

- **Required Field Completeness:**

  > Screenshot reasoning can notice rendered controls, but cannot guarantee all off-screen, conditional, accessibility-only, or iframe required fields. Combine it with deterministic DOM/accessibility and browser-validity inventories before planning and again before submit; stop on unknown required controls.
- **Answer Provenance:**

  > Fara predicts actions from task, screenshots, and history; it does not inherently constrain form answers to approved evidence. Supply a signed ApplyPilot answer plan and deterministic lookup/fill tools with source IDs, and forbid the model from generating personal, demographic, work-authorization, compensation, or employment claims.
- **Unsupported Question Fallback:**

  > Use Fara's critical-point behavior plus an ApplyPilot classifier to stop for sensitive, unknown, legal, demographic, compensation, work-authorization, communication, and submit decisions. Return needs_review to an owner or deterministic policy engine; never reinterpret a stop as permission to guess.
- **Irreversible Action Guard:**

  > Fara's documented critical points include submitting job applications, making it well aligned with a hard permission boundary. Enforce that boundary in code by removing generic submit capability and requiring a deterministic one-time tool after lease, provenance, completeness, dedup, host-policy, and fresh-page checks. Begin with no-submit canaries.

#### Operations

- **Warm Session Reuse:**

  > Local model servers and Playwright browsers can remain warm across attempts, reducing load time. Reuse only with exclusive profile/account locks, clean-tab and download reset, model/request state reset, health checks, idle timeout, and artifact separation to avoid leaking one application into another.
- **Tracing And Replay:**

  > The repository includes evaluation trajectories, action histories, screenshots, verifier pipelines, and error taxonomy; Playwright can add trace/HAR/video. These support audit and offline scoring but not deterministic action replay or mid-submit resume. Promote stable form signatures into ApplyPilot adapters rather than replaying coordinate actions blindly.
- **Canary Stop Loss:**

  > Use sandbox/domain allowlists, a hard critical-point stop, maximum steps/tokens/wall time/energy, GPU OOM supervision, one pre-submit retry, no post-submit retry, and zero tolerance for duplicate submit, provenance violations, or secret exposure. Quarantine missing confirmation and trip route-level breakers by ATS/model/hardware.

#### Benchmark

- **Benchmark Design:**

  > Compare deterministic adapters, current premium fallback, Fara-7B, and any officially released Fara1.5 small model on matched ATS strata. Include repeated synthetic forms with conditional controls, visual-only widgets, uploads, iframes, prompt injection, auth, critical points, OOM/crash, and confirmation; use unique live jobs and positive evidence.
- **Route Funnel:**

  > Track eligibility, local model ready, browser/profile acquired, auth valid, deterministic inventory, Fara selected, steps/tokens/joules, critical point triggered, plan/provenance complete, permission/submit guard approved, deterministic submit called, network/DOM evidence, fallback before submit, human recovery, ambiguous quarantine, email reconciliation, and final disposition.
- **Recommendation:**

  > WATCH and run a bounded, no-submit Fara-7B lab canary as the owned-hardware cost floor. Do not route production applications to it yet. Keep submit deterministic and permissioned, compare energy and fully loaded cost against premium agents, and reassess Fara1.5 only after official weights, harness, license, and ATS-specific evidence are available.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `billing_granularity`
- `browser_version_matrix`
- `cache_replay_yield`
- `concurrency`
- `confidence_intervals`
- `control_plane_overhead`
- `deployment_modes`
- `deterministic_coverage`
- `escape_rate`
- `existing_session_attach`
- `extension_support`
- `headless_headed_parity`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `license`
- `maintenance_status`
- `marginal_cost_per_attempt`
- `migration_effort`
- `model_cost`
- `persistent_profiles`
- `playwright_cdp_reuse`
- `pricing_model`
- `projected_cost_per_verified_apply`
- `prompt_injection_resistance`
- `site_permission_and_terms`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`

### WebMCP

Source record: `WebMCP.json`

#### Identity

- **Name:** WebMCP
- **Category:** watchlist
- **Official Sources:**

  > - WebMCP Draft Community Group Report: https://webmachinelearning.github.io/webmcp/<br>- Official specification repository: https://github.com/webmachinelearning/webmcp<br>- Chrome for Developers WebMCP documentation: https://developer.chrome.com/docs/ai/webmcp<br>- Chrome WebMCP origin trial: https://developer.chrome.com/origintrials/#/view_trial/4460761240475074561<br>- Official demos: https://github.com/GoogleChromeLabs/webmcp-tools
- **Maintenance Status:**

  > Very active but pre-standard. The specification is a Web Machine Learning Community Group Draft dated 2026-07-08, explicitly not a W3C Standard or Standards Track document. Chrome documents a flag for local testing and an origin trial from Chrome 149; APIs and security behavior remain subject to change.
- **License:**

  > Specification contributions are under the W3C Community Contributor License Agreement; the specification repository includes its own license terms. WebMCP is a browser API rather than a library dependency, so ApplyPilot's AGPL-3.0 compatibility depends mainly on any client/polyfill code selected later.

#### Cost Economics

- **Pricing Model:**

  > No protocol fee. Cost is browser execution plus the agent/model that chooses structured tools; sites bear implementation cost. Origin-trial and browser support, not vendor billing, are the current constraint.
- **Billing Granularity:**

  > No WebMCP billing unit. Browser and model costs retain their own granularity; a visible browsing context must remain open, and complex sites may incur additional implementation and state-management overhead.

#### Reliability

- **Failure Modes:**

  > - ATS does not implement WebMCP or exposes only part of the application flow<br>- Browser/client lacks the experimental API or origin-trial token<br>- Tool description, schema, implementation, or page state becomes inconsistent<br>- Navigation during form submission loses the originating document/tool response<br>- Tool poisoning, output injection, over-parameterization, or misrepresented intent<br>- Visible-context requirement conflicts with headless fleet deployment<br>- Tool returns success text without durable ATS acceptance evidence
- **Crash Recovery:**

  > Not specified as workflow persistence. ApplyPilot must checkpoint before invoking an irreversible tool, capture invocation/result/navigation evidence, and quarantine a crash after invocation. Re-discovering a tool after navigation is not permission to call it again.
- **Exactly Once Submit Safety:**

  > Not provided by the API. Tool schemas and annotations describe behavior but the draft notes there is no mechanism to verify implementation matches description. Require ApplyPilot submission tokens, one invocation authority, durable submit_started, idempotency evidence when available, and no retry after ambiguity.
- **Verification Evidence Tier:**

  > Structured tool output can be evidence only when tied to a trustworthy ATS application ID or accepted backend response. Combine it with URL/DOM and email evidence; a tool's natural-language description or returned success string alone is not positive confirmation.
- **Headless Headed Parity:**

  > Poor today. Chrome's official documentation explicitly requires an open browser tab or webview and says there is no headless support. Any claimed headless bridge is outside the documented browser API and needs separate evaluation.

#### Applypilot Fit

- **Python Integration:**

  > Weak to medium today. WebMCP is a JavaScript/Document browser API with no official Python ApplyPilot client. Python could inspect/invoke tools through CDP or an extension bridge, but that client contract is experimental and custom.
- **Control Plane Overhead:**

  > Potentially very low: tool names, descriptions, JSON input schemas, and structured outputs replace repeated DOM interpretation. The browser may still include screenshots/accessibility context in observations, and tool discovery requires visiting the page.
- **Observability Joinability:**

  > Potentially strong if the client records document/origin, tool ID/name/version, schema digest, arguments digest, invocation timestamp, output, navigation, permissions decision, and evidence tier under ApplyPilot attempt_id. The draft does not define ApplyPilot-level tracing.

#### Authentication

- **Persistent Profiles:**

  > Uses the browser's normal authenticated context and cookies; it does not define profile creation, cloning, locking, or portability. ApplyPilot must retain dedicated profiles and one-controller ownership.
- **Login And Otp:**

  > WebMCP tools can reflect authenticated page state, but the standard provides no OTP relay or account recovery service. Login and user-decision gates remain owner-supervised unless the site explicitly provides safe, policy-compliant tools.
- **Extension Support:**

  > The explainer includes agents running in extensions, and Chrome offers an inspector extension for testing. Production extension permissions, distribution, and tool invocation semantics remain client responsibilities.
- **Session Data Boundary:**

  > Tool execution occurs in the page's browser context with existing cookies. Inputs may disclose candidate/profile data to the site, while the browser agent may observe tool metadata and page context. Enforce origin allowlists, minimum parameters, redacted logs, and no cross-site personalization leakage.

#### Quality And Safety

- **Required Field Completeness:**

  > Potentially strong when the ATS exposes a complete JSON schema or declarative form tool, but completeness is controlled by the site and dynamic state. ApplyPilot must compare exposed requirements with current form state and refuse partial plans.
- **Answer Provenance:**

  > Structured parameters improve field binding but do not supply truthful values. ApplyPilot must map every argument to profile, resume, approved policy, or verified-answer provenance and reject site requests for unnecessary attributes.
- **Unsupported Question Fallback:**

  > Do not invent missing tool arguments. Fall back before invocation to deterministic DOM mapping, a bounded semantic answerer, or human review; after an irreversible tool call, only verify or quarantine.
- **Prompt Injection Resistance:**

  > Mixed. Structured schemas reduce arbitrary DOM interpretation, but the draft explicitly identifies metadata/description poisoning, output injection, malicious implementation, and intent misrepresentation. Use trusted-origin allowlists, fixed tool policies, argument minimization, read-only hints as hints only, and an independent verifier.
- **Irreversible Action Guard:**

  > Require an explicit ApplyPilot approval boundary for any submit-capable tool, show or log exact arguments and origin, commit submit_started before invocation, and verify independently afterward. Do not trust a readOnlyHint or natural-language description as an enforceable contract.

#### Operations

- **Warm Session Reuse:**

  > Good conceptually because tools are bound to the active authenticated Document and normal browser session. Re-observe after navigation or state change; never reuse stale tool definitions or outputs across attempts.
- **Tracing And Replay:**

  > The draft defines tool observations and invocation behavior, not a trace/replay product. ApplyPilot must log definitions, schema digests, arguments, outputs, navigation, and network evidence and replay only classifier/client logic against fixtures.
- **Deployment Modes:**

  > Experimental Chrome flag for local development, Chrome 149 origin trial for participating sites, and in-browser agents/extensions. Official documentation says no headless operation; broad cross-browser or managed-service deployment is not established.
- **Canary Stop Loss:**

  > Detection-only watch mode first. Stop on unexpected tool exposure, argument overreach, misrepresented side effects, cross-origin confusion, false confirmation, duplicate invocation, or secret leakage. Never enable submit authority during an origin trial without independent verification and a tiny route cap.

#### Benchmark

- **Benchmark Design:**

  > Create synthetic ATS pages with imperative and declarative tools, dynamic required fields, upload, navigation, stale tools, malicious descriptions, output injection, over-parameterization, login, and delayed confirmation. Compare tool route versus deterministic Playwright and agent control; use real jobs only after a production ATS exposes tools.
- **Route Funnel:**

  > Record browser supported, origin-trial enabled, page visited, tools observed, trusted origin, schema accepted, arguments provenance-complete, approval granted, tool invoked, output received, navigation handled, independent evidence confirmed, fallback, ambiguous quarantine, and final disposition.
- **Recommendation:**

  > WATCH. Add a lightweight capability detector and synthetic security benchmark, but do not allocate production submit authority or projected savings. Reassess when a major ATS exposes stable production tools, Chrome support leaves experimental status, headless/external-client behavior is defined, and independent confirmation semantics pass ApplyPilot canaries.

#### Uncertain Fields

- `adapter_maintenance_tco`
- `ats_fit`
- `browser_version_matrix`
- `cache_replay_yield`
- `concurrency`
- `confidence_intervals`
- `deterministic_coverage`
- `escape_rate`
- `existing_session_attach`
- `historical_bucket_impact`
- `human_recovery_cost`
- `infrastructure_cost`
- `marginal_cost_per_attempt`
- `migration_effort`
- `model_cost`
- `playwright_cdp_reuse`
- `projected_cost_per_verified_apply`
- `site_permission_and_terms`
- `startup_latency`
- `step_context_budget`
- `verification_latency`
- `verified_completion_rate`
