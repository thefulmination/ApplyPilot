from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from applypilot.fleet.cost_quality_report import classify_ats


@dataclass(frozen=True)
class HostPolicyDecision:
    mode: str
    reason: str
    host: str
    ats: str

    @property
    def unattended_allowed(self) -> bool:
        return self.mode in {"allow", "canary"}

    @property
    def label(self) -> str:
        return f"{self.mode}:{self.reason}"


LOW_YIELD_SUPERVISED_HOSTS = {
    "www.indeed.com": "login_gate_prone",
    "hiring.cafe": "login_gate_prone",
    "www.linkedin.com": "linkedin_profile_required",
}


def host_from_url(url: str | None) -> str:
    try:
        parsed = urlparse(url or "")
    except Exception:
        return ""
    return (parsed.hostname or "").lower()


def decide_host_policy(
    application_url: str | None,
    *,
    trusted_hosts: dict[str, str] | None = None,
) -> HostPolicyDecision:
    host = host_from_url(application_url)
    ats = classify_ats(application_url)
    trusted_hosts = trusted_hosts or {}
    if host in trusted_hosts:
        return HostPolicyDecision(mode=trusted_hosts[host], reason="trusted_host", host=host, ats=ats)
    if ats == "workday":
        return HostPolicyDecision(mode="supervised", reason="workday_tenant_requires_trust", host=host, ats=ats)
    if host in LOW_YIELD_SUPERVISED_HOSTS:
        return HostPolicyDecision(mode="supervised", reason=LOW_YIELD_SUPERVISED_HOSTS[host], host=host, ats=ats)
    if ats in {"ashby", "greenhouse", "workable"}:
        return HostPolicyDecision(mode="allow", reason=f"{ats}_baseline_healthy", host=host, ats=ats)
    if ats in {"lever", "smartrecruiters"}:
        return HostPolicyDecision(mode="canary", reason=f"{ats}_limited_history", host=host, ats=ats)
    return HostPolicyDecision(mode="allow", reason="default_unclassified", host=host, ats=ats)
