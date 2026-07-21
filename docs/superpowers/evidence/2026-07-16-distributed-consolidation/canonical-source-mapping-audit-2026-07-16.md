# Canonical Source Mapping Audit

Date: 2026-07-16

Source receipt: `canonical-sqlite-source-receipt-2026-07-16.json`

## Results

| Check | Result | Import disposition |
|---|---:|---|
| Jobs without a URL | 0 | Import all jobs |
| Duplicate job URLs | 0 | URL-to-job identity is deterministic |
| FitMap labels whose URL is absent from `jobs` | 291 | Quarantine with source row and reason |
| Pairwise left endpoints absent from `jobs` | 0 | Import all pairwise rows |
| Pairwise right endpoints absent from `jobs` | 0 | Import all pairwise rows |
| Email events with a non-null unmatched job URL | 0 | Import all resolvable email rows |
| Reviewed outcomes with an unmatched job URL | 0 | Import all outcomes |
| Reviewed outcomes with an unmatched email event | 0 | Import all outcomes |
| Job decisions with an unmatched job URL | 0 | Import all decisions |

## Policy and decision shape

- Policy versions: 14 total, 9 ATS and 5 LinkedIn.
- All 14 source policy rows are currently `draft`; none may be activated during import.
- Job decisions: 701,658 total, 543,018 ATS and 158,640 LinkedIn.
- Decision actions: 699,066 `review`, 2,592 `reject`, and 0 `apply`.
- Qualification verdicts: 773 `qualified`, 698,293 `uncertain`, and 2,592 `unqualified`.

## Required importer behavior

1. Use the source receipt SHA-256 as the migration fingerprint and reject any
   source that does not match it.
2. Preserve the 291 unresolved labels in
   `brain_migration_quarantine`; do not discard them and do not synthesize
   `brain_jobs` rows from labels alone.
3. Import all resolvable rows with deterministic source IDs and idempotent
   `(source_namespace, source_event_id)` keys.
4. Create policy partitions before importing decisions, while keeping every
   policy in `draft` lifecycle.
5. Run count and endpoint parity after import. A successful artifact upload or
   migration batch completion is not sufficient evidence of parity.
