# P1 Fail-Closed Controls Design

## Goal

Close three reliability defects found in the OTP/Gmail audit: responder overload reported as healthy, dead-man infrastructure failures reported as success, and an unverifiable LinkedIn fleet lock treated as inactive.

## Design

### OTP responder overload

`otp_relay.answer_pending()` will continue to fail closed when the pending-request or mailbox-candidate snapshot exceeds its safety bound, but it will raise a typed operational exception instead of returning a successful zero-answer result. The responder loop will record a privacy-safe error heartbeat and will not publish an `idle` heartbeat for that cycle. In `--once` mode, a failed cycle exits nonzero.

### Dead-man infrastructure failure

The dead-man CLI will return nonzero when it cannot connect, migrate, check, or persist monitor state. It will also write a local `fleet-ALERT.txt` fallback containing only the failure class and a generic monitoring-failure message, ensuring that database connection details and credentials are not persisted.

### LinkedIn ownership interlock

The fleet lock probe will return a three-state result: active, inactive, or unknown. A configured but unreachable fleet database produces unknown. Job acquisition will treat active and unknown as reasons to exclude LinkedIn while allowing offsite ATS work to continue. An absent fleet DSN remains equivalent to inactive because no fleet coordinator was configured.

## Verification

Add focused regression tests for overload propagation, responder heartbeat and one-shot status, dead-man failure status and fallback alert, and fleet-lock unknown behavior. Run the full OTP, dead-man, launcher, supervisor, and browser lifecycle suites, Python compilation, PowerShell parsing, and `git diff --check`.
