"""ApplyPilot configuration: paths, platform detection, user data."""

import os
import platform
import shutil
import subprocess
from pathlib import Path

# User data directory — all user-specific files live here
APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot"))


def _path_from_env(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else default


# Core paths
DB_PATH = _path_from_env("APPLYPILOT_DB_PATH", APP_DIR / "applypilot.db")
PROFILE_PATH = _path_from_env("APPLYPILOT_PROFILE_PATH", APP_DIR / "profile.json")
RESUME_PATH = _path_from_env("APPLYPILOT_RESUME_PATH", APP_DIR / "resume.txt")
RESUME_PDF_PATH = _path_from_env("APPLYPILOT_RESUME_PDF_PATH", APP_DIR / "resume.pdf")
SEARCH_CONFIG_PATH = _path_from_env("APPLYPILOT_SEARCH_CONFIG_PATH", APP_DIR / "searches.yaml")
RESUME_STRATEGY_PATH = _path_from_env("APPLYPILOT_RESUME_STRATEGY_PATH", APP_DIR / "resume_strategy.yaml")
PREFERENCE_PROFILE_PATH = _path_from_env("APPLYPILOT_PREFERENCE_PROFILE_PATH", APP_DIR / "job_preference_profile.json")
# antigravity: python-scorer-integration-v1
KNOWLEDGE_GRAPH_PROMPT_PATH = _path_from_env("APPLYPILOT_KNOWLEDGE_GRAPH_PROMPT_PATH", APP_DIR / "job_knowledge_graph_prompt.md")
ENV_PATH = _path_from_env("APPLYPILOT_ENV_PATH", APP_DIR / ".env")


def base_resume_enabled() -> bool:
    """True when apply should use the base resume (no per-job tailoring).

    Set by `applypilot apply --base-resume` via APPLYPILOT_BASE_RESUME. In this
    mode the apply flow falls back to RESUME_PATH / RESUME_PDF_PATH for any job
    that has no tailored resume of its own -- so the owner applies with his
    hand-made base resume as-is, never an AI-tailored rewrite.
    """
    return os.environ.get("APPLYPILOT_BASE_RESUME", "").strip().lower() in ("1", "true", "yes", "on")


def cos_rescue_enabled() -> bool:
    """Opt-in: floor audit_score for title-certain Chief-of-Staff / Strategy-&-Ops
    roles with very high role_fit, so the LLM's pivot-penalized base_score can't
    bury them below the apply gate. Default OFF; benchmark-sensitive (changes labels).
    """
    return os.environ.get("APPLYPILOT_COS_RESCUE", "").strip().lower() in ("1", "true", "yes", "on")


def resolve_resume_stem(tailored_resume_path: str | None) -> str | None:
    """Resume stem whose .pdf/.txt siblings the apply flow uploads/reads.

    Returns the per-job tailored path if present; otherwise the base resume
    stem when --base-resume mode is on and the base PDF exists; otherwise None.
    """
    if tailored_resume_path:
        return tailored_resume_path
    if base_resume_enabled() and RESUME_PDF_PATH.exists():
        return str(RESUME_PATH)
    return None


# Generated output
TAILORED_DIR = APP_DIR / "tailored_resumes"
COVER_LETTER_DIR = APP_DIR / "cover_letters"
JOB_EXPORT_DIR = APP_DIR / "job_exports"
SCORE_AUDIT_DIR = APP_DIR / "score_audits"
APPLICATION_EXPORT_DIR = APP_DIR / "application_exports"
LOG_DIR = APP_DIR / "logs"

# Chrome worker isolation
CHROME_WORKER_DIR = APP_DIR / "chrome-workers"
APPLY_WORKER_DIR = APP_DIR / "apply-workers"

# Package-shipped config (YAML registries)
PACKAGE_DIR = Path(__file__).parent
CONFIG_DIR = PACKAGE_DIR / "config"


def get_chrome_path() -> str:
    """Auto-detect Chrome/Chromium executable path, cross-platform.

    Override with CHROME_PATH environment variable.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    system = platform.system()

    if system == "Windows":
        candidates = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:  # Linux
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for c in candidates:
        if c and c.exists():
            return str(c)

    # Fall back to PATH search
    for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError(
        "Chrome/Chromium not found. Install Chrome or set CHROME_PATH environment variable."
    )


def get_claude_path() -> str:
    """Find Claude Code CLI, including the project-local npm install."""
    env_path = os.environ.get("CLAUDE_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    found = shutil.which("claude") or shutil.which("claude.exe") or shutil.which("claude.cmd")
    if found:
        return found

    project_root = PACKAGE_DIR.parent.parent
    if platform.system() == "Windows":
        candidates = [
            project_root / ".tools/claude/node_modules/@anthropic-ai/claude-code/bin/claude.exe",
            project_root / ".tools/claude/node_modules/@anthropic-ai/claude-code-win32-x64/claude.exe",
            project_root / ".tools/claude/node_modules/.bin/claude.cmd",
        ]
    else:
        candidates = [
            project_root / ".tools/claude/node_modules/.bin/claude",
        ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    raise FileNotFoundError(
        "Claude Code CLI not found. Install from https://claude.ai/code or set CLAUDE_PATH."
    )


def get_codex_path() -> str:
    """Find Codex CLI for the auto-apply agent."""
    def _is_runnable(path: str | Path) -> bool:
        try:
            subprocess.run(
                [str(path), "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    env_path = os.environ.get("CODEX_PATH")
    if env_path and Path(env_path).exists() and _is_runnable(env_path):
        return env_path

    found = shutil.which("codex") or shutil.which("codex.exe") or shutil.which("codex.cmd")
    if found and _is_runnable(found):
        return found

    project_root = PACKAGE_DIR.parent.parent
    candidates = [
        project_root / ".tools/codex/node_modules/.bin/codex.cmd",
        project_root / ".tools/codex/node_modules/.bin/codex",
    ]
    for candidate in candidates:
        if candidate.exists() and _is_runnable(candidate):
            return str(candidate)

    raise FileNotFoundError(
        "Codex CLI not found. Install Codex CLI or set CODEX_PATH."
    )


def get_chrome_user_data() -> Path:
    """Default Chrome user data directory, cross-platform."""
    system = platform.system()
    if system == "Windows":
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    else:
        return Path.home() / ".config" / "google-chrome"


def ensure_dirs():
    """Create all required directories."""
    for d in [
        APP_DIR,
        TAILORED_DIR,
        COVER_LETTER_DIR,
        JOB_EXPORT_DIR,
        SCORE_AUDIT_DIR,
        APPLICATION_EXPORT_DIR,
        LOG_DIR,
        CHROME_WORKER_DIR,
        APPLY_WORKER_DIR,
    ]:
        d.mkdir(parents=True, exist_ok=True)


def load_profile() -> dict:
    """Load user profile from ~/.applypilot/profile.json."""
    import json
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Profile not found at {PROFILE_PATH}. Run `applypilot init` first."
        )
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def load_search_config() -> dict:
    """Load search configuration from ~/.applypilot/searches.yaml."""
    import yaml
    if not SEARCH_CONFIG_PATH.exists():
        # Fall back to package-shipped example
        example = CONFIG_DIR / "searches.example.yaml"
        if example.exists():
            return yaml.safe_load(example.read_text(encoding="utf-8"))
        return {}
    return yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8"))


def load_resume_strategy() -> dict:
    """Load optional resume positioning rules used by tailoring/cover prompts."""
    import yaml
    if not RESUME_STRATEGY_PATH.exists():
        return {}
    return yaml.safe_load(RESUME_STRATEGY_PATH.read_text(encoding="utf-8")) or {}


def load_preference_profile() -> dict | None:
    """Load optional human-labeled job preference profile for score calibration.

    This file is produced by an external recommendation engine ("brainstorm").
    Because it crosses a project boundary, malformed or wrong-shaped input must
    NOT crash scoring -- on any problem we warn and return None so scoring
    proceeds without calibration rather than aborting the whole stage.
    """
    import json
    import logging

    path = _path_from_env("APPLYPILOT_PREFERENCE_PROFILE_PATH", PREFERENCE_PROFILE_PATH)
    if not path.exists():
        return None
    log = logging.getLogger(__name__)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, ValueError) as e:
        log.warning(
            "Ignoring preference profile at %s: could not read/parse it (%s). "
            "Scoring will proceed without recommendation calibration.",
            path, e,
        )
        return None
    if not isinstance(data, dict):
        log.warning(
            "Ignoring preference profile at %s: expected a JSON object, got %s.",
            path, type(data).__name__,
        )
        return None
    return data


def load_sites_config() -> dict:
    """Load sites.yaml configuration (sites list, manual_ats, blocked, etc.)."""
    import yaml
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def is_manual_ats(url: str | None) -> bool:
    """Check if a URL routes through an ATS that requires manual application."""
    if not url:
        return False
    sites_cfg = load_sites_config()
    domains = sites_cfg.get("manual_ats", [])
    url_lower = url.lower()
    return any(domain in url_lower for domain in domains)


def is_auth_gated_application(url: str | None) -> bool:
    """Check if an application URL is likely to require login, account setup, or 2FA."""
    if not url:
        return False
    sites_cfg = load_sites_config()
    auth_cfg = sites_cfg.get("auth_gated", {}) or {}
    domains = auth_cfg.get("domains", [])
    patterns = auth_cfg.get("url_patterns", [])
    url_lower = url.lower()
    return (
        any(domain.lower() in url_lower for domain in domains)
        or any(pattern.lower() in url_lower for pattern in patterns)
        or any(domain.lower() in url_lower for domain in load_blocked_sso())
    )


def load_blocked_sites() -> tuple[set[str], list[str]]:
    """Load blocked sites and URL patterns from sites.yaml.

    Returns:
        (blocked_site_names, blocked_url_patterns)
    """
    cfg = load_sites_config()
    blocked = cfg.get("blocked", {})
    sites = set(blocked.get("sites", []))
    patterns = blocked.get("url_patterns", [])
    return sites, patterns


def load_blocked_sso() -> list[str]:
    """Load blocked SSO domains from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("blocked_sso", [])


def load_base_urls() -> dict[str, str | None]:
    """Load site base URLs for URL resolution from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("base_urls", {})


# ---------------------------------------------------------------------------
# Default values — referenced across modules instead of magic numbers
# ---------------------------------------------------------------------------

DEFAULTS = {
    "min_score": 7,
    "generation_batch_size": 900,
    "max_apply_attempts": 3,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
}


def get_min_score() -> int:
    """Default minimum score for tailor/cover/apply selection.

    Reads ``APPLYPILOT_MIN_SCORE`` (e.g. set in ~/.applypilot/.env) so the floor
    can be configured once instead of passing --min-score every run; falls back
    to DEFAULTS['min_score']. Call after load_env() so .env values are visible.
    """
    raw = os.environ.get("APPLYPILOT_MIN_SCORE")
    if raw is None or str(raw).strip() == "":
        return DEFAULTS["min_score"]
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULTS["min_score"]
    if not (1 <= value <= 10):
        import logging
        logging.getLogger(__name__).warning(
            "APPLYPILOT_MIN_SCORE=%r is outside 1-10; using default %d",
            raw, DEFAULTS["min_score"],
        )
        return DEFAULTS["min_score"]
    return value


def load_env():
    """Load environment variables from ~/.applypilot/.env if it exists."""
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    # Also try CWD .env as fallback
    load_dotenv()


# ---------------------------------------------------------------------------
# Tier system — feature gating by installed dependencies
# ---------------------------------------------------------------------------

TIER_LABELS = {
    1: "Discovery",
    2: "AI Scoring & Tailoring",
    3: "Full Auto-Apply",
}

TIER_COMMANDS: dict[int, list[str]] = {
    1: ["init", "run discover", "run enrich", "status", "dashboard"],
    2: ["run score", "run tailor", "run cover", "run pdf", "run"],
    3: ["apply"],
}


def get_tier() -> int:
    """Detect the current tier based on available dependencies.

    Tier 1 (Discovery):            Python + pip
    Tier 2 (AI Scoring & Tailoring): + LLM API key
    Tier 3 (Full Auto-Apply):       + Claude Code CLI or Codex CLI + Chrome
    """
    load_env()

    has_llm = any(os.environ.get(k) for k in ("GEMINI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LLM_URL"))
    if not has_llm:
        return 1

    has_apply_agent = False
    for get_agent_path in (get_claude_path, get_codex_path):
        try:
            get_agent_path()
            has_apply_agent = True
            break
        except FileNotFoundError:
            pass
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    if has_apply_agent and has_chrome:
        return 3

    return 2


def check_tier(required: int, feature: str) -> None:
    """Raise SystemExit with a clear message if the current tier is too low.

    Args:
        required: Minimum tier needed (1, 2, or 3).
        feature: Human-readable description of the feature being gated.
    """
    current = get_tier()
    if current >= required:
        return

    from rich.console import Console
    _console = Console(stderr=True)

    missing: list[str] = []
    if required >= 2 and not any(os.environ.get(k) for k in ("GEMINI_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LLM_URL")):
        missing.append("LLM API key — run [bold]applypilot init[/bold] or set GEMINI_API_KEY or DEEPSEEK_API_KEY")
    if required >= 3:
        has_apply_agent = False
        for get_agent_path in (get_claude_path, get_codex_path):
            try:
                get_agent_path()
                has_apply_agent = True
                break
            except FileNotFoundError:
                pass
        if not has_apply_agent:
            missing.append("Claude Code CLI or Codex CLI — install one, or set CLAUDE_PATH/CODEX_PATH")
        try:
            get_chrome_path()
        except FileNotFoundError:
            missing.append("Chrome/Chromium — install or set CHROME_PATH")

    _console.print(
        f"\n[red]'{feature}' requires {TIER_LABELS.get(required, f'Tier {required}')} (Tier {required}).[/red]\n"
        f"Current tier: {TIER_LABELS.get(current, f'Tier {current}')} (Tier {current})."
    )
    if missing:
        _console.print("\n[yellow]Missing:[/yellow]")
        for m in missing:
            _console.print(f"  - {m}")
    _console.print()
    raise SystemExit(1)
