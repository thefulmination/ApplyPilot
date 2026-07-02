from __future__ import annotations


def test_load_search_config_uses_env_path_at_call_time(tmp_path, monkeypatch) -> None:
    """APPLYPILOT_SEARCH_CONFIG_PATH set AFTER import (the .env case) must win.

    Regression for the load-order bug where SEARCH_CONFIG_PATH froze at module
    import, silently ignoring searches_tuned.yaml configured via .applypilot/.env.
    """
    from applypilot import config

    tuned = tmp_path / "searches_tuned.yaml"
    tuned.write_text("searches:\n  - keywords: tuned-marker\n", encoding="utf-8")
    # Simulate load_env() having populated the env after config was imported.
    monkeypatch.setenv("APPLYPILOT_SEARCH_CONFIG_PATH", str(tuned))

    cfg = config.load_search_config()

    assert cfg == {"searches": [{"keywords": "tuned-marker"}]}


def test_load_search_config_falls_back_to_frozen_default(tmp_path, monkeypatch) -> None:
    # Without the env var, behavior is unchanged: the import-time default path.
    from applypilot import config

    monkeypatch.delenv("APPLYPILOT_SEARCH_CONFIG_PATH", raising=False)
    frozen = tmp_path / "searches.yaml"
    frozen.write_text("searches:\n  - keywords: frozen-marker\n", encoding="utf-8")
    monkeypatch.setattr(config, "SEARCH_CONFIG_PATH", frozen)

    cfg = config.load_search_config()

    assert cfg == {"searches": [{"keywords": "frozen-marker"}]}
