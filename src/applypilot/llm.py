"""
Unified LLM client for ApplyPilot.

Auto-detects provider from environment:
  GEMINI_API_KEY    -> Google Gemini (default: gemini-2.0-flash)
  DEEPSEEK_API_KEY  -> DeepSeek (when explicitly requested or model starts deepseek-)
  OPENAI_API_KEY    -> OpenAI (default: gpt-4o-mini)
  LLM_URL           -> Local llama.cpp / Ollama compatible endpoint

LLM_MODEL env var overrides the model name for any provider.
"""

import logging
import os
import time

import httpx

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Provider detection
# ---------------------------------------------------------------------------

_DEEPSEEK_BASE = "https://api.deepseek.com"


def _clean_provider(provider: str | None) -> str:
    return (provider or "").strip().lower().replace("_", "-")


def _detect_provider(
    model_override: str | None = None,
    provider_override: str | None = None,
) -> tuple[str, str, str]:
    """Return (base_url, model, api_key) based on environment variables.

    Reads env at call time (not module import time) so that load_env() called
    in _bootstrap() is always visible here.
    """
    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    local_url = os.environ.get("LLM_URL", "")
    model_override = model_override or os.environ.get("LLM_MODEL", "")
    provider = _clean_provider(provider_override or os.environ.get("LLM_PROVIDER", ""))

    if not provider and model_override.lower().startswith("deepseek-"):
        provider = "deepseek"

    if provider in {"deepseek", "deepseek-api"}:
        if not deepseek_key:
            raise RuntimeError("DEEPSEEK_API_KEY is required for DeepSeek models.")
        return (
            os.environ.get("DEEPSEEK_BASE_URL", _DEEPSEEK_BASE).rstrip("/"),
            model_override or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            deepseek_key,
        )

    if provider == "gemini":
        if not gemini_key:
            raise RuntimeError("GEMINI_API_KEY is required for Gemini models.")
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            model_override or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
            gemini_key,
        )

    if provider == "openai":
        if not openai_key:
            raise RuntimeError("OPENAI_API_KEY is required for OpenAI models.")
        return (
            "https://api.openai.com/v1",
            model_override or os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            openai_key,
        )

    if provider == "local":
        if not local_url:
            raise RuntimeError("LLM_URL is required for local LLM provider.")
        return (
            local_url.rstrip("/"),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )

    if gemini_key and not local_url:
        return (
            "https://generativelanguage.googleapis.com/v1beta/openai",
            model_override or "gemini-2.0-flash",
            gemini_key,
        )

    if openai_key and not local_url:
        return (
            "https://api.openai.com/v1",
            model_override or "gpt-4o-mini",
            openai_key,
        )

    if deepseek_key and not local_url:
        return (
            os.environ.get("DEEPSEEK_BASE_URL", _DEEPSEEK_BASE).rstrip("/"),
            model_override or os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            deepseek_key,
        )

    if local_url:
        return (
            local_url.rstrip("/"),
            model_override or "local-model",
            os.environ.get("LLM_API_KEY", ""),
        )

    raise RuntimeError(
        "No LLM provider configured. "
        "Set GEMINI_API_KEY, DEEPSEEK_API_KEY, OPENAI_API_KEY, or LLM_URL in your environment."
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_MAX_RETRIES = 5
_TIMEOUT = 120  # seconds

# Base wait on first 429/503 (doubles each retry, caps at 60s).
# Keep the first retry long enough for provider-side quota windows to recover.
_RATE_LIMIT_BASE_WAIT = 10


_GEMINI_COMPAT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
_GEMINI_NATIVE_BASE = "https://generativelanguage.googleapis.com/v1beta"


class LLMClient:
    """Thin LLM client supporting OpenAI-compatible and native Gemini endpoints.

    For Gemini keys, starts on the OpenAI-compat layer. On a 403 (which
    happens with preview/experimental models not exposed via compat), it
    automatically switches to the native generateContent API and stays there
    for the lifetime of the process.
    """

    def __init__(self, base_url: str, model: str, api_key: str) -> None:
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self._client = httpx.Client(timeout=_TIMEOUT)
        # True once we've confirmed the native Gemini API works for this model
        self._use_native_gemini: bool = False
        self._is_gemini: bool = base_url.startswith(_GEMINI_COMPAT_BASE)
        self._is_deepseek: bool = base_url.startswith(_DEEPSEEK_BASE)
        self.last_usage: dict | None = None

    @property
    def provider_name(self) -> str:
        """Human-readable provider name for job-level model metadata."""
        if self._is_gemini:
            return "gemini"
        if self._is_deepseek:
            return "deepseek"
        if self.base_url.startswith("https://api.openai.com"):
            return "openai"
        return "local"

    # -- Native Gemini API --------------------------------------------------

    def _chat_native_gemini(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the native Gemini generateContent API.

        Used automatically when the OpenAI-compat endpoint returns 403,
        which happens for preview/experimental models not exposed via compat.

        Converts OpenAI-style messages to Gemini's contents/systemInstruction
        format transparently.
        """
        contents: list[dict] = []
        system_parts: list[dict] = []

        for msg in messages:
            role = msg["role"]
            text = msg.get("content", "")
            if role == "system":
                system_parts.append({"text": text})
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": text}]})
            elif role == "assistant":
                # Gemini uses "model" instead of "assistant"
                contents.append({"role": "model", "parts": [{"text": text}]})

        generation_config: dict = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        thinking_budget = _thinking_budget_for_model(self.model)
        if thinking_budget is not None:
            generation_config["thinkingConfig"] = {"thinkingBudget": thinking_budget}

        payload: dict = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": system_parts}

        url = f"{_GEMINI_NATIVE_BASE}/models/{self.model}:generateContent"
        resp = self._client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
        )
        resp.raise_for_status()
        data = resp.json()
        self.last_usage = _normalize_gemini_usage(data.get("usageMetadata") or {})
        parts = data["candidates"][0].get("content", {}).get("parts", [])
        text_parts = [p.get("text", "") for p in parts if p.get("text")]
        if not text_parts:
            finish_reason = data["candidates"][0].get("finishReason", "unknown")
            raise RuntimeError(f"Gemini native returned no text (finishReason={finish_reason})")
        return "\n".join(text_parts)

    # -- OpenAI-compat API --------------------------------------------------

    def _chat_compat(
        self,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call the OpenAI-compatible endpoint."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self._is_deepseek:
            thinking = os.environ.get("DEEPSEEK_THINKING", "disabled").strip().lower()
            if thinking in {"enabled", "disabled"}:
                payload["thinking"] = {"type": thinking}
                if thinking == "enabled":
                    payload["reasoning_effort"] = os.environ.get(
                        "DEEPSEEK_REASONING_EFFORT", "high"
                    )

        resp = self._client.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers=headers,
        )

        # 403 on Gemini compat = model not available on compat layer.
        # Raise a specific sentinel so chat() can switch to native API.
        if resp.status_code in (403, 404) and self._is_gemini:
            raise _GeminiCompatForbidden(resp)

        try:
            return self._handle_compat_response(resp)
        except _NoAssistantContent as exc:
            if self._is_gemini:
                raise _GeminiCompatNoContent(resp) from exc
            raise

    def _handle_compat_response(self, resp: httpx.Response) -> str:
        resp.raise_for_status()
        data = resp.json()
        self.last_usage = _normalize_openai_usage(data.get("usage") or {})
        choice = data["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            content = "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("text")
            )
        if not content:
            raise _NoAssistantContent(
                f"No assistant content (finish_reason={choice.get('finish_reason')})"
            )
        return content

    # -- public API ---------------------------------------------------------

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """Send a chat completion request and return the assistant message text."""
        # Qwen3 optimization: prepend /no_think to skip chain-of-thought
        # reasoning, saving tokens on structured extraction tasks.
        if "qwen" in self.model.lower() and messages:
            first = messages[0]
            if first.get("role") == "user" and not first["content"].startswith("/no_think"):
                messages = [{"role": first["role"], "content": f"/no_think\n{first['content']}"}] + messages[1:]

        for attempt in range(_MAX_RETRIES):
            try:
                # Route to native Gemini if we've already confirmed it's needed
                if self._use_native_gemini:
                    return self._chat_native_gemini(messages, temperature, max_tokens)

                return self._chat_compat(messages, temperature, max_tokens)

            except _GeminiCompatForbidden:
                # Model not available on OpenAI-compat layer â€” switch to native.
                log.warning(
                    "Gemini compat endpoint returned 403 for model '%s'. "
                    "Switching to native generateContent API. "
                    "(Preview/experimental models are often compat-only on native.)",
                    self.model,
                )
                self._use_native_gemini = True
                # Retry immediately with native â€” don't count as a rate-limit wait
                try:
                    return self._chat_native_gemini(messages, temperature, max_tokens)
                except httpx.HTTPStatusError as native_exc:
                    raise RuntimeError(
                        f"Both Gemini endpoints failed. Compat: 403 Forbidden. "
                        f"Native: {native_exc.response.status_code} â€” "
                        f"{native_exc.response.text[:200]}"
                    ) from native_exc

            except _GeminiCompatNoContent:
                log.warning(
                    "Gemini compat returned no assistant content for model '%s'. "
                    "Retrying on native generateContent API with a larger output budget.",
                    self.model,
                )
                self._use_native_gemini = True
                return self._chat_native_gemini(
                    messages,
                    temperature,
                    max(max_tokens, _native_min_output_tokens()),
                )

            except httpx.HTTPStatusError as exc:
                resp = exc.response
                if resp.status_code in (429, 503) and attempt < _MAX_RETRIES - 1:
                    # Respect Retry-After header if provided (Gemini sends this).
                    retry_after = (
                        resp.headers.get("Retry-After")
                        or resp.headers.get("X-RateLimit-Reset-Requests")
                    )
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (ValueError, TypeError):
                            wait = _RATE_LIMIT_BASE_WAIT * (2 ** attempt)
                    else:
                        wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)

                    log.warning(
                        "LLM rate limited (HTTP %s). Waiting %ds before retry %d/%d. "
                        "Paid API tiers can still throttle; lower batch size/workers "
                        "or wait for quota to recover.",
                        resp.status_code, wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

            except httpx.TimeoutException:
                if attempt < _MAX_RETRIES - 1:
                    wait = min(_RATE_LIMIT_BASE_WAIT * (2 ** attempt), 60)
                    log.warning(
                        "LLM request timed out, retrying in %ds (attempt %d/%d)",
                        wait, attempt + 1, _MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                raise

        raise RuntimeError("LLM request failed after all retries")

    def ask(self, prompt: str, **kwargs) -> str:
        """Convenience: single user prompt -> assistant response."""
        return self.chat([{"role": "user", "content": prompt}], **kwargs)

    def close(self) -> None:
        self._client.close()


class _GeminiCompatForbidden(Exception):
    """Sentinel: Gemini OpenAI-compat returned 403. Switch to native API."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Gemini compat 403: {response.text[:200]}")


class _GeminiCompatNoContent(Exception):
    """Sentinel: Gemini compat returned 200 but no assistant text."""
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__("Gemini compat returned no assistant content")


class _NoAssistantContent(Exception):
    """Internal sentinel for OpenAI-compatible responses missing content."""


def _thinking_budget_for_model(model: str) -> int | None:
    """Return native Gemini thinking budget for 2.5 models."""
    model_l = model.lower()
    if "gemini-2.5-pro" in model_l:
        return int(os.environ.get("GEMINI_PRO_THINKING_BUDGET", "1024"))
    if "gemini-2.5-flash" in model_l:
        return int(os.environ.get("GEMINI_FLASH_THINKING_BUDGET", "0"))
    return None


def _native_min_output_tokens() -> int:
    return int(os.environ.get("GEMINI_NATIVE_MIN_OUTPUT_TOKENS", "8192"))


def _normalize_openai_usage(usage: dict) -> dict:
    if not usage:
        return {}
    completion_details = usage.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "thinking_tokens": completion_details.get("reasoning_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def _normalize_gemini_usage(usage: dict) -> dict:
    if not usage:
        return {}
    return {
        "prompt_tokens": usage.get("promptTokenCount"),
        "completion_tokens": usage.get("candidatesTokenCount"),
        "thinking_tokens": usage.get("thoughtsTokenCount"),
        "total_tokens": usage.get("totalTokenCount"),
    }


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instances: dict[tuple[str, str, str], LLMClient] = {}


def _model_for_stage(stage: str | None) -> str | None:
    """Return a stage-specific model override from environment, if configured."""
    if not stage:
        return None
    stage_key = f"LLM_{stage.upper()}_MODEL"
    return os.environ.get(stage_key) or None


def _provider_for_stage(stage: str | None) -> str | None:
    """Return a stage-specific provider override from environment, if configured."""
    if not stage:
        return None
    stage_key = f"LLM_{stage.upper()}_PROVIDER"
    return os.environ.get(stage_key) or None


def get_client(
    model_override: str | None = None,
    stage: str | None = None,
    provider_override: str | None = None,
) -> LLMClient:
    """Return a cached LLMClient, optionally using a stage-specific model."""
    model_override = model_override or _model_for_stage(stage)
    provider_override = provider_override or _provider_for_stage(stage)
    base_url, model, api_key = _detect_provider(
        model_override=model_override,
        provider_override=provider_override,
    )
    key = (base_url, model, api_key)
    if key not in _instances:
        log.info("LLM provider: %s  model: %s", base_url, model)
        _instances[key] = LLMClient(base_url, model, api_key)
    return _instances[key]

