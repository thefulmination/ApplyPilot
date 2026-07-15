from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from applypilot.brain.canonical_hash import canonicalize_jcs, canonicalize_jcs_bytes, jcs_sha256


FIXTURE = Path(__file__).parent / "fixtures" / "canonical-brain" / "canonical-hash-v1.json"


def test_fixture_bytes_are_pinned_to_typescript_source_receipt():
    assert len(FIXTURE.read_bytes()) == 2802
    assert (
        hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
        == "fdd9635a788db07552d434a7ee58732d14ea2195dbf0b0e841c203b6c60db39d"
    )


def test_adversarial_ts_vector_matches_fixed_jcs_and_sha256():
    value = {
        "z": [
            10**20,
            1e-6,
            1.25,
            1e-7,
            'line\n"\\',
            {"\ue000": "bmp", "𐀀": "astral", "é": "雪"},
        ],
        "a": {"nested": True},
    }
    expected = (
        '{"a":{"nested":true},"z":[100000000000000000000,0.000001,1.25,1e-7,'
        '"line\\n\\"\\\\",{"é":"雪","𐀀":"astral","":"bmp"}]}'
    )
    assert canonicalize_jcs(value) == expected
    assert jcs_sha256(value) == "d8c17244d78fd8abd5bc764940bf04caf7a6e4bc8ae914d70069159a8574d764"


def test_fixture_input_covers_unicode_numbers_timestamps_policy_decision_and_replay():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    canonical = canonicalize_jcs(fixture["input"])

    assert canonical == fixture["canonicalJson"]
    assert jcs_sha256(fixture["input"]) == fixture["sha256"]
    assert (
        '"propertyNameOrder":{"a":"ascii","𐀀":"linear-b-syllable","😀":"grinning-face","":"bmp-private-use"}'
        in canonical
    )
    assert '"numbers":[-9007199254740991,9007199254740991,100000000000000000000,1e+21,0.000001,1e-7,5e-324' in canonical
    assert '"decisionCreatedAt":"2026-07-10T12:00:00.001Z"' in canonical
    assert '"policyVersion":"policy-雪-v1"' in canonical
    assert '"decisionId":"decision-fixture-v1"' in canonical
    assert '"schemaVersion":"canonical_replay_v4"' in canonical
    assert canonicalize_jcs_bytes(fixture["input"]) == canonical.encode("utf-8")
    assert jcs_sha256(fixture["input"]) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "value",
    [
        math.nan,
        math.inf,
        -math.inf,
        -0.0,
        {"bad": b"bytes"},
        {1: "non-string-key"},
        {"bad": "\ud800"},
    ],
)
def test_rejects_nonfinite_negative_zero_unsafe_and_unsupported_domains(value):
    with pytest.raises((TypeError, ValueError)):
        canonicalize_jcs(value)


def test_boundary_and_exponent_spellings_match_ecmascript_jcs():
    assert canonicalize_jcs([1e20, 1e21, 1e-6, 1e-7, 5e-324, 1.7976931348623157e308]) == (
        "[100000000000000000000,1e+21,0.000001,1e-7,5e-324,1.7976931348623157e+308]"
    )


def test_large_integer_domain_never_rounds_distinct_python_ints_to_same_jcs_number():
    assert canonicalize_jcs(10**20) == "100000000000000000000"
    with pytest.raises(ValueError, match="exactly representable"):
        canonicalize_jcs(10**20 + 1)
    with pytest.raises(ValueError, match="IEEE-754"):
        canonicalize_jcs(10**400)
    assert jcs_sha256(10**20) != jcs_sha256(10**20 + 16_384)


def test_ieee_safe_integer_boundaries_match_cross_language_contract():
    assert canonicalize_jcs(9_007_199_254_740_991) == "9007199254740991"
    assert canonicalize_jcs(-9_007_199_254_740_991) == "-9007199254740991"
    assert canonicalize_jcs(2**53) == "9007199254740992"
    assert canonicalize_jcs(-(2**53)) == "-9007199254740992"
    assert canonicalize_jcs(10**20) == "100000000000000000000"
    for value in (2**53 + 1, -(2**53 + 1), 10**20 + 1):
        with pytest.raises(ValueError, match="exactly representable"):
            canonicalize_jcs(value)
