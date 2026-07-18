"""Authenticated coordination for immutable brain-artifact authority manifests.

AWS verification deliberately completes before a database connection is opened. The database
function is the final authority boundary and owns exact-request replay semantics.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO, Callable, Mapping

import rfc8785


PURPOSE = "brain-artifact-authority-registration-v1"
SCHEMA_VERSION = 1
MAX_CLOCK_SKEW = timedelta(minutes=5)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ArtifactAuthorityError(ValueError):
    """A manifest cannot be trusted or registered."""


class AmbiguousCommitError(ArtifactAuthorityError):
    """The database connection failed while commit disposition was unknowable."""


def _object_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArtifactAuthorityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_json_constant(value: str) -> None:
    raise ArtifactAuthorityError(f"invalid JSON number: {value}")


def parse_json_strict(value: str | bytes, *, require_canonical: bool = True) -> dict[str, Any]:
    """Parse an object while rejecting duplicates and non-canonical byte encodings."""
    try:
        raw = value.encode("utf-8") if isinstance(value, str) else bytes(value)
        parsed = json.loads(
            raw,
            object_pairs_hook=_object_no_duplicates,
            parse_constant=_reject_non_json_constant,
        )
    except ArtifactAuthorityError:
        raise
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactAuthorityError("invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise ArtifactAuthorityError("JSON root must be an object")
    if require_canonical and raw != canonical_manifest_bytes(parsed):
        raise ArtifactAuthorityError("input must be exact canonical JSON")
    return parsed


def _utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ArtifactAuthorityError("timestamp must be RFC3339 text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
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


def _nonempty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ArtifactAuthorityError(f"{field_name} must be non-empty text")
    return value


def _provider_checksum(value: object) -> str:
    checksum = _nonempty_text(value, "awsChecksumSha256")
    try:
        decoded = base64.b64decode(checksum, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise ArtifactAuthorityError("awsChecksumSha256 must be canonical base64 SHA-256") from exc
    if len(decoded) != hashlib.sha256().digest_size or base64.b64encode(decoded).decode() != checksum:
        raise ArtifactAuthorityError("awsChecksumSha256 must be canonical base64 SHA-256")
    return checksum


def normalize_manifest(raw: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schemaVersion",
        "purpose",
        "requestId",
        "issuedAt",
        "expiresAt",
        "destination",
        "artifacts",
    }
    _required_keys(raw, expected, "manifest")
    if raw["schemaVersion"] != SCHEMA_VERSION or raw["purpose"] != PURPOSE:
        raise ArtifactAuthorityError("unsupported schema version or purpose")
    request_id = _nonempty_text(raw["requestId"], "requestId")
    try:
        if str(uuid.UUID(request_id)) != request_id:
            raise ValueError
    except ValueError as exc:
        raise ArtifactAuthorityError("requestId must be a canonical UUID") from exc
    _utc(raw["issuedAt"])
    _utc(raw["expiresAt"])

    destination = raw["destination"]
    if not isinstance(destination, Mapping):
        raise ArtifactAuthorityError("destination must be an object")
    _required_keys(destination, {"systemId", "databaseName"}, "destination")
    _nonempty_text(destination["systemId"], "systemId")
    _nonempty_text(destination["databaseName"], "databaseName")

    artifacts = raw["artifacts"]
    if not isinstance(artifacts, list) or not artifacts:
        raise ArtifactAuthorityError("artifacts must be a non-empty array")

    normalized: list[dict[str, Any]] = []
    ordinals: set[int] = set()
    identities: set[tuple[str, str, str, str]] = set()
    required = {
        "ordinal",
        "artifactSha256",
        "byteCount",
        "mediaType",
        "backend",
        "bucket",
        "objectKey",
        "versionId",
        "etag",
        "awsChecksumSha256",
        "storageImmutable",
        "encryptionMode",
        "encryptionKeyId",
        "policySourceId",
    }
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise ArtifactAuthorityError("artifact must be an object")
        _required_keys(item, required, "artifact")
        entry = dict(item)
        backend = entry["backend"]
        if backend not in {"s3", "aws_s3"}:
            raise ArtifactAuthorityError("artifact backend must be s3 or aws_s3")
        entry["backend"] = "s3"
        ordinal = entry["ordinal"]
        digest = entry["artifactSha256"]
        size = entry["byteCount"]
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
            raise ArtifactAuthorityError("ordinal must be a positive integer")
        if ordinal in ordinals:
            raise ArtifactAuthorityError("duplicate artifact ordinal")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ArtifactAuthorityError("artifactSha256 must be lowercase SHA-256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ArtifactAuthorityError("byteCount must be a non-negative integer")
        for name in ("mediaType", "bucket", "objectKey", "versionId", "etag"):
            _nonempty_text(entry[name], name)
        _provider_checksum(entry["awsChecksumSha256"])
        if entry["storageImmutable"] is not True:
            raise ArtifactAuthorityError("storageImmutable must be true")
        if entry["encryptionMode"] not in {"provider_managed", "customer_managed"}:
            raise ArtifactAuthorityError("unsupported encryptionMode")
        _nonempty_text(entry["encryptionKeyId"], "encryptionKeyId")
        if entry["policySourceId"] is not None:
            _nonempty_text(entry["policySourceId"], "policySourceId")
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
    try:
        return rfc8785.dumps(manifest)
    except (rfc8785.CanonicalizationError, TypeError) as exc:
        raise ArtifactAuthorityError("value cannot be represented as canonical JSON") from exc


def manifest_sha256(manifest: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_manifest_bytes(manifest)).hexdigest()


def signing_message(canonical_bytes: bytes) -> bytes:
    """Domain-separate authority signatures from every other use of the HMAC key."""
    return PURPOSE.encode("ascii") + b"\x00" + canonical_bytes


@dataclass(frozen=True)
class VerifiedManifest:
    manifest: dict[str, Any]
    manifest_sha256: str
    key_id: str
    verified_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def verify_manifest_signature(
    manifest: Mapping[str, Any],
    envelope: Mapping[str, Any],
    *,
    keys: Mapping[str, bytes],
    expected_system_id: str,
    expected_database_name: str,
    now: datetime | None = None,
    clock_skew: timedelta = MAX_CLOCK_SKEW,
    allow_expired_replay: bool = False,
) -> VerifiedManifest:
    normalized = normalize_manifest(manifest)
    _required_keys(envelope, {"algorithm", "keyId", "manifestSha256", "signature"}, "signature")
    if envelope["algorithm"] != "hmac-sha256":
        raise ArtifactAuthorityError("unsupported signature algorithm")
    key_id = envelope["keyId"]
    if not isinstance(key_id, str) or key_id not in keys:
        raise ArtifactAuthorityError("unknown signing key")
    key = keys[key_id]
    if not isinstance(key, bytes) or len(key) < 32:
        raise ArtifactAuthorityError("HMAC key must contain at least 32 bytes")
    canonical = canonical_manifest_bytes(normalized)
    digest = hashlib.sha256(canonical).hexdigest()
    supplied_digest = envelope["manifestSha256"]
    if not isinstance(supplied_digest, str) or not hmac.compare_digest(supplied_digest, digest):
        raise ArtifactAuthorityError("manifest digest mismatch")
    expected = hmac.new(key, signing_message(canonical), hashlib.sha256).digest()
    signature = envelope["signature"]
    if not isinstance(signature, str):
        raise ArtifactAuthorityError("invalid signature encoding")
    try:
        supplied = base64.b64decode(
            signature + "=" * (-len(signature) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, base64.binascii.Error) as exc:
        raise ArtifactAuthorityError("invalid signature encoding") from exc
    canonical_signature = base64.urlsafe_b64encode(supplied).rstrip(b"=").decode()
    if len(supplied) != hashlib.sha256().digest_size or canonical_signature != signature:
        raise ArtifactAuthorityError("invalid signature encoding")
    if not hmac.compare_digest(supplied, expected):
        raise ArtifactAuthorityError("invalid manifest signature")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issued, expires = _utc(normalized["issuedAt"]), _utc(normalized["expiresAt"])
    if expires <= issued or current < issued - clock_skew or (current >= expires and not allow_expired_replay):
        raise ArtifactAuthorityError("manifest is outside its validity window")
    destination = normalized["destination"]
    system_id = _nonempty_text(expected_system_id, "expected_system_id")
    database_name = _nonempty_text(expected_database_name, "expected_database_name")
    if not hmac.compare_digest(destination["systemId"], system_id):
        raise ArtifactAuthorityError("destination system mismatch")
    if not hmac.compare_digest(destination["databaseName"], database_name):
        raise ArtifactAuthorityError("destination database mismatch")
    return VerifiedManifest(normalized, digest, key_id, current)


def _body_digests(body: BinaryIO, chunk_size: int = 1024 * 1024) -> tuple[str, str, int]:
    digest = hashlib.sha256()
    byte_count = 0
    while True:
        chunk = body.read(chunk_size)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise ArtifactAuthorityError("S3 body did not return bytes")
        byte_count += len(chunk)
        digest.update(chunk)
    return digest.hexdigest(), base64.b64encode(digest.digest()).decode(), byte_count


def verify_s3_versions(verified: VerifiedManifest, s3_client: Any) -> VerifiedManifest:
    for item in verified.manifest["artifacts"]:
        args = {"Bucket": item["bucket"], "Key": item["objectKey"], "VersionId": item["versionId"]}
        head = s3_client.head_object(**args, ChecksumMode="ENABLED")
        if head.get("DeleteMarker") is True or str(head.get("VersionId", "")) != item["versionId"]:
            raise ArtifactAuthorityError("S3 version mismatch")
        if head.get("ContentLength") != item["byteCount"]:
            raise ArtifactAuthorityError("S3 byte count mismatch")
        if head.get("ContentType") != item["mediaType"]:
            raise ArtifactAuthorityError("S3 media type mismatch")
        if head.get("ChecksumSHA256") != item["awsChecksumSha256"]:
            raise ArtifactAuthorityError("S3 provider checksum mismatch")
        if str(head.get("ETag", "")).strip('"') != item["etag"]:
            raise ArtifactAuthorityError("S3 ETag mismatch")
        if head.get("ObjectLockMode") != "COMPLIANCE":
            raise ArtifactAuthorityError("S3 object is not under COMPLIANCE Object Lock")
        retain_until = head.get("ObjectLockRetainUntilDate")
        if (
            not isinstance(retain_until, datetime)
            or retain_until.tzinfo is None
            or retain_until.astimezone(timezone.utc) <= verified.verified_at
        ):
            raise ArtifactAuthorityError("S3 Object Lock retention is not active")
        server_encryption = head.get("ServerSideEncryption")
        if item["encryptionMode"] == "customer_managed":
            if server_encryption != "aws:kms" or head.get("SSEKMSKeyId") != item["encryptionKeyId"]:
                raise ArtifactAuthorityError("S3 customer-managed encryption mismatch")
        elif server_encryption != "AES256" or item["encryptionKeyId"] != "aws/s3":
            raise ArtifactAuthorityError("S3 provider-managed encryption mismatch")

        response = s3_client.get_object(**args, ChecksumMode="ENABLED")
        if str(response.get("VersionId", "")) != item["versionId"]:
            raise ArtifactAuthorityError("S3 downloaded version mismatch")
        if response.get("ChecksumSHA256") != item["awsChecksumSha256"]:
            raise ArtifactAuthorityError("S3 downloaded provider checksum mismatch")
        body = response.get("Body")
        if body is None or not hasattr(body, "read"):
            raise ArtifactAuthorityError("S3 response has no readable body")
        content_digest, provider_digest, byte_count = _body_digests(body)
        if content_digest != item["artifactSha256"]:
            raise ArtifactAuthorityError("S3 content digest mismatch")
        if provider_digest != item["awsChecksumSha256"]:
            raise ArtifactAuthorityError("S3 checksum does not match downloaded content")
        if byte_count != item["byteCount"] or response.get("ContentLength", byte_count) != byte_count:
            raise ArtifactAuthorityError("S3 downloaded byte count mismatch")
    return verified


def _database_artifacts(verified: VerifiedManifest) -> list[dict[str, Any]]:
    return [
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
        for item in verified.manifest["artifacts"]
    ]


def _registration_params(verified: VerifiedManifest) -> tuple[Any, ...]:
    manifest = verified.manifest
    destination = manifest["destination"]
    return (
        manifest["requestId"],
        verified.manifest_sha256,
        manifest["purpose"],
        verified.key_id,
        manifest["issuedAt"],
        manifest["expiresAt"],
        destination["systemId"],
        destination["databaseName"],
        json.dumps(_database_artifacts(verified), sort_keys=True, separators=(",", ":")),
    )


def validate_registration_receipt(receipt: object, verified: VerifiedManifest) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise ArtifactAuthorityError("database returned an invalid authority receipt")
    manifest = verified.manifest
    expected = {
        "request_id": manifest["requestId"],
        "manifest_sha256": verified.manifest_sha256,
        "destination_system_id": manifest["destination"]["systemId"],
        "destination_database_name": manifest["destination"]["databaseName"],
        "artifact_count": len(manifest["artifacts"]),
    }
    labels = {
        "request_id": "request ID",
        "manifest_sha256": "manifest digest",
        "destination_system_id": "destination system",
        "destination_database_name": "destination database",
        "artifact_count": "artifact count",
    }
    if receipt.get("status") != "registered":
        raise ArtifactAuthorityError("database receipt is not registered")
    for key, value in expected.items():
        actual = receipt.get(key)
        if key == "request_id":
            actual = str(actual) if actual is not None else None
        if actual != value:
            raise ArtifactAuthorityError(f"database receipt {labels[key]} mismatch")
    return receipt


def register_verified_manifest(connection: Any, verified: VerifiedManifest) -> dict[str, Any]:
    """Execute one request. A commit exception is deliberately classified as ambiguous."""
    sql = """SELECT brain_register_authoritative_artifact_manifest(
        %s::uuid, %s, %s, %s, %s::timestamptz, %s::timestamptz,
        %s, %s, %s::jsonb
    )"""
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, _registration_params(verified))
            row = cursor.fetchone()
        if not row:
            raise ArtifactAuthorityError("database returned no authority receipt")
        receipt = row[0]
        if isinstance(receipt, str):
            receipt = parse_json_strict(receipt, require_canonical=False)
        validated = validate_registration_receipt(receipt, verified)
    except Exception:
        try:
            connection.rollback()
        except Exception:
            pass
        raise
    try:
        connection.commit()
    except Exception as exc:
        raise AmbiguousCommitError("database commit disposition is ambiguous") from exc
    return validated


def _register_once(connection_factory: Callable[[], Any], verified: VerifiedManifest) -> dict[str, Any]:
    connection = connection_factory()
    try:
        return register_verified_manifest(connection, verified)
    finally:
        try:
            connection.close()
        except Exception:
            pass


def coordinate_artifact_registration(
    manifest: Mapping[str, Any],
    envelope: Mapping[str, Any],
    *,
    keys: Mapping[str, bytes],
    expected_system_id: str,
    expected_database_name: str,
    s3_client: Any,
    connection_factory: Callable[[], Any] | None,
    dry_run: bool = False,
    now: datetime | None = None,
    allow_expired_replay: bool = False,
) -> dict[str, Any]:
    if dry_run and allow_expired_replay:
        raise ArtifactAuthorityError("dry-run cannot perform expired replay recovery")
    verified = verify_manifest_signature(
        manifest,
        envelope,
        keys=keys,
        expected_system_id=expected_system_id,
        expected_database_name=expected_database_name,
        now=now,
        allow_expired_replay=allow_expired_replay,
    )
    verify_s3_versions(verified, s3_client)
    if dry_run:
        return {
            "artifact_count": len(verified.manifest["artifacts"]),
            "authoritative": False,
            "dry_run": True,
            "manifest_sha256": verified.manifest_sha256,
            "promotable": False,
            "request_id": verified.manifest["requestId"],
        }
    if connection_factory is None:
        raise ArtifactAuthorityError("database connection is required")
    try:
        return _register_once(connection_factory, verified)
    except AmbiguousCommitError:
        # The only safe recovery operation is the exact same database request. The v6 database
        # API must return the original receipt before applying its normal expiry check on replay.
        return _register_once(connection_factory, verified)
