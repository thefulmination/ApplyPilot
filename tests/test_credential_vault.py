from applypilot.apply.credential_vault import get_or_create


def test_vault_encrypts_and_reuses_generated_password(tmp_path):
    path = tmp_path / "vault.json"
    protect = lambda value: b"encrypted:" + value[::-1]
    unprotect = lambda value: value.removeprefix(b"encrypted:")[::-1]
    first = get_or_create("tenant.example", path=path, protect_fn=protect, unprotect_fn=unprotect)
    second = get_or_create("tenant.example", path=path, protect_fn=protect, unprotect_fn=unprotect)
    assert first == second
    assert first not in path.read_text(encoding="ascii")
    assert len(first) == 24
    assert any(ch.isupper() for ch in first)
    assert any(ch.islower() for ch in first)
    assert any(ch.isdigit() for ch in first)
