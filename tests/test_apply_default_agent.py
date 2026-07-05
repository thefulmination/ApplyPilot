"""The default apply agent is Codex, not Claude.

Flipping the default moves fleet + CLI apply runs onto the ChatGPT (Codex)
quota pool by default, off the Claude Max subscription. Callers can still pass
--agent claude explicitly, and APPLYPILOT_FALLBACK_AGENT / --fallback-agent
control spillover.
"""

import inspect

from applypilot import cli
from applypilot.fleet.apply_worker_main import build_parser


def test_fleet_apply_worker_defaults_to_codex():
    args = build_parser().parse_args(["--worker-id", "w0"])
    assert args.agent == "codex"


def test_cli_apply_defaults_to_codex():
    default = inspect.signature(cli.apply).parameters["agent"].default
    # typer wraps option defaults in an OptionInfo whose .default holds the value
    assert getattr(default, "default", default) == "codex"
