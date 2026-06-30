import json
import subprocess
import pytest
from applypilot.fleet import cli_providers as clp


class _FakeProc:
    def __init__(self, returncode=0): self.returncode = returncode; self.stdout = ""; self.stderr = ""


def _runner_writing(obj, returncode=0):
    def run(argv, **kw):
        # find the -o output file and write the (maybe malformed) content
        out = argv[argv.index("-o") + 1]
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj))
        return _FakeProc(returncode)
    return run


def test_score_via_codex_parses_and_passes_flags(tmp_path):
    seen = {}
    def run(argv, **kw):
        seen["argv"] = argv
        with open(argv[argv.index("-o") + 1], "w", encoding="utf-8") as fh:
            fh.write('{"score": 8, "reasoning": "fit"}')
        return _FakeProc(0)
    out = clp.score_via_codex("PROMPT", schema_path=str(tmp_path / "s.json"), model="gpt-5.5", _runner=run)
    assert out["score"] == 8
    a = seen["argv"]
    assert a[:2] == ["codex", "exec"] and "-m" in a and "gpt-5.5" in a
    assert "--output-schema" in a and "-o" in a and "--json" not in a


def test_score_via_codex_retries_then_raises_on_malformed(tmp_path):
    out = _runner_writing("not json", 0)
    with pytest.raises(clp.SubscriptionUnavailable):
        clp.score_via_codex("P", schema_path=str(tmp_path / "s.json"), retries=2, _runner=out)


def test_score_via_codex_raises_on_nonzero_exit(tmp_path):
    with pytest.raises(clp.SubscriptionUnavailable):
        clp.score_via_codex("P", schema_path=str(tmp_path / "s.json"), _runner=_runner_writing({}, returncode=1))


def test_score_via_codex_file_not_found_raises_clear_message(tmp_path):
    """Finding #2: missing codex binary should give a named, actionable error message."""
    def _missing(argv, **kw):
        raise FileNotFoundError(2, "No such file or directory", "codex")
    with pytest.raises(clp.SubscriptionUnavailable, match="codex CLI not found on PATH"):
        clp.score_via_codex("P", schema_path=str(tmp_path / "s.json"), _runner=_missing)
