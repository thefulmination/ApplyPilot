# ApplyPilot Local Inference and Cost Benchmark

**Date:** 2026-07-10  
**Decision:** Use `qwen3:8b` only for bounded, verifier-backed application answers. Do not let any tested local model own browser navigation, retry safety, eligibility termination, or submission confirmation.

## Historical Cost Baseline

These numbers come from the live ApplyPilot ledgers, not public API price cards. The snapshots were read-only.

| Ledger | Coverage | Recorded cost | Useful interpretation |
|---|---:|---:|---|
| Postgres `apply_queue` | 9,884 jobs; 916 applied | $1,904.94 cumulative | **$2.08 all-in cost per successful application** |
| Postgres `apply_result_events` | 2,521 attempts; 408 applied | $1,279.87 | $3.14 per success in the event cohort |
| SQLite `llm_usage`, `apply_agent` | 40 calls | $31.87 | Mean $0.80; median $0.61; p95 $1.78; max $3.70 |
| SQLite `llm_usage`, all stages | 76,739 calls | $197.56 | $158.70 is scoring, not browser applying |

The direct-call average near $0.70-$0.80 is real, but it is not the cost per successful application. Failed and parked attempts consume paid calls too. The Postgres queue ledger is the appropriate current CPA numerator. The ledgers have different coverage and must not be added together.

### Event Spend By Outcome

| Bucket | Attempts | Cost | Share |
|---|---:|---:|---:|
| Successful applications | 408 | $412.24 | 32.2% |
| Budget exhaustion | 70 | $235.60 | 18.4% |
| Expired | 670 | $168.07 | 13.1% |
| Browser/MCP infrastructure | 497 | $149.21 | 11.7% |
| No confirmation | 50 | $117.53 | 9.2% |
| Other failures | 419 | $82.40 | 6.4% |
| `no_result_line` | 262 | $65.77 | 5.1% |
| Location ineligible | 136 | $37.57 | 2.9% |
| Authentication/verification | 9 | $11.48 | 0.9% |

Failure attempts consumed $867.63, or 67.8% of event-cohort spend. Budget exhaustion, browser infrastructure, and `no_result_line` alone account for $450.58, but this is an addressable upper bound, not guaranteed savings.

### Host Economics

Host family uses the recorded effective target host, including `grnh.se` as Greenhouse.

| Host | Attempts | Applied | Success rate | Cost | Cost/success |
|---|---:|---:|---:|---:|---:|
| Ashby | 341 | 138 | 40.5% | $154.79 | $1.12 |
| Greenhouse | 355 | 100 | 28.2% | $251.78 | $2.52 |
| SmartRecruiters | 19 | 7 | 36.8% | $18.74 | $2.68 |
| Other | 1,266 | 151 | 11.9% | $603.62 | $4.00 |
| Lever | 24 | 2 | 8.3% | $16.55 | $8.28 |
| Workday | 516 | 10 | 1.9% | $234.39 | $23.44 |

Hard-parking Workday before any paid call would have removed 18.3% of event-cohort spend while deferring 2.45% of confirmed successes. As a historical counterfactual, cohort CPA falls from $3.14 to $2.63. Workday should therefore remain outside the general agent lane until it has a bounded adapter or a separately approved exception policy.

The existing Greenhouse adapter has six recorded zero-model-cost submissions: two positively confirmed and four `adapter_no_confirmation`. This is promising cost evidence, but 2/6 confirmation is not production-ready. Confirmation reliability is the next adapter defect to fix.

### CPA Targets

Holding 916 successes constant:

| Target CPA | Total allowed cost | Required savings | Reduction |
|---|---:|---:|---:|
| $1.25 | $1,145.00 | $759.94 | 39.9% |
| $1.00 | $916.00 | $988.94 | 51.9% |
| $0.70 | $641.20 | $1,263.74 | 66.3% |

Successful attempts alone cost $412.24 / 408 = $1.01 in the event cohort. Therefore, eliminating failed attempts cannot by itself reach $0.70. That target requires replacing paid inference on successful paths with deterministic adapters and local inference too.

## Benchmark Design

- 229 historical Tarpon transcripts, stratified across nine outcomes.
- 24 profile-grounded open-ended questions from 12 historical job descriptions.
- 16 synthetic ATS states and an 11-tool catalog; no browser was launched.
- Transcript `RESULT:` lines were removed for classification, direct identifiers were redacted, and raw text stayed outside Git.
- Independent validation found no email patterns, phone patterns, known personal literals, or raw prompt/answer/profile fields in result files.
- Temperature 0, 4,096-token context, sequential requests.

The classification task intentionally includes control-plane labels such as timeout and `no_result_line` that are not always inferable from narrative text. Raw accuracy therefore understates some model capability. Unsafe-retry false positives remain decisive because retry safety must never be guessed.

## Tarpon Runtime

- Intel Core i9-13900HX, 32 GB RAM, RTX 4070 Laptop GPU with 8,188 MiB VRAM.
- Ollama 0.31.2 installed under `C:\ApplyPilot\.local-inference-benchmark`.
- Endpoint bound only to `127.0.0.1:11434`; no firewall rule, Tailscale exposure, or `LLM_URL` wiring.
- `qwen3:4b-instruct` and `qwen3:8b` run fully on GPU.
- `gpt-oss:20b` runs with 57% CPU / 43% GPU offload and required Ollama's bundled CUDA 12 backend on this host.

During GPT-OSS answer generation, the GPU component averaged 24.5 W over a 30-second sample. A deliberately conservative 200 W whole-system scenario at $0.30/kWh costs about $0.00004 for a 2.4-second Qwen call and $0.00070 for a 42-second GPT-OSS call. Electricity is not literally free, but it is orders of magnitude below the historical API calls; hardware depreciation and maintenance are excluded.

## Model Results

| Model | Outcome accuracy | Unsafe retry FPs | Tool exact | Verifier-clean answers | Median latency |
|---|---:|---:|---:|---:|---:|
| Qwen3 4B Instruct | 49.3% of 146 usable; 19 parse errors | 26 | 9/16 | 18/24 | 2.28 s |
| Qwen3 8B | 35.8% of 165 | 59 | 11/16 | **23/24** | 2.39 s |
| GPT-OSS 20B | Not scored; structured final output was usually empty | Not scored | **13/16** | 15/24 | 37.94 s |

No model emitted an out-of-state `submit_application` call in the synthetic tool suite. That does not make them authoritative:

- Qwen3 4B incorrectly called `report_applied` on a validation-error state.
- Qwen3 8B attempted profile filling before inspecting the form and mishandled post-submit ambiguity.
- GPT-OSS omitted the required park action for an unknown question and called `stop_ineligible` on a fixable validation error.
- Qwen classification produced 26 unsafe-retry false positives; Qwen3 8B produced 59.
- GPT-OSS high reasoning exhausted its output budget and returned eight empty final answers.

The winning bounded route is Qwen3 8B answer generation followed by the existing deterministic verifier. The one failed answer was caught as `fabricated_company`, so it can be escalated without reaching a form.

## Recommended Cost Architecture

1. **Deterministic preflight before any model call.** Resolve the effective ATS URL; check duplicate, liveness, location, employment type, account requirement, host policy, and remaining turn budget. Workday parks here by default.
2. **Adapter-first execution.** Repair Greenhouse confirmation, add an Ashby adapter next, and keep Lever bounded by its CAPTCHA behavior. Adapter-ready jobs must not invoke a general browser agent.
3. **Deterministic browser state machine.** Playwright/WebDriver owns navigation, selectors, required-field checks, review, submit, and positive confirmation. Cache form fingerprints and field mappings by ATS/version.
4. **Qwen3 8B for open-ended text only.** Use profile/resume/job context, then run `verify_answer`. Any empty or flagged output escalates immediately; do not spend another local turn trying to persuade the model.
5. **Paid model as a one-turn exception.** Invoke it only for a pre-submit state that the adapter, profile rules, answer corpus, and local verifier could not resolve. Enforce one paid decision and a dollar cap per job.
6. **Human exception queue.** Login, CAPTCHA, unknown factual answers, email verification failures, and any maybe-submitted state park with a screenshot/state bundle.
7. **Deterministic terminal ownership.** Only code may mark applied, safe-to-retry, expired, or ineligible. Missing/invalid tool output becomes `park_exception` in the same process, not another paid turn.

Distributed compute helps through independent queue workers, not pooled VRAM. Tarpon should serve the always-on 8B answer lane; GGGTower can run a larger independent worker when online. Do not use unauthenticated llama.cpp RPC across the tailnet: its own documentation describes the RPC backend as insecure, and a critical unauthenticated RCE advisory remains relevant to this design.

## Next Canary

1. Add a disabled-by-default `local_answer` provider that calls Qwen3 8B through an authenticated, Tailscale-restricted queue or proxy. Keep Ollama itself loopback-only.
2. Shadow 100 real open-ended questions. Persist only hashes, verifier results, latency, fallback reason, and estimated displaced paid cost.
3. Accept a local answer only when the deterministic verifier returns no findings. Require zero unsupported accepted claims; verifier failures fall back once to the paid answerer.
4. Instrument cost by phase: preflight, navigation, answer, recovery, confirmation. Current SQLite coverage has only 40 `apply_agent` rows versus 2,521 Postgres events, so total savings cannot yet be attributed precisely by phase.
5. Separately canary the repaired Greenhouse confirmation path. Do not scale adapter-owned submit until positive confirmation is reliable and duplicate protection is proven.

## Verification

- Fleet remained `paused=true`, `ats_paused=true`, `canary_enabled=false`, `canary_remaining=0`.
- Applied count stayed 916; result events stayed 2,521 with max ID 2,551; `applied_set` stayed 2,690.
- Queued discoveries rose from 3,235 to 3,247 during the benchmark, but leased/applied/failed/blocked/crash counts did not move.
- No queue row has a benchmark lease/worker ID.
- Ollama listens only on `127.0.0.1`; `http://100.77.65.8:11434` timed out from the home machine.
- Only the pre-existing commented `LLM_URL` example exists; Git diff contains no `LLM_URL` change.
- The benchmark process exited 0. Ollama remains running locally, with live routing disabled.

Result SHA-256 hashes:

- `qwen3:4b-instruct`: `66a954a2dce4a14d02734695f8a7843f8b9838c97b0c0a8c420c087e5a26c7f1`
- `qwen3:8b`: `1d090fa58fc06eb164201b532a239fd639c9e8220e7962b33c841e48d9fcc218`
- `gpt-oss:20b` tools/answers: `5f979b9c397456ea80770c7394c1d093f3e1e8e9bcd7b08b81e1e906bade024a`

## External References

- Ollama Windows and standalone installation: https://docs.ollama.com/windows
- Context sizing: https://docs.ollama.com/context-length
- Tool calling and structured outputs: https://docs.ollama.com/capabilities/tool-calling and https://docs.ollama.com/capabilities/structured-outputs
- Qwen3 model sizes: https://ollama.com/library/qwen3
- GPT-OSS model characteristics: https://openai.com/index/introducing-gpt-oss/ and https://ollama.com/library/gpt-oss
- llama.cpp RPC security: https://github.com/ggml-org/llama.cpp/security and https://github.com/ggml-org/llama.cpp/security/advisories/GHSA-j8rj-fmpv-wcxw
