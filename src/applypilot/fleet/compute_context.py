"""Versioned shared-context assets (resume / preference / KG prompt / search config)
served through the broker's fleet_assets blob store. Workers fetch once and re-fetch
on a version change; the brain never lands on a worker disk persistently."""
from __future__ import annotations

import json
import base64

from applypilot.apply import pgqueue
from applypilot.fleet.compute_adapters import ComputeContext

_RESUME, _PREF, _KG, _CFG, _VER = "ctx:resume", "ctx:preference", "ctx:kg_prompt", "ctx:search_cfg", "ctx:version"


def _b(s: str | None) -> bytes:
    return (s or "").encode("utf-8")


def publish_context(conn, *, resume_text, preference_profile, kg_prompt, search_cfg, version) -> None:
    pgqueue.put_asset(conn, _RESUME, _b(resume_text))
    pgqueue.put_asset(conn, _PREF, _b(json.dumps(preference_profile or {})))
    pgqueue.put_asset(conn, _KG, _b(kg_prompt))
    pgqueue.put_asset(conn, _CFG, _b(json.dumps(search_cfg or {})))
    pgqueue.put_asset(conn, _VER, _b(version))


def load_context(conn, *, providers, fallback=(), ensemble=False) -> tuple[ComputeContext, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT public.fleet_worker_runtime_state('') AS state")
        state = cur.fetchone()["state"] or {}
    conn.rollback()
    assets = state.get("compute_context") or {}

    def text(name: str) -> str:
        encoded = assets.get(name)
        return base64.b64decode(encoded).decode("utf-8") if encoded else ""

    version = text(_VER)
    pref = text(_PREF)
    cfg = text(_CFG)
    ctx = ComputeContext(
        resume_text=text(_RESUME),
        preference_profile=json.loads(pref) if pref else None,
        kg_prompt=text(_KG) or None,
        search_cfg=json.loads(cfg) if cfg else None,
        ctx_version=version,
        providers=list(providers), fallback=list(fallback), ensemble=bool(ensemble),
    )
    return ctx, version
