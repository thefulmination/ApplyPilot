from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import multiprocessing
import os
import secrets
import shutil
import stat
import subprocess
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

import applypilot.brain.artifacts as artifacts_module
from applypilot.brain.artifacts import (
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactLockTimeout,
    ArtifactState,
    ArtifactWriteRequest,
    LocalArtifactStore,
)


def _request(request_id: str = "opaque-request-1") -> ArtifactWriteRequest:
    return ArtifactWriteRequest(
        request_id=request_id,
        media_type="application/octet-stream",
        schema_version=1,
    )


class BoundedReader(io.BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        assert 0 < size <= LocalArtifactStore.CHUNK_SIZE
        self.read_sizes.append(size)
        return super().read(size)


def _process_publish(root: str, start, output) -> None:
    start.wait()
    try:
        store = LocalArtifactStore(root)
        pending = store.stage(io.BytesIO(b"cross-process"), _request("cross-process-request"))
        receipt = store.verify_pending(pending)
        ref = store.commit(pending, receipt)
        output.put(("ok", pending.generation_id, ref.object_key, ref.content_sha256))
    except Exception as exc:  # pragma: no cover - surfaced through the parent
        output.put(("error", type(exc).__name__, str(exc)))


def _process_publish_distinct_request(root: str, request_id: str, start, output) -> None:
    start.wait()
    try:
        store = LocalArtifactStore(root)
        pending = store.stage(io.BytesIO(b"shared-cross-process"), _request(request_id))
        ref = store.commit(pending, store.verify_pending(pending))
        output.put(("ok", ref.request_id, ref.generation_id, ref.object_key))
    except Exception as exc:  # pragma: no cover - surfaced through the parent
        output.put(("error", type(exc).__name__, str(exc)))


def _process_initialize_with_anchor(root: str, anchor: str, start, output) -> None:
    start.wait()
    try:
        store = LocalArtifactStore(root, anchor_root=anchor)
        output.put(("ok", anchor, store._load_coordinator_key().key_id))
    except Exception as exc:  # pragma: no cover - surfaced through the parent
        output.put(("error", anchor, type(exc).__name__, str(exc)))


def _process_stage_exit(root: str, kill_point: str) -> None:
    def exit_at(point: str) -> None:
        if point == kill_point:
            os._exit(71)

    store = LocalArtifactStore(root, fault_injector=exit_at)
    store.stage(io.BytesIO(b"abrupt-stage"), _request("abrupt-stage"))


def _process_commit_exit(root: str, pending, receipt, kill_point: str) -> None:
    def exit_at(point: str) -> None:
        if point == kill_point:
            os._exit(72)

    LocalArtifactStore(root, fault_injector=exit_at).commit(pending, receipt)


def _process_commit_pause(root: str, pending, receipt, entered, release) -> None:
    def pause_at(point: str) -> None:
        if point == "after_commit_claim":
            entered.set()
            if not release.wait(20):
                os._exit(79)

    LocalArtifactStore(root, fault_injector=pause_at).commit(pending, receipt)


class _TerminatedReader:
    def __init__(self, entered, release) -> None:
        self._entered = entered
        self._release = release
        self._sent = False

    def read(self, _size=-1):
        if not self._sent:
            self._sent = True
            self._entered.set()
            return b"x" * LocalArtifactStore.CHUNK_SIZE
        self._release.wait(30)
        return b""


def _process_blocked_stage(root: str, entered, release) -> None:
    LocalArtifactStore(root).stage(
        _TerminatedReader(entered, release),
        _request("terminated-stage"),
    )


def _rewrite_canonical(path: Path, mutate) -> None:
    value = json.loads(path.read_bytes())
    mutate(value)
    path.write_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"
    )


def _committed_fixture(root: Path, request_id: str = "authenticated-chain"):
    store = LocalArtifactStore(root)
    pending = store.stage(io.BytesIO(b"authenticated"), _request(request_id))
    receipt = store.verify_pending(pending)
    ref = store.commit(pending, receipt)
    return store, pending, receipt, ref


@pytest.mark.parametrize("payload", [b"", b"large-stream" * 200_000], ids=["empty", "large"])
def test_stage_streams_empty_and_large_inputs_in_bounded_chunks(tmp_path, payload):
    store = LocalArtifactStore(tmp_path)
    stream = BoundedReader(payload)

    pending = store.stage(stream, _request())
    receipt = store.verify_pending(pending)
    ref = store.commit(pending, receipt)

    assert ref.content_sha256 == hashlib.sha256(payload).hexdigest()
    assert ref.byte_count == len(payload)
    assert stream.read_sizes
    with store.open_verified(ref) as accepted:
        assert accepted.read() == payload


def test_equal_content_converges_to_one_content_addressed_blob(tmp_path):
    store = LocalArtifactStore(tmp_path)
    payload = b"same content"
    first = store.stage(io.BytesIO(payload), _request("opaque-a"))
    second = store.stage(io.BytesIO(payload), _request("opaque-b"))

    expected_hash = hashlib.sha256(payload).hexdigest()
    assert first.content_sha256 == second.content_sha256 == expected_hash
    assert first.object_key == second.object_key == expected_hash
    assert first.generation_id != second.generation_id

    first_ref = store.commit(first, store.verify_pending(first))
    second_ref = store.commit(second, store.verify_pending(second))
    assert first_ref.generation_id != second_ref.generation_id
    assert first_ref.request_id != second_ref.request_id
    assert first_ref.object_key == second_ref.object_key == expected_hash
    assert len(list((store.root / "committed").glob("*.blob"))) == 1


def test_commit_never_overwrites_an_existing_object(tmp_path):
    store = LocalArtifactStore(tmp_path)
    first = store.stage(io.BytesIO(b"first"), _request())
    first_ref = store.commit(first, store.verify_pending(first))
    second = store.stage(io.BytesIO(b"second"), _request("opaque-request-2"))
    second_receipt = store.verify_pending(second)
    occupied = store.root / "committed" / f"{second.object_key}.blob"
    occupied.write_bytes(b"occupied")

    with pytest.raises(FileExistsError):
        store.commit(second, second_receipt)
    assert occupied.read_bytes() == b"occupied"
    with store.open_verified(first_ref) as accepted:
        assert accepted.read() == b"first"


def test_changed_source_file_is_rejected(tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path / "store")
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    real_snapshot = store._source_snapshot
    calls = 0

    def changing_snapshot(path):
        nonlocal calls
        calls += 1
        snapshot = real_snapshot(path)
        if calls == 2:
            return dataclasses.replace(snapshot, mtime_ns=snapshot.mtime_ns + 1)
        return snapshot

    monkeypatch.setattr(store, "_source_snapshot", changing_snapshot)

    with pytest.raises(ArtifactIntegrityError, match="changed during staging"):
        store.stage(source, _request())
    assert not list((store.root / "pending").glob("*.blob"))


def test_corrupt_pending_is_quarantined_and_corrupt_committed_fails_closed(tmp_path):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"pending"), _request())
    (store.root / "pending" / f"{pending.generation_id}.blob").write_bytes(b"corrupt")

    with pytest.raises(ArtifactIntegrityError):
        store.verify_pending(pending)
    assert list((store.root / "quarantine").glob(f"{pending.generation_id}.*"))

    clean = store.stage(io.BytesIO(b"committed"), _request("clean-committed"))
    ref = store.commit(clean, store.verify_pending(clean))
    committed_path = store.root / "committed" / f"{ref.object_key}.blob"
    committed_path.write_bytes(b"corrupt")

    with pytest.raises(ArtifactIntegrityError):
        store.verify_committed(ref)
    assert committed_path.exists()


def test_reconcile_quarantines_only_stale_or_mismatched_pending_residue(tmp_path):
    store = LocalArtifactStore(tmp_path)
    healthy = store.stage(io.BytesIO(b"healthy"), _request("healthy"))
    committed = store.stage(io.BytesIO(b"committed"), _request("committed"))
    ref = store.commit(committed, store.verify_pending(committed))
    stale = store.root / "pending" / ("a" * 32 + ".blob")
    stale.write_bytes(b"crash residue")
    old = 1_600_000_000
    os.utime(stale, (old, old))
    mismatched = store.stage(io.BytesIO(b"mismatch"), _request("mismatched"))
    (store.root / "pending" / f"{mismatched.generation_id}.blob").write_bytes(b"bad")

    quarantined = store.reconcile_pending(stale_after=timedelta(hours=1))

    assert "a" * 32 in quarantined
    assert mismatched.generation_id not in quarantined
    for path in (store.root / "pending").glob(f"{mismatched.generation_id}.*"):
        os.utime(path, (old, old))
    assert mismatched.generation_id in store.reconcile_pending(stale_after=timedelta(hours=1))
    assert healthy.generation_id not in quarantined
    store.verify_pending(healthy)
    assert (store.root / "committed" / f"{ref.object_key}.blob").read_bytes() == b"committed"
    store.verify_committed(ref)


def test_concurrent_commit_of_same_generation_converges_without_overwrite(tmp_path):
    stores = (LocalArtifactStore(tmp_path), LocalArtifactStore(tmp_path))
    pending = stores[0].stage(io.BytesIO(b"once"), _request())
    receipt = stores[0].verify_pending(pending)
    barrier = threading.Barrier(3)
    refs = []
    failures = []

    def publish(store):
        barrier.wait()
        try:
            refs.append(store.commit(pending, receipt))
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=publish, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert not failures
    assert len(refs) == 2 and refs[0] == refs[1]
    assert len(list((stores[0].root / "committed").glob("*.blob"))) == 1


def test_metadata_is_canonical_and_tracks_unprotected_local_lifecycle(tmp_path):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"metadata"), _request())
    pending_sidecar = store.root / "pending" / f"{pending.generation_id}.json"
    pending_bytes = pending_sidecar.read_bytes()
    pending_value = json.loads(pending_bytes)

    assert pending_bytes == json.dumps(
        pending_value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii") + b"\n"
    assert pending_value["durability_state"] == "pending"
    assert pending_value["encryption_algorithm"] == "none"

    ref = store.commit(pending, store.verify_pending(pending))
    committed_bytes = (store.root / "committed" / f"{ref.object_key}.json").read_bytes()
    committed_value = json.loads(committed_bytes)
    assert committed_bytes == json.dumps(
        committed_value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii") + b"\n"
    assert ref.durability_state == "committed_unprotected"
    assert committed_value["durability_state"] == "committed_unprotected"
    assert "durable" not in committed_value


def test_path_traversal_in_forged_handles_is_rejected(tmp_path):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"value"), _request())
    escaped = dataclasses.replace(pending, generation_id="../escape")

    with pytest.raises(ValueError, match="opaque key"):
        store.verify_pending(escaped)
    ref = store.commit(pending, store.verify_pending(pending))
    escaped_ref = dataclasses.replace(ref, object_key="..\\escape")
    with pytest.raises(ValueError, match="content address"):
        store.open_verified(escaped_ref)


def test_open_verified_hashes_before_returning_content(tmp_path):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"accepted"), _request())
    ref = store.commit(pending, store.verify_pending(pending))

    accepted = store.open_verified(ref)
    try:
        assert accepted.tell() == 0
        assert accepted.read() == b"accepted"
    finally:
        accepted.close()

    (store.root / "committed" / f"{ref.object_key}.blob").write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError):
        store.open_verified(ref)


def test_request_id_is_durable_idempotency_key_before_and_after_commit(tmp_path):
    store = LocalArtifactStore(tmp_path)
    request = _request("durable-request")

    first = store.stage(io.BytesIO(b"one payload"), request)
    retry_pending = LocalArtifactStore(tmp_path).stage(io.BytesIO(b"one payload"), request)
    assert retry_pending == first

    first_ref = store.commit(first, store.verify_pending(first))
    retry = LocalArtifactStore(tmp_path)
    committed_pending = retry.stage(io.BytesIO(b"one payload"), request)
    committed_ref = retry.commit(committed_pending, retry.verify_pending(committed_pending))

    assert committed_pending == first
    assert committed_ref == first_ref
    assert len(list((store.root / "committed").glob("*.blob"))) == 1
    assert request.request_id not in first.object_key
    assert first.object_key == first.content_sha256
    for registry_path in (store.root / "registry").glob("*.json"):
        assert len(registry_path.stem) == 32
        int(registry_path.stem, 16)
        assert request.request_id not in registry_path.name
        assert first.content_sha256 not in registry_path.name


@pytest.mark.parametrize(
    ("write_request", "payload"),
    [
        (ArtifactWriteRequest("same-request", media_type="text/plain", schema_version=1), b"payload"),
        (ArtifactWriteRequest("same-request", media_type="application/json", schema_version=2), b"payload"),
        (ArtifactWriteRequest("same-request", media_type="text/plain", schema_version=1), b"different"),
    ],
    ids=["media-type", "schema-version", "payload"],
)
def test_same_request_with_any_immutable_mismatch_fails(tmp_path, write_request, payload):
    store = LocalArtifactStore(tmp_path)
    store.stage(
        io.BytesIO(b"payload"),
        ArtifactWriteRequest("same-request", media_type="application/json", schema_version=1),
    )

    with pytest.raises(ArtifactConflictError, match="request_id"):
        LocalArtifactStore(tmp_path).stage(io.BytesIO(payload), write_request)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("request_id", "other-request"),
        ("object_key", "f" * 32),
        ("content_sha256", "0" * 64),
        ("byte_count", 999),
        ("media_type", "text/plain"),
        ("schema_version", 99),
        ("created_at", "2000-01-01T00:00:00Z"),
        ("encryption_algorithm", "forged"),
        ("encryption_provider_version", "forged-v1"),
        ("provider_version", "forged-provider"),
        ("durability_state", "durable"),
        ("state", ArtifactState.COMMITTED),
    ],
)
def test_pending_handle_full_field_tamper_is_rejected_by_registry(tmp_path, field, replacement):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"trusted"), _request("tamper-pending"))

    with pytest.raises((ArtifactIntegrityError, ValueError)):
        store.verify_pending(dataclasses.replace(pending, **{field: replacement}))


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("request_id", "forged-request"),
        ("media_type", "text/forged"),
        ("schema_version", 77),
        ("created_at", "1999-01-01T00:00:00Z"),
    ],
)
def test_pending_sidecar_cannot_authenticate_matching_tampered_handle(tmp_path, field, replacement):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"trusted"), _request("registry-authority"))
    sidecar = store.root / "pending" / f"{pending.generation_id}.json"
    forged = dataclasses.replace(pending, **{field: replacement})
    _rewrite_canonical(sidecar, lambda value: value.__setitem__(field, replacement))

    with pytest.raises(ArtifactIntegrityError, match="registry"):
        store.verify_pending(forged)


def test_verification_receipt_full_field_tamper_is_rejected(tmp_path):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"verified"), _request("receipt-tamper"))
    receipt = store.verify_pending(pending)

    for field in dataclasses.fields(receipt):
        if field.name == "verified_at":
            replacement = "1990-01-01T00:00:00Z"
        elif field.name == "state":
            replacement = ArtifactState.PENDING
        elif field.type in {int, "int"}:
            replacement = 123456
        else:
            replacement = "f" * 64 if "sha256" in field.name else "forged"
        forged = dataclasses.replace(receipt, **{field.name: replacement})
        with pytest.raises(ArtifactIntegrityError):
            store.commit(pending, forged)


def test_reconcile_preserves_recent_incomplete_generation_until_stale(tmp_path):
    store = LocalArtifactStore(tmp_path)
    generation_id = secrets.token_hex(16)
    blob = store.root / "pending" / f"{generation_id}.blob"
    blob.write_bytes(b"partial")

    assert generation_id not in store.reconcile_pending(stale_after=timedelta(hours=1))
    assert blob.exists()
    old = time.time() - 7200
    os.utime(blob, (old, old))
    assert generation_id in store.reconcile_pending(stale_after=timedelta(hours=1))


def test_reconcile_does_not_quarantine_an_active_stage(tmp_path):
    store = LocalArtifactStore(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    class SlowReader(io.BytesIO):
        def read(self, size=-1):
            chunk = super().read(size)
            if chunk and not entered.is_set():
                entered.set()
                assert release.wait(5)
            return chunk

    result = []
    thread = threading.Thread(
        target=lambda: result.append(store.stage(SlowReader(b"active-stage"), _request("active-stage")))
    )
    thread.start()
    assert entered.wait(5)
    reconciled = []
    reconcile = threading.Thread(
        target=lambda: reconciled.append(store.reconcile_pending(stale_after=0))
    )
    reconcile.start()
    try:
        reconcile.join(2)
        assert not reconcile.is_alive()
    finally:
        release.set()
        thread.join(5)
    assert len(result) == 1
    assert reconciled == [()]


def test_caller_stream_read_does_not_hold_global_coordination_lock(tmp_path):
    store = LocalArtifactStore(tmp_path)
    unrelated: list[object] = []
    failures: list[BaseException] = []

    def stage_unrelated() -> None:
        try:
            unrelated.append(store.stage(io.BytesIO(b"other"), _request("other-request")))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    class CoordinatingReader(io.BytesIO):
        invoked = False

        def read(self, size=-1):
            if not self.invoked:
                self.invoked = True
                thread = threading.Thread(target=stage_unrelated)
                thread.start()
                thread.join(2)
                if thread.is_alive():
                    raise AssertionError("caller read was invoked while the coordination lock was held")
            return super().read(size)

    outer = store.stage(CoordinatingReader(b"outer"), _request("outer-request"))
    assert not failures
    assert len(unrelated) == 1
    assert outer.request_id == "outer-request"


def test_caller_stream_may_reenter_store_without_deadlock(tmp_path):
    store = LocalArtifactStore(tmp_path)

    class ReentrantReader(io.BytesIO):
        invoked = False

        def read(self, size=-1):
            if not self.invoked:
                self.invoked = True
                nested = store.stage(io.BytesIO(b"nested"), _request("nested-request"))
                assert nested.request_id == "nested-request"
            return super().read(size)

    outer = store.stage(ReentrantReader(b"outer"), _request("reentrant-outer"))
    assert outer.request_id == "reentrant-outer"


def test_lock_retry_policy_waits_past_nine_seconds_and_times_out_fail_closed(monkeypatch):
    clock = [0.0]
    attempts = [0]

    def monotonic() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    def eventually_locks(_descriptor: int) -> bool:
        attempts[0] += 1
        return attempts[0] == 4

    monkeypatch.setattr(artifacts_module, "_try_lock_descriptor_once", eventually_locks)
    assert artifacts_module._lock_descriptor(
        0,
        blocking=True,
        timeout_seconds=20,
        retry_interval_seconds=4,
        monotonic=monotonic,
        sleeper=sleep,
    )
    assert clock[0] == 12

    monkeypatch.setattr(artifacts_module, "_try_lock_descriptor_once", lambda _descriptor: False)
    clock[0] = 0
    with pytest.raises(ArtifactLockTimeout, match="timed out"):
        artifacts_module._lock_descriptor(
            0,
            blocking=True,
            timeout_seconds=10,
            retry_interval_seconds=4,
            monotonic=monotonic,
            sleeper=sleep,
        )
    assert clock[0] >= 10


def test_one_deadline_covers_thread_file_and_generation_locks(tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path, lock_timeout_seconds=0.30)
    clock = [0.0]

    class DelayedThreadLock:
        def acquire(self, *, timeout):
            assert timeout == pytest.approx(0.30)
            clock[0] += 0.20
            return True

        def release(self):
            pass

    attempts = [0]

    def try_lock(_descriptor: int) -> bool:
        attempts[0] += 1
        return attempts[0] == 1

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    store._thread_lock = DelayedThreadLock()
    store._monotonic = lambda: clock[0]
    store._sleeper = sleep
    monkeypatch.setattr(artifacts_module, "_try_lock_descriptor_once", try_lock)
    monkeypatch.setattr(artifacts_module, "_unlock_descriptor", lambda _descriptor: None)
    deadline = store._new_deadline()
    claim = store.root / "pending" / f"{'d' * 32}.active"

    with pytest.raises(ArtifactLockTimeout, match="timed out"):
        with store._coordination(deadline=deadline):
            with store._generation_claim(claim, deadline=deadline):
                pass
    assert clock[0] == pytest.approx(0.30, abs=0.001)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL creation regression")
def test_windows_secret_temp_is_restricted_before_first_secret_byte(tmp_path):
    parent = tmp_path / "permissive"
    parent.mkdir()
    subprocess.run(
        ["icacls", str(parent), "/grant", "*S-1-1-0:(OI)(CI)(F)"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    root = parent / "root"
    anchor = parent / "anchor"
    observed: dict[str, object] = {}

    def inspect_before_write(point: str) -> None:
        if point != "before_coordinator_secret_write":
            return
        temporary = next(anchor.glob("*.key.tmp"))
        observed["size"] = temporary.stat().st_size
        observed["temp_acl"] = subprocess.run(
            ["icacls", str(temporary)],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
        observed["anchor_acl"] = subprocess.run(
            ["icacls", str(anchor)],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
        raise RuntimeError("pre-write probe")

    with pytest.raises(RuntimeError, match="pre-write probe"):
        LocalArtifactStore(root, anchor_root=anchor, fault_injector=inspect_before_write)

    assert observed["size"] == 0
    for acl in (str(observed["temp_acl"]), str(observed["anchor_acl"])):
        assert "Everyone:" not in acl
        assert "*S-1-1-0:" not in acl


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL handle-capture regression")
def test_windows_permissive_existing_anchor_cannot_capture_key_temp_handle(tmp_path):
    parent = tmp_path / "parent"
    root = parent / "root"
    anchor = parent / "anchor"
    root.mkdir(parents=True)
    anchor.mkdir()
    for directory in (root, anchor):
        for identity in ("*S-1-1-0", "*S-1-5-11"):
            subprocess.run(
                ["icacls", str(directory), "/grant", f"{identity}:(OI)(CI)(F)"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )

    before_write = threading.Event()
    attempted = threading.Event()
    stop = threading.Event()
    captured: list[io.BufferedReader] = []

    def handle_opener() -> None:
        assert before_write.wait(10)
        while not stop.is_set():
            for temporary in anchor.glob("*.key.tmp"):
                attempted.set()
                try:
                    captured.append(temporary.open("rb"))
                    return
                except PermissionError:
                    pass
            time.sleep(0.001)

    opener = threading.Thread(target=handle_opener)
    opener.start()

    def pause_before_write(point: str) -> None:
        if point != "before_coordinator_secret_write":
            return
        before_write.set()
        assert attempted.wait(10)
        time.sleep(0.05)
        stop.set()

    try:
        store = LocalArtifactStore(root, anchor_root=anchor, fault_injector=pause_before_write)
    finally:
        stop.set()
        opener.join(10)

    leaked = []
    for handle in captured:
        try:
            handle.seek(0)
            leaked.append(handle.read())
        finally:
            handle.close()
    assert captured == [], leaked

    for path in (root, anchor, store.anchor_root / ".coordinator.key"):
        acl = subprocess.run(
            ["icacls", str(path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
        assert "Everyone:" not in acl
        assert "*S-1-1-0:" not in acl
        assert "Authenticated Users:" not in acl
        assert "*S-1-5-11:" not in acl
        assert os.environ["USERNAME"].lower() in acl.lower()
        assert "system" in acl.lower()
        assert "administrators" in acl.lower()


def test_lifecycle_exposes_coordinator_neutral_states_and_non_atomic_local_commit(tmp_path):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"state"), _request("state-contract"))
    assert pending.state is ArtifactState.PENDING
    receipt = store.verify_pending(pending)
    assert receipt.state is ArtifactState.OBJECT_VERIFIED
    ref = store.commit(pending, receipt)
    assert ref.state is ArtifactState.COMMITTED
    assert ref.coordinator_atomic is False
    assert ref.generation_id == pending.generation_id


def test_directory_fsync_probe_downgrades_without_durable_claim(tmp_path, monkeypatch):
    monkeypatch.setattr("applypilot.brain.artifacts._probe_directory_fsync", lambda _path: False)
    store = LocalArtifactStore(tmp_path)
    assert store.capabilities.directory_fsync_supported is False
    assert store.capabilities.coordinator_atomic is False
    assert store.capabilities.durability_state == "committed_unprotected"
    assert "durable" not in store.capabilities.durability_state

    pending = store.stage(io.BytesIO(b"unprotected"), _request("unprotected"))
    ref = store.commit(pending, store.verify_pending(pending))
    assert ref.durability_state == "committed_unprotected"


def test_local_provider_version_remains_explicit_and_compatible():
    assert ArtifactWriteRequest("provider-version").provider_version == "local-v1"


def test_committed_reference_full_field_tamper_is_rejected_by_registry(tmp_path):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"committed authority"), _request("committed-tamper"))
    ref = store.commit(pending, store.verify_pending(pending))

    for field in dataclasses.fields(ref):
        if field.name == "state":
            replacement = ArtifactState.PENDING
        elif field.name == "coordinator_atomic":
            replacement = True
        elif field.type in {int, "int"}:
            replacement = 123456
        else:
            replacement = "f" * 64 if "sha256" in field.name else "forged"
        forged = dataclasses.replace(ref, **{field.name: replacement})
        with pytest.raises((ArtifactIntegrityError, ValueError)):
            store.verify_committed(forged)


def test_committed_sidecar_cannot_authenticate_matching_tampered_reference(tmp_path):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"committed trusted"), _request("committed-sidecar"))
    ref = store.commit(pending, store.verify_pending(pending))
    sidecar = store.root / "committed" / f"{ref.object_key}.json"
    forged = dataclasses.replace(ref, media_type="text/forged")
    _rewrite_canonical(sidecar, lambda value: value.__setitem__("media_type", "text/forged"))

    with pytest.raises(ArtifactIntegrityError, match="registry"):
        store.verify_committed(forged)


def test_reconcile_sharing_violation_leaves_original_pair_for_retry(tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"sharing"), _request("sharing-violation"))
    pending_dir = store.root / "pending"
    old = time.time() - 7200
    for path in pending_dir.glob(f"{pending.generation_id}.*"):
        os.utime(path, (old, old))
    real_copy = store._copy_file_exclusive

    def sharing_copy(source, destination):
        source_path = Path(source)
        if source_path.parent == pending_dir and source_path.suffix == ".json":
            raise PermissionError("sharing violation")
        return real_copy(source, destination)

    monkeypatch.setattr(store, "_copy_file_exclusive", sharing_copy)
    assert store.reconcile_pending(stale_after=timedelta(hours=1)) == ()
    assert (pending_dir / f"{pending.generation_id}.blob").exists()
    assert (pending_dir / f"{pending.generation_id}.json").exists()


def test_open_verified_returns_detached_read_only_snapshot(tmp_path):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"immutable snapshot"), _request("snapshot"))
    ref = store.commit(pending, store.verify_pending(pending))

    snapshot = store.open_verified(ref)
    committed = store.root / "committed" / f"{ref.object_key}.blob"
    committed.write_bytes(b"mutated after verification")
    try:
        assert snapshot.read() == b"immutable snapshot"
        assert not snapshot.writable()
        with pytest.raises((io.UnsupportedOperation, AttributeError)):
            snapshot.write(b"forged")
        with pytest.raises(io.UnsupportedOperation):
            snapshot.fileno()
    finally:
        snapshot.close()


@pytest.mark.parametrize("kill_point", ["after_committed_data", "after_committed_metadata"])
def test_commit_restarts_after_publication_kill_points(tmp_path, monkeypatch, kill_point):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"restartable"), _request(f"restart-{kill_point}"))
    receipt = store.verify_pending(pending)

    def crash(point):
        if point == kill_point:
            raise RuntimeError(f"crash at {point}")

    monkeypatch.setattr(store, "_kill_point", crash)
    with pytest.raises(RuntimeError, match=kill_point):
        store.commit(pending, receipt)

    restarted = LocalArtifactStore(tmp_path)
    ref = restarted.commit(pending, restarted.verify_pending(pending))
    with restarted.open_verified(ref) as snapshot:
        assert snapshot.read() == b"restartable"


def test_stage_crash_residue_is_preserved_while_recent_then_quarantined(tmp_path, monkeypatch):
    store = LocalArtifactStore(tmp_path)

    def crash(point):
        if point == "after_pending_data":
            raise RuntimeError("stage crash")

    monkeypatch.setattr(store, "_kill_point", crash)
    with pytest.raises(RuntimeError, match="stage crash"):
        store.stage(io.BytesIO(b"partial-stage"), _request("stage-crash"))

    restarted = LocalArtifactStore(tmp_path)
    residues = list((restarted.root / "pending").glob("*.blob"))
    assert len(residues) == 1
    assert restarted.reconcile_pending(stale_after=timedelta(hours=1)) == ()
    old = time.time() - 7200
    for path in (restarted.root / "pending").iterdir():
        os.utime(path, (old, old))
    assert restarted.reconcile_pending(stale_after=timedelta(hours=1))


def test_cross_process_same_request_converges_to_one_object(tmp_path):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    output = context.Queue()
    processes = [context.Process(target=_process_publish, args=(str(tmp_path), start, output)) for _ in range(4)]
    for process in processes:
        process.start()
    start.set()
    results = [output.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(20)
        assert process.exitcode == 0

    assert all(result[0] == "ok" for result in results), results
    assert len({result[1:] for result in results}) == 1
    assert len(list((tmp_path / "committed").glob("*.blob"))) == 1


def test_cross_process_distinct_requests_share_one_content_addressed_blob(tmp_path):
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    output = context.Queue()
    processes = [
        context.Process(
            target=_process_publish_distinct_request,
            args=(str(tmp_path), f"distinct-{index}", start, output),
        )
        for index in range(4)
    ]
    for process in processes:
        process.start()
    start.set()
    results = [output.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(30)
        assert process.exitcode == 0

    assert all(result[0] == "ok" for result in results), results
    assert len({result[1] for result in results}) == 4
    assert len({result[2] for result in results}) == 4
    expected = hashlib.sha256(b"shared-cross-process").hexdigest()
    assert {result[3] for result in results} == {expected}
    assert len(list((tmp_path / "committed").glob("*.blob"))) == 1


def test_joint_sidecar_and_complete_receipt_chain_edits_fail_hmac(tmp_path):
    store, _pending, _receipt, ref = _committed_fixture(tmp_path)
    for registry_path in (store.root / "registry").glob("*.json"):
        _rewrite_canonical(
            registry_path,
            lambda value: value["receipt"].__setitem__("media_type", "text/forged"),
        )
    sidecar = store.root / "committed" / f"{ref.object_key}.json"
    _rewrite_canonical(sidecar, lambda value: value.__setitem__("media_type", "text/forged"))
    forged = dataclasses.replace(ref, media_type="text/forged")

    with pytest.raises(ArtifactIntegrityError, match="HMAC|chain"):
        LocalArtifactStore(tmp_path).verify_committed(forged)


def test_registry_tail_delete_and_rollback_fail_closed(tmp_path):
    store, _pending, _receipt, _ref = _committed_fixture(tmp_path, "delete-tail")
    committed_record = next(
        path
        for path in (store.root / "registry").glob("*.json")
        if json.loads(path.read_bytes()).get("lifecycle") == "committed"
        or json.loads(path.read_bytes()).get("record_type") == "committed"
    )
    committed_record.unlink()

    with pytest.raises(ArtifactIntegrityError, match="truncat|rollback|chain"):
        LocalArtifactStore(tmp_path)


def test_registry_duplicate_sequence_fails_closed(tmp_path):
    store, _pending, _receipt, _ref = _committed_fixture(tmp_path, "duplicate-chain")
    source = next((store.root / "registry").glob("*.json"))
    shutil.copyfile(source, store.root / "registry" / f"{secrets.token_hex(16)}.json")

    with pytest.raises(ArtifactIntegrityError, match="duplicate|sequence|chain"):
        LocalArtifactStore(tmp_path)


def test_registry_reordered_sequence_fails_closed(tmp_path):
    store, _pending, _receipt, _ref = _committed_fixture(tmp_path, "reordered-chain")
    records = sorted((store.root / "registry").glob("*.json"))
    first = json.loads(records[0].read_bytes())
    second = json.loads(records[1].read_bytes())
    first["sequence"], second["sequence"] = second["sequence"], first["sequence"]
    records[0].write_bytes(
        json.dumps(first, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    )
    records[1].write_bytes(
        json.dumps(second, sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    )

    with pytest.raises(ArtifactIntegrityError, match="HMAC|sequence|chain"):
        LocalArtifactStore(tmp_path)


def test_registry_truncated_record_fails_closed(tmp_path):
    store, _pending, _receipt, _ref = _committed_fixture(tmp_path, "truncated-record")
    next((store.root / "registry").glob("*.json")).write_bytes(b'{"sequence":')

    with pytest.raises(ArtifactIntegrityError, match="malformed|chain"):
        LocalArtifactStore(tmp_path)


def test_coordinator_key_replacement_invalidates_existing_registry(tmp_path):
    store, _pending, _receipt, _ref = _committed_fixture(tmp_path, "key-replacement")
    key_path = store.anchor_root / ".coordinator.key"
    assert key_path.exists()
    if os.name == "nt":
        acl = subprocess.run(
            ["icacls", str(key_path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
        assert os.environ["USERNAME"].lower() in acl.lower()
        assert "Everyone:" not in acl
        assert "Authenticated Users:" not in acl
        assert "BUILTIN\\Users:" not in acl
    else:
        assert stat.S_IMODE(key_path.stat().st_mode) & 0o077 == 0
    key_path.write_bytes(secrets.token_bytes(64))

    with pytest.raises(ArtifactIntegrityError, match="key|HMAC|chain"):
        LocalArtifactStore(tmp_path)


def test_artifact_root_rollback_is_detected_by_independent_newer_anchor(tmp_path):
    root = tmp_path / "artifacts"
    anchor = tmp_path / "coordinator-anchor"
    snapshot = tmp_path / "artifact-snapshot"
    store = LocalArtifactStore(root, anchor_root=anchor)
    pending = store.stage(io.BytesIO(b"rollback"), _request("rollback-anchor"))
    shutil.copytree(root, snapshot)
    store.commit(pending, store.verify_pending(pending))

    shutil.rmtree(root)
    shutil.copytree(snapshot, root)

    with pytest.raises(ArtifactIntegrityError, match="truncat|rollback|chain"):
        LocalArtifactStore(root, anchor_root=anchor)


def test_anchor_root_is_separate_and_reports_same_volume_honestly(tmp_path):
    root = tmp_path / "artifacts"
    anchor = tmp_path / "anchor"
    store = LocalArtifactStore(root, anchor_root=anchor)

    assert store.anchor_root == anchor.resolve()
    assert store.capabilities.anchor_path_separated is True
    assert store.capabilities.anchor_same_volume == (root.stat().st_dev == anchor.stat().st_dev)

    with pytest.raises(ValueError, match="anchor_root"):
        LocalArtifactStore(root, anchor_root=root / "nested-anchor")


def test_concurrent_initialization_binds_artifact_root_to_exactly_one_anchor(tmp_path):
    root = tmp_path / "artifacts"
    anchors = (tmp_path / "anchor-a", tmp_path / "anchor-b")
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    output = context.Queue()
    processes = [
        context.Process(
            target=_process_initialize_with_anchor,
            args=(str(root), str(anchor), start, output),
        )
        for anchor in anchors
    ]
    for process in processes:
        process.start()
    start.set()
    results = [output.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(30)
        assert process.exitcode == 0

    successes = [result for result in results if result[0] == "ok"]
    failures = [result for result in results if result[0] == "error"]
    assert len(successes) == len(failures) == 1, results
    assert failures[0][2] == "ArtifactIntegrityError"
    winner = Path(successes[0][1])
    loser = Path(failures[0][1])
    assert not (loser / ".coordinator.key").exists()
    restarted = LocalArtifactStore(root, anchor_root=winner)
    assert restarted._load_coordinator_key().key_id == successes[0][2]
    with pytest.raises(ArtifactIntegrityError, match="bound|anchor|identity"):
        LocalArtifactStore(root, anchor_root=loser)
    records = list((root / "registry").glob("*.json"))
    assert records == []


def test_authenticated_root_binding_rejects_tamper_and_hardlink(tmp_path):
    root = tmp_path / "artifacts"
    anchor = tmp_path / "anchor"
    LocalArtifactStore(root, anchor_root=anchor)
    binding = root / ".artifact-anchor-binding.json"
    outside = tmp_path / "outside-binding.json"
    os.link(binding, outside)

    with pytest.raises(ArtifactIntegrityError, match="hardlink|binding|link"):
        LocalArtifactStore(root, anchor_root=anchor)

    outside.unlink()
    _rewrite_canonical(binding, lambda value: value.__setitem__("anchor_root", "forged"))
    with pytest.raises(ArtifactIntegrityError, match="HMAC|binding|anchor"):
        LocalArtifactStore(root, anchor_root=anchor)


def test_authenticated_root_binding_deletion_fails_closed(tmp_path):
    root = tmp_path / "artifacts"
    anchor = tmp_path / "anchor"
    LocalArtifactStore(root, anchor_root=anchor)
    (root / ".artifact-anchor-binding.json").unlink()

    with pytest.raises(ArtifactIntegrityError, match="binding.*deleted|binding.*rollback"):
        LocalArtifactStore(root, anchor_root=anchor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink regression")
def test_root_and_storage_subdirectory_symlinks_are_rejected(tmp_path):
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ArtifactIntegrityError, match="symlink|reparse|link"):
        LocalArtifactStore(linked_root, anchor_root=tmp_path / "anchor-a")

    store = LocalArtifactStore(tmp_path / "root", anchor_root=tmp_path / "anchor-b")
    outside = tmp_path / "outside"
    outside.mkdir()
    store._committed_dir.rmdir()
    store._committed_dir.symlink_to(outside, target_is_directory=True)
    pending = store.stage(io.BytesIO(b"escape"), _request("symlink-escape"))
    with pytest.raises(ArtifactIntegrityError, match="symlink|reparse|escape"):
        store.commit(pending, store.verify_pending(pending))
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_windows_committed_junction_escape_is_rejected(tmp_path):
    store = LocalArtifactStore(tmp_path / "root", anchor_root=tmp_path / "anchor")
    outside = tmp_path / "outside"
    outside.mkdir()
    store._committed_dir.rmdir()
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(store._committed_dir), str(outside)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    pending = store.stage(io.BytesIO(b"junction"), _request("junction-escape"))
    with pytest.raises(ArtifactIntegrityError, match="reparse|junction|escape"):
        store.commit(pending, store.verify_pending(pending))
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_windows_root_and_anchor_junctions_are_rejected(tmp_path):
    real_root = tmp_path / "real-root"
    real_anchor = tmp_path / "real-anchor"
    real_root.mkdir()
    real_anchor.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_anchor = tmp_path / "linked-anchor"
    for link, target in ((linked_root, real_root), (linked_anchor, real_anchor)):
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )

    with pytest.raises(ArtifactIntegrityError, match="reparse|junction|link"):
        LocalArtifactStore(linked_root, anchor_root=tmp_path / "safe-anchor")
    with pytest.raises(ArtifactIntegrityError, match="reparse|junction|link"):
        LocalArtifactStore(tmp_path / "safe-root", anchor_root=linked_anchor)


@pytest.mark.parametrize(
    "kill_point",
    [
        "after_pending_data",
        "after_pending_metadata",
        "after_registry_record_publish:pending",
    ],
)
def test_actual_stage_process_death_recovers_or_quarantines(tmp_path, kill_point):
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_process_stage_exit, args=(str(tmp_path), kill_point))
    process.start()
    process.join(20)
    assert process.exitcode == 71

    restarted = LocalArtifactStore(tmp_path)
    if kill_point == "after_registry_record_publish:pending":
        pending = restarted.stage(io.BytesIO(b"abrupt-stage"), _request("abrupt-stage"))
        ref = restarted.commit(pending, restarted.verify_pending(pending))
        with restarted.open_verified(ref) as snapshot:
            assert snapshot.read() == b"abrupt-stage"
    else:
        assert restarted.reconcile_pending(stale_after=timedelta(hours=1)) == ()
        old = time.time() - 7200
        for path in (restarted.root / "pending").iterdir():
            os.utime(path, (old, old))
        assert restarted.reconcile_pending(stale_after=timedelta(hours=1))


@pytest.mark.parametrize(
    "kill_point",
    [
        "after_registry_record_publish:commit_intent",
        "after_committed_data",
        "after_committed_metadata",
        "after_registry_record_publish:committed",
    ],
)
def test_actual_commit_process_death_recovers_in_separate_process(tmp_path, kill_point):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"abrupt-commit"), _request(f"abrupt-{kill_point}"))
    receipt = store.verify_pending(pending)
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_process_commit_exit,
        args=(str(tmp_path), pending, receipt, kill_point),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 72

    restarted = LocalArtifactStore(tmp_path)
    ref = restarted.commit(pending, restarted.verify_pending(pending))
    with restarted.open_verified(ref) as snapshot:
        assert snapshot.read() == b"abrupt-commit"


def test_reconcile_alone_finishes_authenticated_intent_after_process_death(tmp_path):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"intent-recovery"), _request("intent-recovery"))
    receipt = store.verify_pending(pending)
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_process_commit_exit,
        args=(str(tmp_path), pending, receipt, "after_registry_record_publish:commit_intent"),
    )
    process.start()
    process.join(20)
    assert process.exitcode == 72
    assert (store.root / "pending" / f"{pending.generation_id}.blob").exists()

    restarted = LocalArtifactStore(tmp_path)
    assert restarted.reconcile_pending(stale_after=0) == ()
    assert not list((restarted.root / "pending").glob(f"{pending.generation_id}.*"))
    assert not list((restarted.root / "quarantine").glob(f"{pending.generation_id}.*"))
    assert (restarted.root / "committed" / f"{pending.content_sha256}.blob").read_bytes() == b"intent-recovery"

    ref = restarted.commit(pending, receipt)
    with restarted.open_verified(ref) as snapshot:
        assert snapshot.read() == b"intent-recovery"


def test_terminated_stage_releases_claim_and_stale_residue_reconciles(tmp_path):
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    process = context.Process(
        target=_process_blocked_stage,
        args=(str(tmp_path), entered, release),
    )
    process.start()
    assert entered.wait(20)
    process.terminate()
    process.join(20)
    assert process.exitcode not in {None, 0}

    restarted = LocalArtifactStore(tmp_path)
    old = time.time() - 7200
    for path in (restarted.root / "pending").iterdir():
        os.utime(path, (old, old))
    assert restarted.reconcile_pending(stale_after=timedelta(hours=1))


@pytest.mark.skipif(os.name != "nt", reason="deterministic Windows lock-order regression")
def test_windows_commit_reconcile_race_uses_one_cross_process_lock_order(tmp_path):
    store = LocalArtifactStore(tmp_path)
    pending = store.stage(io.BytesIO(b"windows-race"), _request("windows-race"))
    receipt = store.verify_pending(pending)
    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    process = context.Process(
        target=_process_commit_pause,
        args=(str(tmp_path), pending, receipt, entered, release),
    )
    process.start()
    assert entered.wait(20)

    result = []
    reconcile = threading.Thread(target=lambda: result.append(store.reconcile_pending(stale_after=0)))
    reconcile.start()
    time.sleep(0.25)
    assert reconcile.is_alive()
    release.set()
    process.join(20)
    reconcile.join(20)

    assert process.exitcode == 0
    assert result == [()]
