from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

from applypilot.apply import lifecycle_fault


def test_concurrent_fault_writers_preserve_every_fault(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle_fault.config, "DB_PATH", tmp_path / "applypilot.db")

    with ThreadPoolExecutor(max_workers=12) as executor:
        paths = list(
            executor.map(
                lambda pid: lifecycle_fault.persist_lifecycle_hard_fault(
                    "concurrent cleanup uncertain", pid=pid
                ),
                range(100, 124),
            )
        )

    assert len(set(paths)) == 24
    assert set(lifecycle_fault.lifecycle_hard_fault_paths()) == set(paths)
    records = [lifecycle_fault.load_lifecycle_hard_fault(path) for path in paths]
    assert len({record.payload["fault_id"] for record in records}) == 24
    assert {record.payload["pid"] for record in records} == set(range(100, 124))


def test_fault_reader_includes_legacy_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle_fault.config, "DB_PATH", tmp_path / "applypilot.db")
    legacy = lifecycle_fault.legacy_lifecycle_hard_fault_marker()
    legacy.write_text(
        json.dumps(
            {
                "version": 1,
                "reason": "legacy uncertain",
                "timestamp": "2026-07-11T00:00:00+00:00",
                "pid": 101,
                "created_at": 0.0,
                "executable_name": "",
                "executable_sha256": "",
                "command_sha256": "",
            }
        ),
        encoding="utf-8",
    )
    current = lifecycle_fault.persist_lifecycle_hard_fault("current uncertain", pid=102)

    assert lifecycle_fault.lifecycle_hard_fault_paths() == [legacy, current]
    assert lifecycle_fault.load_lifecycle_hard_fault(legacy).legacy is True


def test_replacement_race_preserves_replacement_fault(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle_fault.config, "DB_PATH", tmp_path / "applypilot.db")
    original = lifecycle_fault.persist_lifecycle_hard_fault("original", pid=101)
    replacement = lifecycle_fault.persist_lifecycle_hard_fault("replacement", pid=202)
    replacement_bytes = replacement.read_bytes()
    replacement.unlink()
    expected = original.read_bytes()
    real_replace = lifecycle_fault.os.replace

    def replace_with_race(source, target):
        if source == original:
            original.write_bytes(replacement_bytes)
        return real_replace(source, target)

    monkeypatch.setattr(lifecycle_fault.os, "replace", replace_with_race)

    assert lifecycle_fault.remove_lifecycle_fault_if_unchanged(original, expected) is False
    remaining = lifecycle_fault.lifecycle_hard_fault_paths()
    assert len(remaining) == 1
    assert remaining[0].read_bytes() == replacement_bytes
