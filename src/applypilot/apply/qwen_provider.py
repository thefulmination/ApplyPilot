"""Disabled-by-default local Qwen3 answer provider."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import time
import urllib.request

DEFAULT_MODEL = "qwen3:8b"
DEFAULT_URL = "http://127.0.0.1:11434/api/chat"


@dataclass(frozen=True)
class LocalAnswerResult:
    text: str
    verified: bool
    checks: tuple[str, ...]
    model: str
    latency_ms: int
    error: str | None


def enabled() -> bool:
    return os.environ.get("APPLYPILOT_LOCAL_QWEN_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _default_transport(payload: dict, timeout: float) -> dict:
    request = urllib.request.Request(
        os.environ.get("APPLYPILOT_LOCAL_QWEN_URL") or DEFAULT_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _content(response: dict) -> str:
    text = str((response.get("message") or {}).get("content") or response.get("response") or "")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.I | re.S).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def answer_locally(
    *,
    system_prompt: str,
    user_prompt: str,
    verify,
    transport=None,
    timeout_seconds: float | None = None,
    model: str | None = None,
) -> LocalAnswerResult:
    """Make one local call and apply the caller's deterministic verifier."""
    transport = transport or _default_transport
    model = model or os.environ.get("APPLYPILOT_LOCAL_QWEN_MODEL") or DEFAULT_MODEL
    timeout = float(timeout_seconds or os.environ.get("APPLYPILOT_LOCAL_QWEN_TIMEOUT") or 20)
    started = time.monotonic()
    try:
        response = transport(
            {
                "model": model,
                "stream": False,
                "think": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "options": {"temperature": 0.2, "num_predict": 400},
            },
            timeout,
        )
        text = _content(response or {})
        checks = tuple(verify(text))
        return LocalAnswerResult(
            text=text if not checks else "",
            verified=not checks,
            checks=checks,
            model=model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=None,
        )
    except Exception as exc:
        return LocalAnswerResult(
            text="",
            verified=False,
            checks=("local_provider_error",),
            model=model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}:{str(exc)[:160]}",
        )
