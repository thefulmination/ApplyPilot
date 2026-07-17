from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_no_uncommitted_v3_runtime_contract_or_fixture_remains() -> None:
    assert not (ROOT / "src" / "applypilot" / "brain" / "canonical_contract_v3.py").exists()
    fixture_directory = ROOT / "tests" / "fixtures" / "canonical_brain_v3"
    assert not fixture_directory.exists() or not any(fixture_directory.rglob("*"))
    assert not (ROOT / "tests" / "test_brain_cross_language_contract_v3.py").exists()
