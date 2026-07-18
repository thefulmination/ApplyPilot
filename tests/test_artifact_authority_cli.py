from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from applypilot.brain.artifact_authority import (
    PURPOSE,
    AmbiguousCommitError,
    ArtifactAuthorityError,
    canonical_manifest_bytes,
    coordinate_artifact_registration,
    normalize_manifest,
    parse_json_strict,
    signing_message,
    verify_manifest_signature,
    verify_s3_versions,
)


NOW = datetime(2026, 7, 18, 12, 30, tzinfo=timezone.utc)


def _manifest(*, expires_at: str = "2026-07-18T13:00:00Z") -> dict:
    return {
        "artifacts": [
            {
                "artifactSha256": hashlib.sha256(b"content").hexdigest(),
                "awsChecksumSha256": base64.b64encode(hashlib.sha256(b"content").digest()).decode(),
                "backend": "s3",
                "bucket": "immutable",
                "byteCount": 7,
                "encryptionKeyId": "kms-key-1",
                "encryptionMode": "customer_managed",
                "etag": "etag-1",
                "mediaType": "application/json",
                "objectKey": "sha256/content",
                "ordinal": 1,
                "policySourceId": "policy-1",
                "storageImmutable": True,
                "versionId": "version-1",
            }
        ],
        "destination": {"databaseName": "brain", "systemId": "42"},
        "expiresAt": expires_at,
        "issuedAt": "2026-07-18T12:00:00Z",
        "purpose": PURPOSE,
        "requestId": "11111111-1111-1111-1111-111111111111",
        "schemaVersion": 1,
    }


def _envelope(manifest: dict, key: bytes = b"s" * 32) -> dict:
    normalized = normalize_manifest(manifest)
    canonical = canonical_manifest_bytes(normalized)
    return {
        "algorithm": "hmac-sha256",
        "keyId": "key-1",
        "manifestSha256": hashlib.sha256(canonical).hexdigest(),
        "signature": base64.urlsafe_b64encode(
            hmac.new(key, signing_message(canonical), hashlib.sha256).digest()
        ).rstrip(b"=").decode(),
    }


class _S3:
    def head_object(self, **kwargs):
        assert kwargs == {
            "Bucket": "immutable",
            "ChecksumMode": "ENABLED",
            "Key": "sha256/content",
            "VersionId": "version-1",
        }
        return {
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(b"content").digest()).decode(),
            "ContentLength": 7,
            "ContentType": "application/json",
            "ETag": '"etag-1"',
            "ObjectLockMode": "COMPLIANCE",
            "ObjectLockRetainUntilDate": datetime(2099, 1, 1, tzinfo=timezone.utc),
            "SSEKMSKeyId": "kms-key-1",
            "ServerSideEncryption": "aws:kms",
            "VersionId": "version-1",
        }

    def get_object(self, **kwargs):
        assert kwargs == {
            "Bucket": "immutable",
            "ChecksumMode": "ENABLED",
            "Key": "sha256/content",
            "VersionId": "version-1",
        }
        return {
            "Body": io.BytesIO(b"content"),
            "ChecksumSHA256": base64.b64encode(hashlib.sha256(b"content").digest()).decode(),
            "ContentLength": 7,
            "VersionId": "version-1",
        }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("VersionId", "substituted", "version mismatch"),
        ("ChecksumSHA256", base64.b64encode(b"x" * 32).decode(), "checksum mismatch"),
        ("ContentType", "text/plain", "media type mismatch"),
        ("SSEKMSKeyId", "wrong-key", "encryption mismatch"),
    ),
)
def test_s3_head_substitution_is_rejected(field: str, value: object, message: str) -> None:
    class Substituted(_S3):
        def head_object(self, **kwargs):
            result = super().head_object(**kwargs)
            result[field] = value
            return result

    manifest = _manifest()
    verified = verify_manifest_signature(
        manifest,
        _envelope(manifest),
        keys={"key-1": b"s" * 32},
        expected_system_id="42",
        expected_database_name="brain",
        now=NOW,
    )
    with pytest.raises(ArtifactAuthorityError, match=message):
        verify_s3_versions(verified, Substituted())


def _receipt(manifest: dict) -> dict:
    normalized = normalize_manifest(manifest)
    return {
        "artifact_count": 1,
        "destination_database_name": "brain",
        "destination_system_id": "42",
        "manifest_sha256": hashlib.sha256(canonical_manifest_bytes(normalized)).hexdigest(),
        "request_id": manifest["requestId"],
        "status": "registered",
    }


def test_strict_parser_requires_exact_canonical_json_bytes() -> None:
    manifest = _manifest()
    canonical = canonical_manifest_bytes(manifest)
    assert parse_json_strict(canonical) == manifest
    with pytest.raises(ArtifactAuthorityError, match="canonical JSON"):
        parse_json_strict(b" " + canonical)
    with pytest.raises(ArtifactAuthorityError, match="canonical JSON"):
        parse_json_strict(json.dumps(manifest).encode())


def test_hmac_is_domain_separated_by_purpose() -> None:
    manifest = _manifest()
    canonical = canonical_manifest_bytes(normalize_manifest(manifest))
    legacy = _envelope(manifest)
    legacy["signature"] = base64.urlsafe_b64encode(
        hmac.new(b"s" * 32, canonical, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    with pytest.raises(ArtifactAuthorityError, match="signature"):
        verify_manifest_signature(
            manifest,
            legacy,
            keys={"key-1": b"s" * 32},
            expected_system_id="42",
            expected_database_name="brain",
            now=NOW,
        )


class _Cursor:
    def __init__(self, receipt: dict) -> None:
        self.receipt = receipt
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _sql, params) -> None:
        self.params = params

    def fetchone(self):
        return (self.receipt,)


class _Connection:
    def __init__(self, receipt: dict, *, ambiguous_commit: bool = False) -> None:
        self.cursor_value = _Cursor(receipt)
        self.ambiguous_commit = ambiguous_commit
        self.closed = False
        self.rolled_back = False

    def cursor(self):
        return self.cursor_value

    def commit(self) -> None:
        if self.ambiguous_commit:
            raise OSError("connection lost after commit")

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def test_ambiguous_commit_recovers_only_by_exact_request_replay() -> None:
    manifest = _manifest()
    expected = _receipt(manifest)
    first = _Connection(expected, ambiguous_commit=True)
    recovered = _Connection(expected)
    connections = iter((first, recovered))
    result = coordinate_artifact_registration(
        manifest,
        _envelope(manifest),
        keys={"key-1": b"s" * 32},
        expected_system_id="42",
        expected_database_name="brain",
        s3_client=_S3(),
        connection_factory=lambda: next(connections),
        now=NOW,
    )
    assert result == expected
    assert first.cursor_value.params == recovered.cursor_value.params
    assert first.closed and recovered.closed


def test_ambiguous_recovery_rejects_mismatched_receipt() -> None:
    manifest = _manifest()
    bad = dict(_receipt(manifest), artifact_count=2)
    connections = iter((_Connection(_receipt(manifest), ambiguous_commit=True), _Connection(bad)))
    with pytest.raises(ArtifactAuthorityError, match="artifact count"):
        coordinate_artifact_registration(
            manifest,
            _envelope(manifest),
            keys={"key-1": b"s" * 32},
            expected_system_id="42",
            expected_database_name="brain",
            s3_client=_S3(),
            connection_factory=lambda: next(connections),
            now=NOW,
        )


def test_expired_manifest_can_be_replayed_after_an_ambiguous_commit() -> None:
    manifest = _manifest(expires_at="2026-07-18T12:31:00Z")
    expected = _receipt(manifest)
    connections = iter((_Connection(expected, ambiguous_commit=True), _Connection(expected)))
    assert coordinate_artifact_registration(
        manifest,
        _envelope(manifest),
        keys={"key-1": b"s" * 32},
        expected_system_id="42",
            expected_database_name="brain",
            s3_client=_S3(),
            connection_factory=lambda: next(connections),
            now=NOW,
        ) == expected


def test_expired_crash_recovery_requires_explicit_exact_replay_mode() -> None:
    manifest = _manifest(expires_at="2026-07-18T12:31:00Z")
    late = datetime(2026, 7, 18, 14, 0, tzinfo=timezone.utc)
    kwargs = {
        "keys": {"key-1": b"s" * 32},
        "expected_system_id": "42",
        "expected_database_name": "brain",
        "s3_client": _S3(),
        "connection_factory": lambda: _Connection(_receipt(manifest)),
        "now": late,
    }
    with pytest.raises(ArtifactAuthorityError, match="validity window"):
        coordinate_artifact_registration(manifest, _envelope(manifest), **kwargs)
    assert coordinate_artifact_registration(
        manifest, _envelope(manifest), allow_expired_replay=True, **kwargs
    ) == _receipt(manifest)


def _load_cli():
    path = Path(__file__).parents[1] / "scripts" / "register-brain-artifact-authority.py"
    spec = importlib.util.spec_from_file_location("artifact_authority_cli", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_receipt_publication_is_create_only_and_rejects_symlink(tmp_path: Path) -> None:
    cli = _load_cli()
    receipt_path = tmp_path / "receipt.json"
    cli.publish_receipt_create_only(receipt_path, {"status": "registered"})
    original = receipt_path.read_bytes()
    with pytest.raises(ArtifactAuthorityError, match="already exists"):
        cli.publish_receipt_create_only(receipt_path, {"status": "different"})
    assert receipt_path.read_bytes() == original

    target = tmp_path / "target.json"
    target.write_bytes(b"do-not-touch")
    link = tmp_path / "linked-receipt.json"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlink creation is not available")
    with pytest.raises(ArtifactAuthorityError, match="already exists|symbolic link"):
        cli.publish_receipt_create_only(link, {"status": "registered"})
    assert target.read_bytes() == b"do-not-touch"


def test_secure_input_rejects_symlink(tmp_path: Path) -> None:
    cli = _load_cli()
    target = tmp_path / "manifest.json"
    target.write_bytes(b"{}")
    link = tmp_path / "manifest-link.json"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlink creation is not available")
    with pytest.raises(ArtifactAuthorityError, match="symbolic link"):
        cli.read_secure_regular_file(link, max_bytes=1024)


def test_existing_receipt_refuses_before_any_input_or_external_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()
    receipt = tmp_path / "receipt.json"
    receipt.write_bytes(b"existing")
    monkeypatch.setattr(
        cli,
        "read_secure_regular_file",
        lambda *_args, **_kwargs: pytest.fail("existing receipt must refuse before reading inputs"),
    )
    with pytest.raises(ArtifactAuthorityError, match="already exists"):
        cli.run(SimpleNamespace(receipt=receipt))


def test_dry_run_ignores_hostile_database_environment_and_opens_no_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()
    manifest = _manifest()
    files = {
        "manifest": canonical_manifest_bytes(manifest),
        "signature": canonical_manifest_bytes(_envelope(manifest)),
        "hmac-key": b"s" * 32,
        "aws": canonical_manifest_bytes(
            {"accessKeyId": "access", "secretAccessKey": "secret", "sessionToken": None}
        ),
    }
    paths = {}
    for name, payload in files.items():
        paths[name] = tmp_path / name
        paths[name].write_bytes(payload)
    monkeypatch.setenv("DATABASE_URL", "postgresql://hostile.invalid/production")
    monkeypatch.setenv("PGSERVICE", "hostile-service")
    monkeypatch.setattr(cli, "_aws_client", lambda *_args: object())
    captured = {}

    def coordinate(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "artifact_count": 1,
            "authoritative": False,
            "dry_run": True,
            "manifest_sha256": "a" * 64,
            "promotable": False,
            "request_id": manifest["requestId"],
        }

    monkeypatch.setattr(cli, "coordinate_artifact_registration", coordinate)
    receipt = tmp_path / "receipt.json"
    result = cli.run(
        SimpleNamespace(
            receipt=receipt,
            dry_run=True,
            recover_committed=False,
            database_dsn_file=None,
            manifest=paths["manifest"],
            signature=paths["signature"],
            key_id="key-1",
            hmac_key_file=paths["hmac-key"],
            aws_credentials=paths["aws"],
            aws_region="us-east-1",
            expected_system_id="42",
            expected_database_name="brain",
        )
    )
    assert captured["connection_factory"] is None
    assert result["dry_run"] is True and result["promotable"] is False
    assert parse_json_strict(receipt.read_bytes()) == result


def test_receipt_link_failure_leaves_neither_partial_receipt_nor_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_cli()
    destination = tmp_path / "receipt.json"
    monkeypatch.setattr(cli.os, "link", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(ArtifactAuthorityError, match="atomic create-only"):
        cli.publish_receipt_create_only(destination, {"status": "registered"})
    assert not destination.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_ambiguous_commit_marker_is_specific() -> None:
    assert issubclass(AmbiguousCommitError, ArtifactAuthorityError)
