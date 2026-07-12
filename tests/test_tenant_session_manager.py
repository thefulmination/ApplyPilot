from __future__ import annotations

import json

from applypilot import database, tenants
from applypilot.apply import tenant_sessions


def _local_registry(monkeypatch, tmp_path):
    registry = tmp_path / "tenant_sessions.json"
    profiles = tmp_path / "profiles"
    monkeypatch.setenv("APPLYPILOT_TENANT_SESSION_REGISTRY", str(registry))
    monkeypatch.setenv("APPLYPILOT_TENANT_PROFILE_DIR", str(profiles))
    return registry, profiles


def test_session_registry_stores_metadata_not_credentials(monkeypatch, tmp_path):
    registry, profiles = _local_registry(monkeypatch, tmp_path)
    conn = database.init_db(tmp_path / "brain.db")
    tenants.set_tenant(conn, "acme.wd5.myworkdayjobs.com", "supervised")

    row = tenants.set_session_state(
        conn,
        "acme.wd5.myworkdayjobs.com",
        "supervised",
        reason="login_required",
    )

    assert row["session_state"] == "supervised"
    assert row["profile_id"]
    assert (profiles / row["profile_id"]).is_dir()
    payload = registry.read_text(encoding="utf-8").lower()
    assert "password" not in payload
    assert "cookie" not in payload
    assert "token" not in payload


def test_ready_and_expired_states_are_explicit(monkeypatch, tmp_path):
    _local_registry(monkeypatch, tmp_path)
    host = "acme.wd5.myworkdayjobs.com"
    ready = tenant_sessions.set_session_state(host, "ready", ttl_hours=12)
    selected = tenant_sessions.select_session(host, profile_id=ready["profile_id"])
    assert selected["state"] == "ready"
    assert selected["profile_dir"] == ready["profile_dir"]

    tenant_sessions.set_session_state(
        host,
        "expired",
        profile_id=ready["profile_id"],
        reason="verification_required",
    )
    selected = tenant_sessions.select_session(host, profile_id=ready["profile_id"])
    assert selected["state"] == "expired"
    assert selected["reason"] == "verification_required"


def test_missing_local_profile_defaults_to_supervised(monkeypatch, tmp_path):
    _local_registry(monkeypatch, tmp_path)
    session = tenant_sessions.select_session("new.wd5.myworkdayjobs.com")
    assert session["state"] == "supervised"
    assert session["reason"] == "login_required"


def test_apply_fn_parks_unready_session_before_browser_or_agent(monkeypatch, tmp_path):
    _local_registry(monkeypatch, tmp_path)
    from applypilot.apply import chrome, launcher
    from applypilot.fleet import apply_worker_main

    launches = []
    runs = []
    monkeypatch.setattr(chrome, "launch_chrome", lambda *a, **k: launches.append(1))
    monkeypatch.setattr(launcher, "run_job", lambda *a, **k: runs.append(1))

    host = "new.wd5.myworkdayjobs.com"
    profile_id = tenant_sessions.profile_id_for_host(host)
    result = apply_worker_main.make_apply_fn("sonnet", "codex", slot=4)({
        "url": f"https://{host}/site/job/Role/JR1",
        "application_url": f"https://{host}/site/job/Role/JR1",
        "target_host": host,
        "session_required": True,
        "tenant_profile_id": profile_id,
    })

    assert result["run_status"] == "auth_required"
    assert result["session_preflight_failure"] is True
    assert result["est_cost_usd"] == 0
    assert launches == []
    assert runs == []


def test_ready_session_selects_tenant_profile_for_chrome(monkeypatch, tmp_path):
    _local_registry(monkeypatch, tmp_path)
    from applypilot.apply import browser_preflight, chrome, launcher
    from applypilot.fleet import apply_worker_main

    host = "ready.wd5.myworkdayjobs.com"
    session = tenant_sessions.set_session_state(host, "ready", ttl_hours=12)
    launch_kwargs = []
    proc = object()
    monkeypatch.setattr(
        chrome,
        "launch_chrome",
        lambda worker_id, **kwargs: launch_kwargs.append(kwargs) or proc,
    )
    monkeypatch.setattr(chrome, "cleanup_worker", lambda worker_id, process: True)
    monkeypatch.setattr(
        browser_preflight,
        "check_browser_readiness",
        lambda port: {"ready": True, "reason": "ready", "checks": []},
    )
    monkeypatch.setattr(launcher, "_should_prearm_inbox_auth", lambda job: False)
    monkeypatch.setattr(
        launcher,
        "run_job",
        lambda job, port, worker_id, model, agent: ("expired", 1),
    )
    monkeypatch.setattr(launcher, "_last_run_stats", {4: {}}, raising=False)
    monkeypatch.setattr(apply_worker_main, "_cdp_page_urls", lambda port: [])

    result = apply_worker_main.make_apply_fn("sonnet", "codex", slot=4)({
        "url": f"https://{host}/site/job/Role/JR1",
        "application_url": f"https://{host}/site/job/Role/JR1",
        "target_host": host,
        "session_required": True,
        "tenant_profile_id": session["profile_id"],
    })

    assert result["run_status"] == "expired"
    assert launch_kwargs[0]["profile_dir"] == session["profile_dir"]
