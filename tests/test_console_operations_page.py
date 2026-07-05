from __future__ import annotations

import threading
import urllib.request
import re
from http.server import ThreadingHTTPServer

import pytest

from applypilot.fleet import console_app


@pytest.fixture()
def live_server(monkeypatch):
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "tok-ops")
    monkeypatch.setattr(
        console_app,
        "build_status",
        lambda: {
            "now": "2026-07-05T00:00:00+00:00",
            "gate": {
                "paused": False,
                "should_halt": False,
                "leasable": 0,
                "spent_usd": 0,
                "spend_cap_usd": 0,
            },
            "queue": {"apply": {"queued": 0}},
            "workers": [],
            "recent": [],
            "challenges": 0,
            "linkedin": {
                "queued": 0,
                "applied": 0,
                "canary_enabled": False,
                "halted": False,
            },
            "doctor": None,
            "discovery": None,
            "deadman_alert": None,
            "deadman_alert_at": None,
            "fleet_diagnosis": {
                "state": {
                    "code": "idle_no_leasable_jobs",
                    "reason": "No leaseable ATS jobs are available.",
                }
            },
        },
    )

    server = ThreadingHTTPServer(("127.0.0.1", 0), console_app._Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


def test_index_contains_operations_sections(live_server):
    with urllib.request.urlopen(f"{live_server}/") as resp:
        html = resp.read().decode("utf-8")

    for text in [
        "Fleet State",
        "Why Not Applying",
        "Agent Routing",
        "Machine Health",
        "Browser Health",
        "Queue Funnel",
        "Safety Rails",
        "Recommended Next Action",
        "Action Queue",
        "Audit Log",
    ]:
        assert text in html

    assert "/api/diagnosis" in html
    assert "/api/agents" in html
    assert "/api/audit" in html
    assert "async function loadAudit()" in html
    assert 'fetch("/api/audit", {cache:"no-store"})' in html
    assert "renderAudit(await r.json())" in html


def test_agent_routing_table_is_responsively_contained(live_server):
    with urllib.request.urlopen(f"{live_server}/") as resp:
        html = resp.read().decode("utf-8")

    assert ".table-scroll" in html
    assert "overflow-x:auto" in html
    assert "overflow-wrap:anywhere" in html
    assert 'id="agentRouting"' in html
    assert '<div class="table-scroll"><table><thead><tr><th>Worker</th><th>Machine</th><th>Agent</th><th>Model</th><th>Chain</th><th>Version</th><th>Switch</th></tr></thead>' in html
    assert html.count("<table") == len(re.findall(r'class="table-scroll"[^>]*><table', html))


def test_favicon_does_not_emit_browser_404(live_server):
    with urllib.request.urlopen(f"{live_server}/favicon.ico") as resp:
        assert resp.status == 204
        assert resp.read() == b""


def test_dashboard_uses_friendly_machine_names(live_server):
    with urllib.request.urlopen(f"{live_server}/") as resp:
        html = resp.read().decode("utf-8")

    assert "machine_display_name" in html
    assert "w.machine_display_name" in html
    assert "machines[k].display_name" in html


def test_dashboard_surfaces_versions_browser_examples_and_worker_comparison(live_server):
    with urllib.request.urlopen(f"{live_server}/") as resp:
        html = resp.read().decode("utf-8")

    assert 'id="deploymentMeta"' in html
    assert 'id="workerComparisonRows"' in html
    assert "renderWorkerComparison" in html
    assert "renderRecommendationList" in html
    assert 'id="recommendationList"' in html
    assert "worker_versions" in html
    assert "browser.examples" in html
    assert "logs_url" in html
    assert 'id="browserWallQueue"' in html
    assert "renderBrowserWallQueue" in html
    assert "wall_queue" in html


def test_dashboard_has_explicit_lane_state_safety_rails(live_server):
    with urllib.request.urlopen(f"{live_server}/") as resp:
        html = resp.read().decode("utf-8")

    assert "Lane State" in html
    assert 'id="laneStateGrid"' in html
    assert "renderLaneState" in html
    assert "Shared pause" in html
    assert "ATS pause" in html
    assert "ATS leaseable" in html
    assert "LinkedIn owner IP" in html
    assert "LinkedIn canary" in html


def test_dashboard_uses_lane_gated_leaseable_count(live_server):
    with urllib.request.urlopen(f"{live_server}/") as resp:
        html = resp.read().decode("utf-8")

    assert "leaseable after pause/canary gates" in html
    assert "liveAts.leaseable" in html


def test_browser_wall_log_links_work_before_worker_dropdown_populates(live_server):
    with urllib.request.urlopen(f"{live_server}/") as resp:
        html = resp.read().decode("utf-8")

    assert "document.createElement(\"option\")" in html
    assert "sel.appendChild(opt)" in html
