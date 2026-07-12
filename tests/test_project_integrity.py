from __future__ import annotations

import ast
import importlib
import pathlib
import py_compile
import tomllib

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_all_production_modules_compile() -> None:
    failures: list[str] = []
    for path in sorted((ROOT / "src" / "applypilot").rglob("*.py")):
        try:
            py_compile.compile(str(path), doraise=True)
        except py_compile.PyCompileError as exc:
            failures.append(f"{path.relative_to(ROOT)}: {exc.msg}")
    assert failures == []


def test_all_declared_scripts_resolve_to_callables() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    failures: list[str] = []
    for name, target in sorted(project["project"]["scripts"].items()):
        module_name, attribute_path = target.split(":", 1)
        try:
            value = importlib.import_module(module_name)
            for attribute in attribute_path.split("."):
                value = getattr(value, attribute)
            if not callable(value):
                failures.append(f"{name}: {target} is not callable")
        except Exception as exc:  # Report every broken script in one test run.
            failures.append(f"{name}: {target}: {exc!r}")
    assert failures == []


def test_ci_runs_automatically_with_bounded_permissions() -> None:
    workflow = yaml.load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    triggers = workflow["on"]
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["pull_request"]["branches"] == ["main"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] == "true"
    assert workflow["jobs"]["test"]["timeout-minutes"] == "30"


def test_fleet_workers_migrate_schema_before_building_loops() -> None:
    workers = {
        "apply_worker_main.py": ("main", "build_apply_loop"),
        "compute_worker_main.py": ("main", "build_compute_loop"),
        "discovery_main.py": ("main_worker", "build_discovery_loop"),
        "linkedin_worker_main.py": ("main", "build_linkedin_loop"),
    }
    for filename, (entrypoint, builder) in workers.items():
        path = ROOT / "src" / "applypilot" / "fleet" / filename
        tree = ast.parse(path.read_text(encoding="utf-8"))
        main = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == entrypoint
        )
        schema_lines = [
            node.lineno
            for node in ast.walk(main)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "ensure_schema_v3"
        ]
        loop_lines = [
            node.lineno
            for node in ast.walk(main)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == builder
        ]
        assert len(schema_lines) == 1, filename
        assert len(loop_lines) == 1, filename
        assert schema_lines[0] < loop_lines[0], filename


def test_primary_fleet_controllers_migrate_schema_on_startup() -> None:
    controllers = {
        "apply_home_main.py": "main",
        "linkedin_home_main.py": "main",
        "compute_home_main.py": "main",
        "discovery_main.py": "main_home",
        "watchdog.py": "main",
        "doctor.py": "main",
        "console_app.py": "main",
        "autotriage_main.py": "main",
        "dedup_repair_main.py": "main",
        "diagnoser_main.py": "main",
        "email_reconcile_main.py": "main",
        "historical_apply_reaudit_main.py": "main",
        "otp_responder_main.py": "main",
        "remediator_main.py": "main",
        "repair_report_main.py": "main",
    }
    for filename, entrypoint in controllers.items():
        path = ROOT / "src" / "applypilot" / "fleet" / filename
        tree = ast.parse(path.read_text(encoding="utf-8"))
        main = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == entrypoint
        )
        schema_calls = [
            node
            for node in ast.walk(main)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "ensure_schema_v3"
        ]
        assert len(schema_calls) == 1, filename
