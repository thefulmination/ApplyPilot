"""Authenticated coordination for immutable brain-artifact authority manifests.

AWS verification deliberately completes before a database transaction is opened.
The database function is the final authority boundary and owns replay semantics.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO, Callable, Mapping


PURPOSE = "brain-artifact-authority-registration-v1"
SCHEMA_VERSION = 1
MAX_CLOCK_SKEW = timedelta(minutes=5)


class ArtifactAuthorityError(ValueError):
    """A manifest cannot be trusted or registered."""


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactAuthorityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json_strict(value: str | bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(value, object_pairs_hook=_object_no_duplicates)
    except ArtifactAuthorityError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactAuthorityError("invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ArtifactAuthorityError("JSON root must be an object")
    return parsed


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ArtifactAuthorityError("timestamp must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ArtifactAuthorityError("timestamp must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _required_keys(value: Mapping[str, Any], expected: set[str], where: str) -> None:
    missing = expected - value.keys()
    extra = value.keys() - expected
    if missing or extra:
        raise ArtifactAuthorityError(
            f"invalid {where} fields (missing={sorted(missing)}, extra={sorted(extra)})"
        )


def normalize_manifest(raw: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schemaVersion", "purpose", "requestId", "issuedAt", "expiresAt",
        "destination", "artifacts",
    }
    _required_keys(raw, expected, "manifest")
    if raw["schemaVersion"] != SCHEMA_VERSION or raw["purpose"] != PURPOSE:
        raise ArtifactAuthorityError("unsupported schema version or purpose")
    destination = raw["destination"]
    if not isinstance(destination, Mapping):
        raise ArtifactAuthorityError("destination must be an object")
    _required_keys(destination, {"systemId", "databaseName"}, "destination")
    artifacts = raw["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise ArtifactAuthorityError("artifacts must be a non-empty array")

    normalized: list[dict[str, Any]] = []
    ordinals: set[int] = set()
    identities: set[tuple[str, str, str, str]] = set()
    required = {
        "ordinal", "artifactSha256", "byteCount", "mediaType", "backend",
        "bucket", "objectKey", "versionId", "etag", "awsChecksumSha256",
        "storageImmutable", "encryptionMode", "encryptionKeyId", "policySourceId",
    }
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise ArtifactAuthorityError("artifact must be an object")
        _required_keys(item, required, "artifact")
        entry = dict(item)
        if entry["backend"] == "aws_s3":
            entry["backend"] = "s3"
        if entry["backend"] != "s3":
            raise ArtifactAuthorityError("artifact backend must be s3")
        ordinal = entry["ordinal"]
        digest = entry["artifactSha256"]
        size = entry["byteCount"]
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
            raise ArtifactAuthorityError("ordinal must be a positive integer")
        if ordinal in ordinals:
            raise ArtifactAuthorityError("duplicate artifact ordinal")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ArtifactAuthorityError("artifactSha256 must be lowercase SHA-256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ArtifactAuthorityError("byteCount must be a non-negative integer")
        for field in ("bucket", "objectKey", "versionId"):
            if not isinstance(entry[field], str) or not entry[field]:
                raise ArtifactAuthorityError(f"{field} must be non-empty")
        if entry["storageImmutable"] is not True:
            raise ArtifactAuthorityError("storageImmutable must be true")
        if entry["encryptionMode"] not in {"provider_managed", "customer_managed"}:
            raise ArtifactAuthorityError("unsupported encryptionMode")
        if not isinstance(entry["encryptionKeyId"], str) or not entry["encryptionKeyId"].strip():
            raise ArtifactAuthorityError("encryptionKeyId must be non-empty")
        identity = (digest, entry["bucket"], entry["objectKey"], entry["versionId"])
        if identity in identities:
            raise ArtifactAuthorityError("duplicate artifact identity")
        ordinals.add(ordinal)
        identities.add(identity)
        normalized.append(entry)
    normalized.sort(key=lambda item: item["ordinal"])
    if [item["ordinal"] for item in normalized] != list(range(1, len(normalized) + 1)):
        raise ArtifactAuthorityError("artifact ordinals must be contiguous starting at one")
    result = dict(raw)
    result["destination"] = dict(destination)
    result["artifacts"] = normalized
    return result


def canonical_manifest_bytes(manifest: Mapping[str, Any]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


@dataclass(frozen=True)
class VerifiedManifest:
    manifest: dict[str, Any]
    manifest_sha256: str
    key_id: str


def verify_manifest_signature(
    manifest: Mapping[str, Any],
    envelope: Mapping[str, Any],
    *,
    keys: Mapping[str, bytes],
    expected_system_id: str,
    expected_database_name: str,
    now: datetime | None = None,
    clock_skew: timedelta = MAX_CLOCK_SKEW,
) -> VerifiedManifest:
    normalized = normalize_manifest(manifest)
    _required_keys(envelope, {"algorithm", "keyId", "manifestSha256", "signature"}, "signature")
    if envelope["algorithm"] != "hmac-sha256":
        raise ArtifactAuthorityError("unsupported signature algorithm")
    key_id = envelope["keyId"]
    if not isinstance(key_id, str) or key_id not in keys:
        raise ArtifactAuthorityError("unknown signing key")
    digest = manifest_sha256(normalized)
    if not hmac.compare_digest(str(envelope["manifestSha256"]), digest):
        raise ArtifactAuthorityError("manifest digest mismatch")
    expected = hmac.new(keys[key_id], canonical_manifest_bytes(normalized), hashlib.sha256).digest()
    try:
        encoded_signature = str(envelope["signature"])
        supplied = base64.b64decode(
            encoded_signature + "=" * (-len(encoded_signature) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, TypeError, base64.binascii.Error) as exc:
        raise ArtifactAuthorityError("invalid signature encoding") from exc
    if not hmac.compare_digest(supplied, expected):
        raise ArtifactAuthorityError("invalid manifest signature")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issued, expires = _utc(normalized["issuedAt"]), _utc(normalized["expiresAt"])
    if expires <= issued or current < issued - clock_skew or current > expires + clock_skew:
        raise ArtifactAuthorityError("manifest is outside its validity window")
    destination = normalized["destination"]
    if not hmac.compare_digest(str(destination["systemId"]), expected_system_id):
        raise ArtifactAuthorityError("destination system mismatch")
    if not hmac.compare_digest(str(destination["databaseName"]), expected_database_name):
        raise ArtifactAuthorityError("destination database mismatch")
    return VerifiedManifest(normalized, digest, key_id)


def _body_digest(body: BinaryIO, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    while True:
        chunk = body.read(chunk_size)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def verify_s3_versions(verified: VerifiedManifest, s3_client: Any) -> VerifiedManifest:
    for item in verified.manifest["artifacts"]:
        args = {"Bucket": item["bucket"], "Key": item["objectKey"], "VersionId": item["versionId"]}
        head = s3_client.head_object(**args)
        if str(head.get("VersionId")) != item["versionId"]:
            raise ArtifactAuthorityError("S3 version mismatch")
        if head.get("ContentLength") != item["byteCount"]:
            raise ArtifactAuthorityError("S3 byte count mismatch")
        if str(head.get("ChecksumSHA256", "")) != item["awsChecksumSha256"]:
            raise ArtifactAuthorityError("S3 provider checksum mismatch")
        if str(head.get("ETag", "")).strip('"') != item["etag"].strip('"'):
            raise ArtifactAuthorityError("S3 ETag mismatch")
        if head.get("ObjectLockMode") != "COMPLIANCE":
            raise ArtifactAuthorityError("S3 object is not under COMPLIANCE Object Lock")
        retain_until = head.get("ObjectLockRetainUntilDate")
        if not isinstance(retain_until, datetime) or retain_until.astimezone(timezone.utc) <= datetime.now(timezone.utc):
            raise ArtifactAuthorityError("S3 Object Lock retention is not active")
        server_encryption = head.get("ServerSideEncryption")
        if item["encryptionMode"] == "customer_managed":
            if server_encryption != "aws:kms" or str(head.get("SSEKMSKeyId", "")) != item["encryptionKeyId"]:
                raise ArtifactAuthorityError("S3 customer-managed encryption mismatch")
        elif server_encryption != "AES256" or item["encryptionKeyId"] != "aws/s3":
            raise ArtifactAuthorityError("S3 provider-managed encryption mismatch")
        response = s3_client.get_object(**args)
        if _body_digest(response["Body"]) != item["artifactSha256"]:
            raise ArtifactAuthorityError("S3 content digest mismatch")
    return verified


def register_verified_manifest(connection: Any, verified: VerifiedManifest) -> dict[str, Any]:
    manifest = verified.manifest
    destination = manifest["destination"]
    sql = """SELECT brain_register_authoritative_artifact_manifest(
        %s::uuid, %s, %s, %s, %s::timestamptz, %s::timestamptz,
        %s, %s, %s::jsonb
    )"""
    database_artifacts = [
        {
            "artifact_hash": item["artifactSha256"],
            "byte_length": item["byteCount"],
            "media_type": item["mediaType"],
            "backend": item["backend"],
            "bucket": item["bucket"],
            "object_key": item["objectKey"],
            "provider_version_id": item["versionId"],
            "provider_checksum": item["awsChecksumSha256"],
            "storage_immutable": item["storageImmutable"],
            "encryption_mode": item["encryptionMode"],
            "encryption_key_id": item["encryptionKeyId"],
            "policy_source_id": item["policySourceId"],
        }
        for item in manifest["artifacts"]
    ]
    params = (
        manifest["requestId"], verified.manifest_sha256, manifest["purpose"],
        verified.key_id, manifest["issuedAt"], manifest["expiresAt"],
        destination["systemId"], destination["databaseName"],
        json.dumps(database_artifacts, sort_keys=True, separators=(",", ":")),
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, params)
            row = cursor.fetchone()
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    if not row:
        raise ArtifactAuthorityError("database returned no authority receipt")
    receipt = row[0]
    if isinstance(receipt, str):
        receipt = json.loads(receipt)
    if not isinstance(receipt, dict):
        raise ArtifactAuthorityError("database returned an invalid authority receipt")
    return receipt


def coordinate_artifact_registration(
    manifest: Mapping[str, Any], envelope: Mapping[str, Any], *, keys: Mapping[str, bytes],
    expected_system_id: str, expected_database_name: str, s3_client: Any,
    connection_factory: Callable[[], Any] | None, dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    verified = verify_manifest_signature(
        manifest, envelope, keys=keys, expected_system_id=expected_system_id,
        expected_database_name=expected_database_name, now=now,
    )
    verify_s3_versions(verified, s3_client)
    if dry_run:
        return {
            "requestId": verified.manifest["requestId"],
            "manifestSha256": verified.manifest_sha256,
            "artifactCount": len(verified.manifest["artifacts"]),
            "dryRun": True,
            "authoritative": False,
            "promotable": False,
        }
    if connection_factory is None:
        raise ArtifactAuthorityError("database connection is required")
    connection = connection_factory()
    try:
        return register_verified_manifest(connection, verified)
    finally:
        connection.close()
