from __future__ import annotations

import json
import base64
from datetime import datetime, timezone
import hashlib
import hmac
import io

import pytest

from applypilot.brain.artifact_authority import (
    ArtifactAuthorityError,
    VerifiedManifest,
    canonical_manifest_bytes,
    coordinate_artifact_registration,
    normalize_manifest,
    parse_json_strict,
    register_verified_manifest,
    signing_message,
    verify_manifest_signature,
    verify_s3_versions,
)


def _manifest() -> dict:
    return {
        "schemaVersion": 1,
        "purpose": "brain-artifact-authority-registration-v1",
        "requestId": "11111111-1111-1111-1111-111111111111",
        "issuedAt": "2026-07-18T12:00:00Z",
        "expiresAt": "2026-07-18T13:00:00Z",
        "destination": {"systemId": "42", "databaseName": "brain"},
        "artifacts": [
            {
                "ordinal": 1,
                "artifactSha256": "a" * 64,
                "byteCount": 7,
                "mediaType": "application/json",
                "backend": "aws_s3",
                "bucket": "immutable",
                "objectKey": "sha256/aa",
                "versionId": "version-1",
                "etag": "etag-1",
                "awsChecksumSha256": base64.b64encode(hashlib.sha256(b"content").digest()).decode(),
                "storageImmutable": True,
                "encryptionMode": "customer_managed",
                "encryptionKeyId": "kms-key-1",
                "policySourceId": "policy-1",
            }
        ],
    }


class _Cursor:
    def __init__(self, receipt=None) -> None:
        self.params = None
        self.receipt = receipt or {
            "status": "registered",
            "request_id": "11111111-1111-1111-1111-111111111111",
            "manifest_sha256": "b" * 64,
            "destination_system_id": "42",
            "destination_database_name": "brain",
            "artifact_count": 1,
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _sql, params) -> None:
        self.params = params

    def fetchone(self):
        return (self.receipt,)


class _Connection:
    def __init__(self) -> None:
        self.cursor_value = _Cursor()
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.cursor_value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def test_registration_translates_signed_manifest_to_fixed_database_contract() -> None:
    manifest = normalize_manifest(_manifest())
    connection = _Connection()
    receipt = register_verified_manifest(connection, VerifiedManifest(manifest, "b" * 64, "key-1"))
    assert receipt["status"] == "registered"
    assert connection.committed is True
    payload = json.loads(connection.cursor_value.params[-1])
    assert payload == [
        {
            "artifact_hash": "a" * 64,
            "backend": "s3",
            "bucket": "immutable",
            "byte_length": 7,
            "encryption_key_id": "kms-key-1",
            "encryption_mode": "customer_managed",
            "media_type": "application/json",
            "object_key": "sha256/aa",
            "policy_source_id": "policy-1",
            "provider_checksum": base64.b64encode(hashlib.sha256(b"content").digest()).decode(),
            "provider_version_id": "version-1",
            "storage_immutable": True,
        }
    ]


@pytest.mark.parametrize("ordinals", ([0], [2], [1, 3]))
def test_manifest_rejects_non_positive_or_gapped_ordinals(ordinals: list[int]) -> None:
    manifest = _manifest()
    manifest["artifacts"] = [dict(manifest["artifacts"][0], ordinal=value, artifactSha256=f"{value + 1:064x}") for value in ordinals]
    with pytest.raises(ArtifactAuthorityError, match="ordinal"):
        normalize_manifest(manifest)


def test_strict_json_rejects_duplicate_keys() -> None:
    with pytest.raises(ArtifactAuthorityError, match="duplicate JSON key"):
        parse_json_strict('{"schemaVersion":1,"schemaVersion":1}')


def test_signature_binds_canonical_manifest_time_and_destination() -> None:
    manifest = _manifest()
    canonical = canonical_manifest_bytes(manifest)
    digest = hashlib.sha256(canonical).hexdigest()
    key = b"s" * 32
    signature = base64.urlsafe_b64encode(
        hmac.new(key, signing_message(canonical), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    verified = verify_manifest_signature(
        manifest,
        {"algorithm": "hmac-sha256", "keyId": "key-1", "manifestSha256": digest, "signature": signature},
        keys={"key-1": key},
        expected_system_id="42",
        expected_database_name="brain",
        now=datetime(2026, 7, 18, 12, 30, tzinfo=timezone.utc),
    )
    assert verified.manifest_sha256 == digest
    with pytest.raises(ArtifactAuthorityError, match="destination system mismatch"):
        verify_manifest_signature(
            manifest,
            {"algorithm": "hmac-sha256", "keyId": "key-1", "manifestSha256": digest, "signature": signature},
            keys={"key-1": key},
            expected_system_id="wrong",
            expected_database_name="brain",
            now=datetime(2026, 7, 18, 12, 30, tzinfo=timezone.utc),
        )


class _S3:
    def head_object(self, **_kwargs):
        return {
            "VersionId": "version-1",
            "ContentLength": 7,
            "ContentType": "application/json",
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(b"content").digest()).decode(),
            "ETag": '"etag-1"',
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": datetime(2099, 1, 1, tzinfo=timezone.utc),
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": "kms-key-1",
        }

    def get_object(self, **_kwargs):
        return {
            "Body": io.BytesIO(b"content"),
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(b"content").digest()).decode(),
            "ContentLength": 7,
            "VersionId": "version-1",
        }


def test_s3_verification_binds_exact_version_content_lock_and_encryption() -> None:
    manifest = normalize_manifest(_manifest())
    manifest["artifacts"][0]["artifactSha256"] = hashlib.sha256(b"content").hexdigest()
    verified = VerifiedManifest(manifest, "b" * 64, "key-1")
    assert verify_s3_versions(verified, _S3()) is verified


def test_s3_verification_rejects_missing_compliance_lock() -> None:
    class Unlocked(_S3):
        def head_object(self, **kwargs):
            result = super().head_object(**kwargs)
            result["ObjectLockMode"] = "GOVERNANCE"
            return result

    manifest = normalize_manifest(_manifest())
    manifest["artifacts"][0]["artifactSha256"] = hashlib.sha256(b"content").hexdigest()
    with pytest.raises(ArtifactAuthorityError, match="COMPLIANCE"):
        verify_s3_versions(VerifiedManifest(manifest, "b" * 64, "key-1"), Unlocked())


def test_invalid_signature_encoding_is_rejected_strictly() -> None:
    manifest = _manifest()
    canonical = canonical_manifest_bytes(manifest)
    with pytest.raises(ArtifactAuthorityError, match="signature encoding"):
        verify_manifest_signature(
            manifest,
            {
                "algorithm": "hmac-sha256",
                "keyId": "key-1",
                "manifestSha256": hashlib.sha256(canonical).hexdigest(),
                "signature": "not+url/safe!",
            },
            keys={"key-1": b"s" * 32},
            expected_system_id="42",
            expected_database_name="brain",
            now=datetime(2026, 7, 18, 12, 30, tzinfo=timezone.utc),
        )


def test_dry_run_verifies_s3_without_opening_database() -> None:
    manifest = _manifest()
    manifest["artifacts"][0]["artifactSha256"] = hashlib.sha256(b"content").hexdigest()
    canonical = canonical_manifest_bytes(manifest)
    key = b"s" * 32
    envelope = {
        "algorithm": "hmac-sha256",
        "keyId": "key-1",
        "manifestSha256": hashlib.sha256(canonical).hexdigest(),
        "signature": base64.urlsafe_b64encode(
            hmac.new(key, signing_message(canonical), hashlib.sha256).digest()
        ).rstrip(b"=").decode(),
    }
    result = coordinate_artifact_registration(
        manifest,
        envelope,
        keys={"key-1": key},
        expected_system_id="42",
        expected_database_name="brain",
        s3_client=_S3(),
        connection_factory=lambda: pytest.fail("dry-run opened a database connection"),
        dry_run=True,
        now=datetime(2026, 7, 18, 12, 30, tzinfo=timezone.utc),
    )
    assert result["dry_run"] is True
    assert result["authoritative"] is False
    assert result["promotable"] is False
