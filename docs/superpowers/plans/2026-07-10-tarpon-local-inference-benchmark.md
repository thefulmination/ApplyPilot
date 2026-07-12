# Tarpon Local Inference Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure whether an always-on local model on Tarpon can replace paid inference for bounded application tasks without touching or submitting a live job application.

**Architecture:** Run Ollama on Tarpon bound only to `127.0.0.1:11434`, with models sized for its 8 GB RTX 4070 Laptop GPU. Build temporary benchmark inputs from labeled Postgres apply-result events and the corresponding local transcript logs, then test outcome classification, profile-grounded answer generation, and browser-tool selection using synthetic page states. Store only aggregate metrics and content hashes; do not copy raw application text into git.

**Tech Stack:** PowerShell, Tailscale SSH, Ollama for Windows, Qwen3 quantized models, Python standard library, PostgreSQL read-only queries

---

### Task 1: Freeze Safety Boundaries and Inventory Historical Coverage

**Files:**
- Create: `docs/superpowers/plans/2026-07-10-tarpon-local-inference-benchmark.md`
- Runtime only: Tarpon `C:\ApplyPilot\.local-inference-benchmark\`

- [x] **Step 1: Verify live apply workers remain paused before any benchmark call**

Run a read-only query against `worker_heartbeat` and `fleet_config`. Record the current gate and worker states in the benchmark manifest. Do not update either table.

- [x] **Step 2: Measure replayable historical coverage**

Run read-only counts for `apply_result_events`, including non-empty `job_log_path`, `transcript_digest`, `application_tool_calls`, status, and failure reason.

- [x] **Step 3: Confirm Tarpon transcript storage without reading unrelated user files**

Restrict the inventory to `C:\ApplyPilot\.applypilot\logs\*.txt` and `worker-*.log`. Record counts, byte totals, and timestamps only.

### Task 2: Install an Isolated Local Runtime on Tarpon

**Files:**
- Runtime only: `C:\ApplyPilot\.local-inference-benchmark\ollama\`
- Runtime only: `C:\ApplyPilot\.local-inference-benchmark\models\`
- Runtime only: `C:\ApplyPilot\.local-inference-benchmark\runtime.json`

- [x] **Step 1: Install the standalone Ollama runtime without administrator privileges**

Download the official `ollama-windows-amd64.zip` package and extract it under the benchmark directory. Launch `ollama serve` with `OLLAMA_MODELS` pointing to the benchmark model directory and `OLLAMA_HOST=127.0.0.1:11434`; do not add the binary to the user PATH, do not set `OLLAMA_HOST=0.0.0.0`, and do not add a firewall rule.

- [x] **Step 2: Pull two bounded candidate models**

```powershell
ollama pull qwen3:4b-instruct
ollama pull qwen3:8b
```

- [x] **Step 3: Verify GPU offload and context allocation**

```powershell
ollama ps
nvidia-smi.exe --query-gpu=name,memory.total,memory.used,memory.free --format=csv,noheader,nounits
```

Require the selected model to run predominantly on the GPU without exhausting Tarpon's browser capacity.

### Task 3: Build a Temporary, Labeled Replay Corpus

**Files:**
- Runtime only: `C:\ApplyPilot\.local-inference-benchmark\corpus.jsonl`
- Runtime only: `C:\ApplyPilot\.local-inference-benchmark\manifest.json`

- [x] **Step 1: Select a stratified event sample**

Sample equal-sized groups where available from `applied`, `no_result_line`, `timeout`, `budget_exhausted`, `no_confirmation`, browser-tool failures, location ineligibility, authentication, and verification failures. Use a fixed seed and record event IDs.

- [x] **Step 2: Join only the referenced Tarpon log files**

Reject paths outside `C:\ApplyPilot\.applypilot\logs`. Normalize invalid legacy encoding and cap each transcript at 20,000 characters.

- [x] **Step 3: Remove direct identifiers from model inputs**

Replace email addresses, phone numbers, postal addresses, LinkedIn URLs, and resume filenames with typed placeholders. Preserve action sequence and terminal-result evidence.

- [x] **Step 4: Store hashes instead of raw text in the manifest**

The manifest contains event ID, label, worker ID, transcript SHA-256, character count, and selection stratum. Raw corpus files remain on Tarpon and outside git.

### Task 4: Run Three Read-Only Benchmark Suites

**Files:**
- Runtime only: `C:\ApplyPilot\.local-inference-benchmark\run_benchmark.py`
- Runtime only: `C:\ApplyPilot\.local-inference-benchmark\results-*.jsonl`

- [x] **Step 1: Benchmark terminal outcome and failure classification**

Require schema-constrained JSON with `outcome`, `failure_class`, `submitted_or_maybe_submitted`, `safe_to_retry`, and `confidence`. Score exact outcome, macro-F1 by failure class, unsafe-retry false positives, and abstention coverage.

- [x] **Step 2: Benchmark profile-grounded application answers**

Use synthetic standard application questions whose expected values are generated deterministically from the profile. Score exact-match fields, unsupported-claim rate, and abstention behavior. Do not send profile data off Tarpon.

- [x] **Step 3: Benchmark browser-tool selection on synthetic ATS states**

Give the model a compact page-state schema and a fixed tool catalog. Score valid tool name, valid element reference, safety-rule compliance, submit-before-review violations, and unnecessary tool calls. No browser is launched.

- [x] **Step 4: Run both candidate models with deterministic settings**

Use temperature `0`, a 4,096-token context, fixed prompts, and sequential requests. Capture prompt/eval token counts, load duration, generation duration, and GPU memory.

### Task 5: Analyze Cost and Define the Routing Gate

**Files:**
- Runtime only: `C:\ApplyPilot\.local-inference-benchmark\summary.json`
- Create only after evidence exists: `docs/applypilot-local-inference-benchmark-2026-07-10.md`

- [x] **Step 1: Calculate quality at confidence thresholds**

Report accuracy and unsafe-action rates at confidence thresholds from 0.50 through 0.95. A local route is eligible only where unsafe-retry and unsupported-claim rates are zero in the evaluated sample.

- [x] **Step 2: Calculate projected API displacement**

Join eligible strata to historical attempt counts and historical spend. Report the fraction of calls and dollars that could be displaced; do not assume local classification replaces full browser-agent execution.

- [x] **Step 3: Keep live routing disabled**

Do not set `LLM_URL`, do not expose port 11434 over Tailscale, and do not modify worker environment files during this phase.

- [x] **Step 4: Recommend the next bounded canary**

The next phase may expose Tarpon through a Tailscale-restricted authenticated proxy and shadow local decisions beside the existing agent. It must remain non-authoritative until shadow disagreement and safety metrics pass an approved threshold.

### Task 6: Verification

**Files:**
- Verify: Tarpon runtime and benchmark artifacts only

- [x] **Step 1: Recheck network isolation**

Verify `11434` listens only on loopback and cannot be reached from the home machine over Tarpon's Tailscale address.

- [x] **Step 2: Recheck fleet isolation**

Verify no ApplyPilot worker environment contains a new `LLM_URL`, no queue row was leased by the benchmark, and no application status changed during the benchmark window.

- [x] **Step 3: Re-run the deterministic benchmark summary**

Recompute metrics from the JSONL outputs and require the same sample IDs, counts, and aggregate values.
