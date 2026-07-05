from pathlib import Path

from applypilot.fleet.software_version import GitIdentity, build_sw_version, git_identity
from applypilot.fleet.worker import WorkerLoop


def test_build_sw_version_formats_clean_git_identity() -> None:
    ident = GitIdentity(
        package_version="0.3.0",
        branch="codex/fleet-applier-hardening",
        commit="53a4fa9e6c2a1b0d",
        dirty=False,
        git_available=True,
    )

    assert build_sw_version(ident) == "0.3.0+git.codex-fleet-applier-hardening.53a4fa9"


def test_build_sw_version_marks_dirty_tree() -> None:
    ident = GitIdentity(
        package_version="0.3.0",
        branch="main",
        commit="abcdef0123456789",
        dirty=True,
        git_available=True,
    )

    assert build_sw_version(ident) == "0.3.0+git.main.abcdef0.dirty"


def test_build_sw_version_falls_back_when_git_unavailable() -> None:
    ident = GitIdentity(
        package_version="0.3.0",
        branch=None,
        commit=None,
        dirty=False,
        git_available=False,
    )

    assert build_sw_version(ident) == "0.3.0+git.unavailable"


def test_git_identity_uses_git_commands_without_touching_live_repo() -> None:
    calls: list[tuple[str, ...]] = []

    def fake_runner(args: list[str], cwd: Path) -> str:
        calls.append(tuple(args))
        if args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return "codex/fleet-applier-hardening\n"
        if args == ["git", "rev-parse", "HEAD"]:
            return "53a4fa9e6c2a1b0d\n"
        if args == ["git", "status", "--porcelain"]:
            return " M fleet-agent.ps1\n"
        raise AssertionError(f"unexpected git command: {args}")

    ident = git_identity(repo=Path("C:/ApplyPilot"), package_version="0.3.0", runner=fake_runner)

    assert ident == GitIdentity(
        package_version="0.3.0",
        branch="codex/fleet-applier-hardening",
        commit="53a4fa9e6c2a1b0d",
        dirty=True,
        git_available=True,
    )
    assert calls == [
        ("git", "rev-parse", "--abbrev-ref", "HEAD"),
        ("git", "rev-parse", "HEAD"),
        ("git", "status", "--porcelain"),
    ]


def test_git_identity_reports_unavailable_when_git_command_fails() -> None:
    def failing_runner(args: list[str], cwd: Path) -> str:
        raise OSError("git missing")

    ident = git_identity(repo=Path("C:/ApplyPilot"), package_version="0.3.0", runner=failing_runner)

    assert ident.git_available is False
    assert build_sw_version(ident) == "0.3.0+git.unavailable"


def test_worker_loop_defaults_to_current_software_version(monkeypatch) -> None:
    from applypilot.fleet import software_version

    monkeypatch.setattr(software_version, "current_sw_version", lambda: "0.3.0+git.main.abc1234")

    loop = WorkerLoop(
        lambda: None,
        "w-version",
        home_ip="1.2.3.4",
        role="compute",
        score_fn=lambda job: ({}, 0),
    )

    assert loop.sw_version == "0.3.0+git.main.abc1234"
