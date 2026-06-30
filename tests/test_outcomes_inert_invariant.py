# tests/test_outcomes_inert_invariant.py
import ast
import inspect

import applypilot.outcome_implied as implied
import applypilot.outcome_lane_signal as lane
import applypilot.cli as cli


def _code_only(obj) -> str:
    """Source of a module/function with docstrings AND comments stripped, so a
    docstring that merely *mentions* a write API (e.g. as a 'deliberately NOT
    here' disclaimer) doesn't trip the no-write-path assertions."""
    tree = ast.parse(inspect.getsource(obj))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body
                and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)  # drop the docstring
    return ast.unparse(tree)  # ast.unparse also omits comments


def test_pure_modules_have_no_write_paths():
    """outcome_implied / outcome_lane_signal must never write the tracker, jobs, or scores."""
    for mod in (implied, lane):
        src = _code_only(mod)
        assert "record_application" not in src
        assert "INSERT" not in src.upper()
        assert "UPDATE " not in src.upper()
        assert "conn.commit" not in src


def test_outcomes_promote_is_preview_only_in_source():
    """The promote command must not expose --apply or write to applications."""
    src = _code_only(cli.outcomes_promote_command)
    assert "--apply" not in src
    assert "record_application" not in src
    assert "INSERT" not in src.upper()
    assert "UPDATE " not in src.upper()
