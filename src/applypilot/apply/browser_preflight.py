"""Zero-model browser and Playwright MCP readiness checks."""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import urllib.request

MCP_PACKAGE = "@playwright/mcp@0.0.76"
_cache_lock = threading.Lock()
_mcp_cache: tuple[float, tuple[bool, str]] | None = None


def _check_cdp_http(port: int, timeout: float) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        if not payload.get("webSocketDebuggerUrl"):
            return False, "cdp_missing_websocket"
        return True, "cdp_http_ready"
    except Exception as exc:
        return False, f"cdp_http:{type(exc).__name__}"


def _check_playwright_cdp(port: int, timeout: float) -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}",
                timeout=max(1, int(timeout * 1000)),
            )
            if not browser.contexts:
                browser.close()
                return False, "playwright_no_context"
            browser.close()
        return True, "playwright_cdp_ready"
    except Exception as exc:
        return False, f"playwright_cdp:{type(exc).__name__}"


def _check_mcp_package(timeout: float, *, cache_seconds: float = 300) -> tuple[bool, str]:
    global _mcp_cache
    now = time.monotonic()
    with _cache_lock:
        if _mcp_cache and now - _mcp_cache[0] < cache_seconds:
            return _mcp_cache[1]

    npx = shutil.which("npx")
    if not npx:
        result = (False, "mcp_npx_missing")
    else:
        try:
            proc = subprocess.run(
                [npx, "--offline", "--yes", MCP_PACKAGE, "--help"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            result = (
                (True, "mcp_package_ready")
                if proc.returncode == 0
                else (False, f"mcp_package_exit_{proc.returncode}")
            )
        except subprocess.TimeoutExpired:
            result = (False, "mcp_package_timeout")
        except OSError as exc:
            result = (False, f"mcp_package:{type(exc).__name__}")

    with _cache_lock:
        _mcp_cache = (now, result)
    return result


def check_browser_readiness(port: int, *, timeout: float = 5.0) -> dict:
    """Return structured readiness evidence without invoking an LLM."""
    checks = []
    for name, check in (
        ("cdp_http", lambda: _check_cdp_http(port, timeout)),
        ("playwright_cdp", lambda: _check_playwright_cdp(port, timeout)),
        ("playwright_mcp", lambda: _check_mcp_package(max(timeout, 10.0))),
    ):
        ok, reason = check()
        checks.append({"check": name, "ok": ok, "reason": reason})
        if not ok:
            return {"ready": False, "reason": reason, "checks": checks}
    return {"ready": True, "reason": "ready", "checks": checks}


def clear_readiness_cache() -> None:
    """Force MCP package re-resolution before the single recovery attempt."""
    global _mcp_cache
    with _cache_lock:
        _mcp_cache = None
