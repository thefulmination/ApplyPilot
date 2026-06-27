# tests/test_codex_bridge.py
import os
import importlib
import pytest
from applypilot.apply import pgqueue


def test_module_imports_without_dsn(monkeypatch):
    # No DB access at import time: importing with FLEET_PG_DSN unset must not raise.
    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    mod = importlib.import_module("applypilot.fleet.codex_bridge")
    importlib.reload(mod)
    assert hasattr(mod, "mcp") and hasattr(mod, "main") and hasattr(mod, "_with_conn")


def test_with_conn_errors_when_dsn_unset(monkeypatch):
    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    from applypilot.fleet import codex_bridge
    out = codex_bridge._with_conn(lambda conn: {"ok": True})
    assert "error" in out and "FLEET_PG_DSN" in out["error"]


def test_with_conn_errors_on_unreachable_db(monkeypatch):
    # A syntactically-valid but dead DSN returns a structured error, not a raise.
    monkeypatch.setenv("FLEET_PG_DSN", "postgresql://postgres@127.0.0.1:1/postgres?connect_timeout=1")
    from applypilot.fleet import codex_bridge
    out = codex_bridge._with_conn(lambda conn: {"ok": True})
    assert "error" in out


def test_with_conn_runs_fn_and_closes(fleet_db, monkeypatch):
    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    from applypilot.fleet import codex_bridge
    out = codex_bridge._with_conn(lambda conn: {"ok": True})
    assert out == {"ok": True}
