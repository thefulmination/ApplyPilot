# P1 Fail-Closed Controls Design

## Goal

Close three reliability defects found in the OTP/Gmail audit: responder overload reported as healthy, dead-man infrastructure failures reported as success, and an unverifiable LinkedIn fleet lock treated as inactive.

## Design

- OTP request or candidate overflow raises a typed operational failure. The responder records an error heartbeat, omits the idle heartbeat, and exits nonzero in one-shot mode.
- Dead-man infrastructure failure exits nonzero and writes a generic local fallback alert without persisting exception or credential text.
- The fleet lock probe returns active, inactive, or unknown. Configured but unverifiable coordination excludes LinkedIn while offsite ATS work continues.

## Verification

Run focused regression tests, the combined OTP/dead-man/lifecycle matrix, Ruff, Python compilation, PowerShell parsing, and `git diff --check`.
