# Policy Snapshot Reference Materialization Design

## Goal

Materialize each non-null `label_snapshot`, `pairwise_snapshot`, and
`outcome_snapshot` source fingerprint as deterministic, content-addressed
JSON bytes. Policy bindings must reference the wrapper artifact hash rather
than treating the unavailable source-content fingerprint as an artifact hash.

## Boundary

This change is limited to pure policy compilation, importer binding, parity
projection, and focused tests. It does not change schema, database roles,
lifecycle gates, coordinator commands, live databases, or external storage.
The existing ten-role lifecycle contract remains unchanged.

The wrapper is explicitly a snapshot *reference*. It proves which source
fingerprint a policy row declared; it does not claim to contain or reproduce
the unavailable snapshot content.

## Canonical artifact

For every non-null snapshot field, the compiler emits one RFC 8785 JSON object:

```json
{
  "kind": "applypilot.policy.snapshot-reference",
  "lane": "ats",
  "policyVersion": "canonical-v7-ats-20260712",
  "role": "label_snapshot",
  "schemaVersion": 1,
  "sourceField": "label_snapshot",
  "sourceSha256": "951f869d6cca89c2b68dd8e88e1a072fced814b3086df11742a267a2f554d42a"
}
```

The exact field set is closed. It includes no timestamps, host data, random
identifiers, or environment state. `sourceField` and `role` are both
present intentionally: provenance identifies the SQLite column while role
identifies the lifecycle binding.

The existing RFC 8785 media type is retained. The descriptor SHA-256 is the
digest of the canonical wrapper bytes and therefore differs from the source
fingerprint except for a cryptographically negligible collision.

## Compilation and binding

`compile_policy_artifacts()` validates source fingerprints using the existing
lowercase SHA-256 rule and appends wrappers in source-field declaration order
after the currently compiled artifacts. Null source fields emit no wrapper.
Original fingerprints remain in `policy_metadata.sourceReferences`.

The importer requires each wrapper descriptor to be present and consistent in
the immutable artifact ledger, then binds `label_snapshot`,
`pairwise_snapshot`, or `outcome_snapshot` to the wrapper descriptor hash.
The source parity projection recomputes the same binding. Knowledge-graph and
replay behavior are unchanged.

## Coverage and counts

The compiler applies to all fourteen source policy rows, not only the two
canary candidates. Each policy gains zero through three wrapper artifacts,
exactly matching its non-null snapshot fields. The six committed candidate
fingerprints therefore produce six canonical wrapper artifacts: three ATS and
three LinkedIn.

## Failure behavior

Invalid non-null hashes continue to fail compilation. Wrapper serialization
uses the existing RFC 8785 error boundary. Import still fails closed when the
