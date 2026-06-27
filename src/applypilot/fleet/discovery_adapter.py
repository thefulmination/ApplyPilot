"""Lean discovery scrape adapter: wraps JobSpy's scrape+filter (imported from
discovery.jobspy, which is NEVER modified) and returns posting dicts. No brain write
-- the worker stages the postings to Postgres; the home box ingests them."""
from __future__ import annotations

from typing import Callable

from applypilot.discovery.jobspy import (
    _scrape_with_retry, _location_ok, _load_location_config, parse_proxy,
)


def make_search_fn(*, results_per_site=50, hours_old=72, proxy=None, search_cfg=None) -> Callable[[dict], list[dict]]:
    accept, reject = _load_location_config(search_cfg or {})
    proxy_config = parse_proxy(proxy) if proxy else None

    def search_fn(task: dict) -> list[dict]:
        params = task.get("params") or {}
        sites = params.get("sites") or [task["board"]]
        kwargs = {
            "site_name": sites, "search_term": task["query"], "location": task.get("location") or "",
            "results_wanted": results_per_site, "hours_old": hours_old,
            "description_format": "markdown", "country_indeed": "usa", "verbose": 1,
        }
        if params.get("remote"):
            kwargs["is_remote"] = True
        if proxy_config:
            kwargs["proxies"] = [proxy_config["jobspy"]]
        if "linkedin" in sites:
            kwargs["linkedin_fetch_description"] = True
        try:
            df = _scrape_with_retry(kwargs)
        except Exception:
            return []  # a scrape block -> empty; the worker records a board-block outcome
        if df is None or len(df) == 0:
            return []
        df = df[df.apply(lambda row: _location_ok(row.get("location"), accept, reject), axis=1)]
        return df.to_dict("records")

    return search_fn
