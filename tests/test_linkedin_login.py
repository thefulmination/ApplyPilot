"""LinkedIn login seed-profile helper.

The apply workers were logged OUT of LinkedIn (the cloned profile had only tracking
cookies -- li_rm etc. -- never the li_at auth session), so every LinkedIn job hit the
login wall. `applypilot linkedin-login` captures a one-time login into a dedicated seed
profile that the workers clone. These tests pin the session detector and the seed-clone
preference (the live login itself needs a human, so it's not unit-tested).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from applypilot.apply import chrome
from applypilot.apply import lifecycle_fault


@pytest.fixture(autouse=True)
def _isolate_lifecycle_faults(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle_fault.config, "DB_PATH", tmp_path / "applypilot.db")


def _make_cookies(profile_dir: Path, cookies: list[tuple[str, str]]) -> None:
    ck = profile_dir / "Default" / "Network" / "Cookies"
    ck.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(ck))
    con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
    con.executemany("INSERT INTO cookies VALUES (?, ?, X'00')", cookies)
    con.commit()
    con.close()


class TestHasLinkedInSession:
    def test_no_cookies_is_false(self, tmp_path):
        _make_cookies(tmp_path, [])
        assert chrome.has_linkedin_session(tmp_path) is False

    def test_li_at_present_is_true(self, tmp_path):
        _make_cookies(tmp_path, [(".www.linkedin.com", "li_at")])
        assert chrome.has_linkedin_session(tmp_path) is True

    def test_tracking_cookies_only_is_false(self, tmp_path):
        # The exact real failure: li_rm + analytics cookies but NO li_at = logged OUT.
        _make_cookies(tmp_path, [(".www.linkedin.com", "li_rm"),
                                 (".linkedin.com", "UserMatchHistory"),
                                 (".linkedin.com", "AnalyticsSyncHistory")])
        assert chrome.has_linkedin_session(tmp_path) is False

    def test_missing_profile_is_false(self, tmp_path):
        assert chrome.has_linkedin_session(tmp_path / "nope") is False

    def test_li_at_in_uncheckpointed_wal_is_detected(self, tmp_path):
        # Chrome's cookie DB is WAL-mode. A just-set li_at sits in the -wal until a
        # checkpoint; copying only the main file would miss it. Keep the connection OPEN
        # (no close-checkpoint) so the row stays in the -wal, and assert it's still found.
        ck = tmp_path / "Default" / "Network" / "Cookies"
        ck.parent.mkdir(parents=True)
        con = sqlite3.connect(str(ck))
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
        con.commit()
        con.execute("INSERT INTO cookies VALUES ('.www.linkedin.com', 'li_at', X'00')")
        con.commit()  # written to the -wal, NOT checkpointed (single row < threshold)
        try:
            assert chrome.has_linkedin_session(tmp_path) is True
        finally:
            con.close()


class TestLinkedInLoginPersistence:
    def test_cdp_cookie_alone_does_not_report_success(self, tmp_path, monkeypatch):
        from applypilot import config

        class FakeProc:
            pid = 12345
            alive = True
            _handle = 9001

            def poll(self):
                return None if self.alive else 0

            def wait(self, timeout=None):
                return 0 if not self.alive else None

        proc = FakeProc()
        monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
        monkeypatch.setattr(chrome.platform, "system", lambda: "Windows")
        monkeypatch.setattr(config, "CHROME_WORKER_DIR", tmp_path)
        monkeypatch.setattr(config, "resolve_browser_path", lambda browser: "chrome.exe")
        seed = tmp_path / chrome.SEED_PROFILE_NAME
        identity = chrome.BrowserProcessIdentity(
            pid=proc.pid,
            created_at=10.0,
            executable="chrome.exe",
            command=(
                f"chrome.exe --remote-debugging-port={chrome.LINKEDIN_LOGIN_CDP_PORT} "
                f'--user-data-dir="{seed}"'
            ),
            profile_dir=str(seed),
            port=chrome.LINKEDIN_LOGIN_CDP_PORT,
            parent_pid=50,
            parent_created_at=5.0,
            parent_executable="python.exe",
            parent_command="python applypilot-worker.py",
        )
        monkeypatch.setattr(chrome, "_assign_kill_on_close_job", lambda *args: True)
        monkeypatch.setattr(
            chrome,
            "_acquire_spawn_guard",
            lambda process: chrome._SpawnGuard(process, "windows", process._handle),
        )
        monkeypatch.setattr(chrome, "_resume_windows_process", lambda handle: True)
        monkeypatch.setattr(
            chrome,
            "_kill_process_tree",
            lambda process, expected, reservation: setattr(proc, "alive", False) or True,
        )
        monkeypatch.setattr(chrome, "_process_identity", lambda pid: identity)
        monkeypatch.setattr(chrome.subprocess, "Popen", lambda *args, **kwargs: proc)
        monkeypatch.setattr(chrome, "_has_linkedin_session_cdp", lambda port: True)
        monkeypatch.setattr(chrome, "_port_is_listening", lambda port: False)
        monkeypatch.setattr(chrome, "has_linkedin_session", lambda profile_dir: False)
        times = [0.0, 0.0, 11.0]
        monkeypatch.setattr(chrome.time, "monotonic", lambda: times.pop(0) if times else 11.0)
        monkeypatch.setattr(chrome.time, "sleep", lambda seconds: None)

        ok, seed = chrome.linkedin_login(timeout_seconds=10, poll_seconds=0)

        assert ok is False
        assert seed == tmp_path / chrome.SEED_PROFILE_NAME

    def test_cleanup_failure_overrides_success(self, tmp_path, monkeypatch):
        from applypilot import config

        class FakeProc:
            pid = 12345
            _handle = 9001

            def poll(self):
                return 0

        proc = FakeProc()
        seed = tmp_path / chrome.SEED_PROFILE_NAME
        monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
        monkeypatch.setattr(chrome.platform, "system", lambda: "Windows")
        monkeypatch.setattr(config, "CHROME_WORKER_DIR", tmp_path)
        monkeypatch.setattr(config, "resolve_browser_path", lambda _browser: "chrome.exe")
        monkeypatch.setattr(chrome, "_port_is_listening", lambda _port: False)
        monkeypatch.setattr(chrome.subprocess, "Popen", lambda *_args, **_kwargs: proc)
        monkeypatch.setattr(
            chrome,
            "_acquire_spawn_guard",
            lambda process: chrome._SpawnGuard(process, "windows", process._handle),
        )
        monkeypatch.setattr(chrome, "_record_launched_browser_identity", lambda *_args: None)
        monkeypatch.setattr(chrome, "_assign_kill_on_close_job", lambda *_args: True)
        monkeypatch.setattr(chrome, "_resume_windows_process", lambda _handle: True)
        monkeypatch.setattr(chrome, "has_linkedin_session", lambda _profile: True)
        monkeypatch.setattr(chrome, "cleanup_worker", lambda *_args: False)

        with pytest.raises(
            lifecycle_fault.LifecycleHardFault,
            match="cleanup could not be proven",
        ):
            chrome.linkedin_login(timeout_seconds=1, poll_seconds=0)

        assert seed.exists()
        assert len(lifecycle_fault.lifecycle_hard_fault_paths()) == 1

    def test_cleanup_failure_preserves_login_error_as_cause(self, tmp_path, monkeypatch):
        from applypilot import config

        class FakeProc:
            pid = 12345
            _handle = 9001

            def poll(self):
                return None

        proc = FakeProc()
        monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
        monkeypatch.setattr(chrome.platform, "system", lambda: "Windows")
        monkeypatch.setattr(config, "CHROME_WORKER_DIR", tmp_path)
        monkeypatch.setattr(config, "resolve_browser_path", lambda _browser: "chrome.exe")
        monkeypatch.setattr(chrome, "_port_is_listening", lambda _port: False)
        monkeypatch.setattr(chrome.subprocess, "Popen", lambda *_args, **_kwargs: proc)
        monkeypatch.setattr(
            chrome,
            "_acquire_spawn_guard",
            lambda process: chrome._SpawnGuard(process, "windows", process._handle),
        )
        monkeypatch.setattr(chrome, "_record_launched_browser_identity", lambda *_args: None)
        monkeypatch.setattr(chrome, "_assign_kill_on_close_job", lambda *_args: True)
        monkeypatch.setattr(chrome, "_resume_windows_process", lambda _handle: True)
        monkeypatch.setattr(
            chrome,
            "has_linkedin_session",
            lambda _profile: (_ for _ in ()).throw(ValueError("cookie read failed")),
        )
        monkeypatch.setattr(chrome, "cleanup_worker", lambda *_args: False)

        with pytest.raises(
            lifecycle_fault.LifecycleHardFault,
            match="cleanup could not be proven",
        ) as caught:
            chrome.linkedin_login(timeout_seconds=1, poll_seconds=0)

        assert isinstance(caught.value.__cause__, ValueError)
        assert str(caught.value.__cause__) == "cookie read failed"


class TestSeedClonePreference:
    def test_worker_clones_from_linkedin_seed(self, tmp_path, monkeypatch):
        from applypilot import config
        monkeypatch.setattr(config, "CHROME_WORKER_DIR", tmp_path)
        # a seed profile with a marker that proves the clone source
        seed_default = tmp_path / chrome.SEED_PROFILE_NAME / "Default"
        seed_default.mkdir(parents=True)
        (seed_default / "marker.txt").write_text("from-seed", encoding="utf-8")

        prof = chrome.setup_worker_profile(0, "chrome")
        assert (prof / "Default" / "marker.txt").read_text(encoding="utf-8") == "from-seed"

    def test_edge_worker_ignores_chrome_seed(self, tmp_path, monkeypatch):
        # The LinkedIn seed is a Chrome profile -- an edge worker must NOT clone it.
        from applypilot import config
        monkeypatch.setattr(config, "CHROME_WORKER_DIR", tmp_path)
        monkeypatch.setattr(config, "get_browser_user_data",
                            lambda b: tmp_path / "_real_edge")
        (tmp_path / "_real_edge" / "Default").mkdir(parents=True)
        (tmp_path / "_real_edge" / "Default" / "real.txt").write_text("edge", encoding="utf-8")
        (tmp_path / chrome.SEED_PROFILE_NAME / "Default").mkdir(parents=True)
        (tmp_path / chrome.SEED_PROFILE_NAME / "Default" / "marker.txt").write_text("seed", encoding="utf-8")

        prof = chrome.setup_worker_profile(0, "edge")
        assert (prof / "Default" / "real.txt").exists()          # cloned the edge real profile
        assert not (prof / "Default" / "marker.txt").exists()    # NOT the chrome seed
