from __future__ import annotations

from applypilot.apply import pgqueue
from applypilot.fleet import queue
from applypilot.fleet.tenant_router import route_tenant


def test_trusted_ready_supported_tenant_routes_deterministic():
    decision = route_tenant(
        tenant_status="trusted",
        session_state="ready",
        adapter_supported=True,
    )
    assert decision.route == "deterministic"
    assert decision.submit_allowed is True


def test_supervised_tenant_routes_review_without_unattended_submit():
    decision = route_tenant(
        tenant_status="supervised",
        session_state="ready",
        adapter_supported=True,
    )
    assert decision.route == "supervised_review"
    assert decision.submit_allowed is False


def test_excluded_halted_unready_or_unsupported_tenants_route_exception():
    assert route_tenant(
        tenant_status="excluded", session_state="ready", adapter_supported=True
    ).route == "exception"
    assert route_tenant(
        tenant_status="trusted", session_state="ready", adapter_supported=True, halted=True
    ).reason == "tenant_halted"
    assert route_tenant(
        tenant_status="trusted", session_state="expired", adapter_supported=True
    ).reason == "session_expired"
    assert route_tenant(
        tenant_status="trusted", session_state="ready", adapter_supported=False
    ).reason == "adapter_unsupported"


def test_unregistered_unsupported_tenant_routes_exception_under_common_framework():
    decision = route_tenant(
        tenant_status=None,
        session_state=None,
        adapter_supported=False,
    )
    assert decision.route == "exception"
    assert decision.reason == "adapter_unsupported"
    assert decision.routing_required is True


def test_unregistered_supported_host_can_route_deterministic():
    decision = route_tenant(
        tenant_status=None, session_state=None, adapter_supported=True,
    )
    assert decision.route == "deterministic"
    assert decision.reason == "adapter_ready"


def _push(conn, url, *, route):
    return queue.push_apply_jobs(
        conn,
        [{
            "url": url,
            "company": "Acme",
            "title": "Role",
            "application_url": url,
            "score": 9.0,
            "target_host": "acme.wd5.myworkdayjobs.com",
            "routing_required": True,
            "execution_route": route,
            "host_policy": f"test_{route}",
        }],
        approved_batch="route-batch",
    )


def test_supervised_route_cannot_enter_unattended_lease(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _push(conn, "https://example.com/supervised", route="supervised_review")
        assert queue.lease_apply(conn, "worker", home_ip="1.2.3.4") is None
        with conn.cursor() as cur:
            cur.execute("SELECT status, execution_route FROM apply_queue")
            row = cur.fetchone()
    assert row["status"] == "queued"
    assert row["execution_route"] == "supervised_review"


def test_deterministic_route_can_lease_and_exception_is_blocked(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        deterministic_url = "https://example.com/deterministic"
        exception_url = "https://example.com/exception"
        _push(conn, deterministic_url, route="deterministic")
        _push(conn, exception_url, route="exception")
        job = queue.lease_apply(conn, "worker", home_ip="1.2.3.4")
        assert job["url"] == deterministic_url
        assert job["execution_route"] == "deterministic"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, apply_status, apply_error FROM apply_queue WHERE url=%s",
                (exception_url,),
            )
            row = cur.fetchone()
    assert row["status"] == "blocked"
    assert row["apply_status"] == "exception_pending"
    assert row["apply_error"] == "test_exception"
