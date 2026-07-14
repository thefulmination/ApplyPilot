from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
import importlib
import pathlib
import py_compile
import tomllib

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _compile_production_modules(
    source_root: pathlib.Path,
    output_root: pathlib.Path,
    *,
    report_root: pathlib.Path | None = None,
) -> list[str]:
    failures: list[str] = []
    report_root = source_root if report_root is None else report_root
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root)
        cfile = output_root / relative.with_suffix(".pyc")
        try:
            cfile.parent.mkdir(parents=True, exist_ok=True)
            py_compile.compile(str(path), cfile=str(cfile), doraise=True)
        except py_compile.PyCompileError as exc:
            failures.append(f"{path.relative_to(report_root)}: {exc.msg}")
        except OSError as exc:
            failures.append(f"{path.relative_to(report_root)}: {exc}")
    return failures


def test_all_production_modules_compile(tmp_path: pathlib.Path) -> None:
    failures = _compile_production_modules(
        ROOT / "src" / "applypilot",
        tmp_path / "bytecode",
        report_root=ROOT,
    )
    assert failures == []


def test_compile_outputs_are_source_isolated_and_concurrent(tmp_path: pathlib.Path) -> None:
    source_root = tmp_path / "project" / "src" / "applypilot"
    modules = {
        "__init__.py": "VALUE = 1\n",
        "providers/qwen_provider.py": "def load():\n    return 'qwen'\n",
    }
    for relative, source in modules.items():
        path = source_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    sentinel = source_root / "providers" / "__pycache__" / "qwen_provider.cpython-312.pyc"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b"existing-bytecode")
    source_bytecode_before = {
        path.relative_to(source_root): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in source_root.rglob("*.pyc")
    }

    output_roots = [tmp_path / "compiled" / f"run-{index}" for index in range(8)]
    with ThreadPoolExecutor(max_workers=len(output_roots)) as executor:
        results = list(
            executor.map(
                lambda output_root: _compile_production_modules(source_root, output_root),
                output_roots,
            )
        )

    assert results == [[] for _ in output_roots]
    expected_outputs = {pathlib.Path("__init__.pyc"), pathlib.Path("providers/qwen_provider.pyc")}
    for output_root in output_roots:
        assert {path.relative_to(output_root) for path in output_root.rglob("*.pyc")} == expected_outputs

    (source_root / "broken.py").write_text("def broken(:\n", encoding="utf-8")
    (source_root / "providers" / "also_broken.py").write_text("if True print('broken')\n", encoding="utf-8")
    failures = _compile_production_modules(source_root, tmp_path / "compiled" / "broken")
    assert len(failures) == 2
    assert [failure.split(": ", 1)[0] for failure in failures] == [
        "broken.py",
        str(pathlib.Path("providers") / "also_broken.py"),
    ]
    assert {
        path.relative_to(source_root): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in source_root.rglob("*.pyc")
    } == source_bytecode_before


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
    remote_workers = {
        "apply_worker_main.py": ("main", "build_apply_loop"),
        "linkedin_worker_main.py": ("main", "build_linkedin_loop"),
        "compute_worker_main.py": ("main", "build_compute_loop"),
        "discovery_main.py": ("main_worker", "build_discovery_loop"),
    }
    for filename, (entrypoint, builder) in remote_workers.items():
        path = ROOT / "src" / "applypilot" / "fleet" / filename
        tree = ast.parse(path.read_text(encoding="utf-8"))
        main = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == entrypoint
        )
        ddl_lines = [
            node.lineno
            for node in ast.walk(main)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "ensure_schema_v3"
        ]
        required_schema_lines = {
            name: [
                node.lineno
                for node in ast.walk(main)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == name
            ]
            for name in (
                "require_apply_result_event_schema",
                "require_apply_attempt_schema",
            )
        }
        loop_lines = [
            node.lineno
            for node in ast.walk(main)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == builder
        ]
        assert ddl_lines == [], filename
        assert all(len(lines) == 1 for lines in required_schema_lines.values()), filename
        assert len(loop_lines) == 1, filename
        assert all(lines[0] < loop_lines[0] for lines in required_schema_lines.values()), filename

def test_primary_fleet_controllers_migrate_schema_on_startup() -> None:
    controllers = {
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


def test_apply_status_controllers_use_read_only_schema_verification() -> None:
    for filename in ("apply_home_main.py", "linkedin_home_main.py"):
        path = ROOT / "src" / "applypilot" / "fleet" / filename
        tree = ast.parse(path.read_text(encoding="utf-8"))
        main = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        schema_calls = [
            node.func.attr
            for node in ast.walk(main)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"ensure_schema_v3", "_verify_schema_v3"}
        ]
        assert schema_calls == ["_verify_schema_v3"], filename
