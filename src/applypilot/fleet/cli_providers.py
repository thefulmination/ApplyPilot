"""Subscription-CLI scoring backends (Flavor B). score_via_codex shells out to the
user's logged-in Codex (ChatGPT subscription) on the home box. Holds no token.
On any limit/auth/parse failure it raises SubscriptionUnavailable so the caller
(frontier_pass) can fail over to a metered API."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


class SubscriptionUnavailable(Exception):
    """The subscription CLI couldn't produce a usable score (limit/auth/exit/parse)."""


def score_via_codex(prompt, *, schema_path, model=None, timeout_s=120, retries=2, _runner=subprocess.run) -> dict:
    last = None
    for _ in range(max(1, retries)):
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "last.txt")
            argv = ["codex", "exec"]
            if model:
                argv += ["-m", model]
            argv += ["--output-schema", schema_path, "-o", out, prompt]
            try:
                proc = _runner(argv, capture_output=True, text=True, timeout=timeout_s)
            except FileNotFoundError as e:  # codex not on PATH
                raise SubscriptionUnavailable(
                    "codex CLI not found on PATH -- is Codex installed/logged in? Falling back to metered."
                ) from e
            except Exception as e:  # transport / timeout
                raise SubscriptionUnavailable(f"codex exec failed: {e}") from e
            if getattr(proc, "returncode", 1) != 0:
                raise SubscriptionUnavailable(f"codex exec exit {getattr(proc, 'returncode', '?')}: {getattr(proc, 'stderr', '')[:200]}")
            try:
                text = Path(out).read_text(encoding="utf-8")
                data = json.loads(text)
                if "score" in data:
                    return data
                last = "no score key"
            except (ValueError, OSError) as e:
                last = str(e)
        prompt = prompt + "\n\nReturn ONLY the JSON object conforming to the schema."
    raise SubscriptionUnavailable(f"codex exec produced no valid score after retries: {last}")
