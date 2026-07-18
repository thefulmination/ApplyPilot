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
wrapper artifact is absent, its byte length or media type conflicts, or its
registered provenance does not identify the expected snapshot reference.

## Artifact-ledger provenance

Schema V6 registration writes `brain_artifacts.provenance` with two fields:
`authority_request_id` and the manifest's opaque `policySourceId` string.
No schema or registration-function change is required.

For snapshot-reference artifacts only, `policySourceId` is the UTF-8 decoding
of the same deterministic RFC 8785 JSON bytes stored as the wrapper content.
Consequently it is the closed object with exactly `kind`, `lane`,
`policyVersion`, `role`, `schemaVersion`, `sourceField`, and
`sourceSha256`. The compiler descriptor carries this string so a downstream
authority-manifest builder cannot substitute policyVersion alone.

Registration and lifecycle validation parse
`brain_artifacts.provenance->>'policy_source_id'` as JSON only when the
binding role is one of `label_snapshot`, `pairwise_snapshot`, or
`outcome_snapshot`. They require exact equality with the expected closed
object. Other artifact roles retain their existing opaque-string
`policySourceId` semantics.

## Normalization and duplicate handling

Lane is not case-folded or trimmed. The existing source compiler accepts only
the exact normalized values `ats` and `linkedin`; every wrapper copies that
validated value.

Each source row has one column per snapshot role, so compilation can emit at
most one descriptor for each role. The ordered descriptor tuple is the
canonical de-duplication boundary. The importer keeps the existing
`(policy_version, artifact_role)` conflict check: an identical binding is
idempotent, while a different wrapper hash fails closed. Content-addressed
ledger insertion may coalesce byte-identical artifacts, but policy identity in
the wrapper means different policies do not normally share snapshot-reference
bytes even when they name the same source fingerprint.

## Exact TDD acceptance cases

1. A populated ATS row emits three snapshot-reference descriptors in
   `label_snapshot`, `pairwise_snapshot`, `outcome_snapshot` order.
   Each descriptor's content equals the expected RFC 8785 bytes; its SHA-256
   equals the digest of those bytes and differs from `sourceSha256`; its media
   type is the existing RFC 8785 JSON media type; and its `policy_source_id`
   equals the UTF-8 wrapper content.
2. Recompiling semantically identical input produces byte-for-byte equal
   descriptors with no timestamp or environment field.
3. Each null snapshot column omits only its corresponding role. A row with all
   three null columns emits no snapshot descriptor and preserves null source
   metadata.
4. A fourteen-row fixture with the committed candidate shape reports exact
   totals: the sum of emitted snapshot descriptors equals the number of
   non-null snapshot columns, and the six ATS/LinkedIn candidate identities
   produce six descriptors.
5. Importer tests preload the wrapper descriptors in the artifact ledger and
   assert each policy binding equals the wrapper content hash, never the source
   fingerprint.
6. Parity source projection recomputes the same three role/hash pairs as the
   importer, so source and target binding arrays are equal.
7. Import fails closed when a wrapper artifact is absent, when byte length or
   media type differs, and when an existing role binding names a different
   hash.
8. Existing lifecycle tests retain the exact ten-role requirement without any
   role rename or additional role.

## Verification

Run the focused policy compiler, importer, and parity tests; Ruff the changed
Python and test files; byte-compile the changed Python modules; and run
`git diff --check`. The final report states the exact emitted role order,
zero-to-three per-policy count behavior, six known candidate wrappers, and
the content-hash-versus-source-hash distinction.
