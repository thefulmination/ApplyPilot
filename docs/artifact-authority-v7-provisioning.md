# Artifact authority v7 role provisioning

Artifact authority topology is a privileged provisioning boundary. The
permanent owner membership is exactly
`brain_artifact_authority_owner -> brain_schema_migrator`, with `ADMIN FALSE`,
`INHERIT FALSE`, and `SET TRUE`. Its grantor must be PostgreSQL bootstrap role
OID 10, and that role must still have `rolsuper = true`. The contract does not
depend on the bootstrap role's cluster-specific name.

When OID 10 owns the database, no provider membership is allowed. When a
dedicated control-plane provider owns the database, one additional permanent
membership is required: `brain_schema_migrator -> <current database owner>`,
with the same three options and grantor OID 10. The provider is exactly LOGIN,
NOSUPERUSER, NOCREATEROLE, and NOINHERIT, with no CREATEDB, REPLICATION, or
BYPASSRLS authority and no other role memberships. The writer role has no
descendants.

The permanent provider SET edge avoids a grant/revoke crash window during
schema migration. A non-superuser provider only migrates a graph that was
already provisioned exactly; it never creates or repairs topology. A genuine
CREATEROLE first-time attempt fails closed because PostgreSQL adds a creator
membership that does not match this contract.

`pg_database_owner` cannot have explicit members. A dedicated provider
therefore preserves that role's ownership of `public`, grants the fixed
migrator the schema grant options required by immutable historical migrations,
and performs migration through the permanent SET edge.
