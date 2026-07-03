"""Tests for Task 4: the console surfaces fleet_config.deadman_alert as a RED banner
so a silent fleet death / stall / running-hot condition (written by run_deadman,
applypilot.fleet.deadman) is visible on the phone-facing status page.

Uses the shared ``fleet_db`` fixture (disposable local Postgres, v3 schema applied)
and the live-server pattern from test_console_challenges_api.py / test_console_token.py.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import console_app


def test_build_status_includes_deadman_alert_when_set(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_config SET deadman_alert=%s, deadman_alert_at=now() WHERE id=1",
                ("silent_death: no apply-worker heartbeat",),
            )
        conn.commit()

    status = console_app.build_status()

    assert status["deadman_alert"] == "silent_death: no apply-worker heartbeat"
    assert status["deadman_alert_at"] is not None


def test_build_status_deadman_alert_null_when_healthy(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    status = console_app.build_status()
    assert status["deadman_alert"] is None
    assert status["deadman_alert_at"] is None


def test_status_route_returns_deadman_alert_json(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_config SET deadman_alert=%s, deadman_alert_at=now() WHERE id=1",
                ("running_hot: apply rate 3x baseline",),
            )
        conn.commit()

    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "tok-deadman")

    server = ThreadingHTTPServer(("127.0.0.1", 0), console_app._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body["deadman_alert"] == "running_hot: apply rate 3x baseline"
            assert body["deadman_alert_at"] is not None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_index_html_contains_deadman_banner_element_and_js_wiring(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "tok-deadman-html")

    server = ThreadingHTTPServer(("127.0.0.1", 0), console_app._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as resp:
            assert resp.status == 200
            html = resp.read().decode("utf-8")
            assert 'id="deadmanBanner"' in html
            assert "deadman_alert" in html
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
