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
        tree="8e72467aabbccdde",
    )

    assert build_sw_version(ident) == "0.3.0+git.tree.8e72467"


def test_build_sw_version_marks_dirty_tree() -> None:
    ident = GitIdentity(
        package_version="0.3.0",
        branch="main",
        commit="abcdef0123456789",
        dirty=True,
        git_available=True,
        tree="baddad0123456789",
    )

    assert build_sw_version(ident) == "0.3.0+git.tree.baddad0.dirty"


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
        if args == ["git", "rev-parse", "HEAD^{tree}"]:
            return "8e72467aabbccdde\n"
        if args == ["git", "status", "--porcelain", "--untracked-files=no"]:
            return " M fleet-agent.ps1\n"
        raise AssertionError(f"unexpected git command: {args}")

    ident = git_identity(repo=Path("C:/ApplyPilot"), package_version="0.3.0", runner=fake_runner)

    assert ident == GitIdentity(
        package_version="0.3.0",
        branch="codex/fleet-applier-hardening",
        commit="53a4fa9e6c2a1b0d",
        dirty=True,
        git_available=True,
        tree="8e72467aabbccdde",
    )
    assert calls == [
        ("git", "rev-parse", "--abbrev-ref", "HEAD"),
        ("git", "rev-parse", "HEAD"),
        ("git", "rev-parse", "HEAD^{tree}"),
        ("git", "status", "--porcelain", "--untracked-files=no"),
    ]


def test_git_identity_ignores_untracked_files_for_dirty_marker() -> None:
    def fake_runner(args: list[str], cwd: Path) -> str:
        if args == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return "codex/fleet-applier-hardening\n"
        if args == ["git", "rev-parse", "HEAD"]:
            return "53a4fa9e6c2a1b0d\n"
        if args == ["git", "rev-parse", "HEAD^{tree}"]:
            return "8e72467aabbccdde\n"
        if args == ["git", "status", "--porcelain", "--untracked-files=no"]:
            return ""
        raise AssertionError(f"unexpected git command: {args}")

    ident = git_identity(repo=Path("C:/ApplyPilot"), package_version="0.3.0", runner=fake_runner)

    assert ident.dirty is False
    assert build_sw_version(ident) == "0.3.0+git.tree.8e72467"


def test_git_identity_reports_unavailable_when_git_command_fails() -> None:
    def failing_runner(args: list[str], cwd: Path) -> str:
        raise OSError("git missing")

    ident = git_identity(repo=Path("C:/ApplyPilot"), package_version="0.3.0", runner=failing_runner)

    assert ident.git_available is False
    assert build_sw_version(ident) == "0.3.0+git.unavailable"


def test_worker_loop_defaults_to_current_software_version(monkeypatch) -> None:
    from applypilot.fleet import software_version

    monkeypatch.setattr(software_version, "current_sw_version", lambda: "0.3.0+git.tree.abc1234")

    loop = WorkerLoop(
        lambda: None,
        "w-version",
        home_ip="1.2.3.4",
        role="compute",
        score_fn=lambda job: ({}, 0),
    )

    assert loop.sw_version == "0.3.0+git.tree.abc1234"
