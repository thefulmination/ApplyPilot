from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


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

TRUSTED_HOST_MODES = {"allow", "canary", "supervised"}


def _normalize_host(host: str | None) -> str:
    if not host or any(char.isspace() for char in host):
        return ""
    host = host.lower()
    if any(not (char.isalnum() or char in ".-") for char in host):
        return ""
    return host


def _host_matches_domain(host: str, exact_host: str, domain: str) -> bool:
    return host == exact_host or host.endswith(f".{domain}")


def _classify_ats_from_host(host: str) -> str:
    if _host_matches_domain(host, "jobs.ashbyhq.com", "ashbyhq.com"):
        return "ashby"
    if _host_matches_domain(host, "boards.greenhouse.io", "greenhouse.io") or _host_matches_domain(
        host, "grnh.se", "grnh.se"
    ):
        return "greenhouse"
    if _host_matches_domain(host, "jobs.lever.co", "lever.co"):
        return "lever"
    if host.endswith(".myworkdayjobs.com") or host.endswith(".workdayjobs.com"):
        return "workday"
    if _host_matches_domain(host, "www.smartrecruiters.com", "smartrecruiters.com"):
        return "smartrecruiters"
    if _host_matches_domain(host, "apply.workable.com", "workable.com"):
        return "workable"
    return "other"


def _normalize_trusted_hosts(trusted_hosts: dict[str, str] | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for host, mode in (trusted_hosts or {}).items():
        if not isinstance(mode, str) or mode not in TRUSTED_HOST_MODES:
            raise ValueError(f"invalid trusted host mode for {host!r}: {mode!r}")
        normalized_host = _normalize_host(host if isinstance(host, str) else None)
        if normalized_host:
            normalized[normalized_host] = mode
    return normalized


def host_from_url(url: str | None) -> str:
    try:
        parsed = urlparse(url or "")
        host = parsed.hostname
    except Exception:
        return ""
    return _normalize_host(host)


def decide_host_policy(
    application_url: str | None,
    *,
    trusted_hosts: dict[str, str] | None = None,
) -> HostPolicyDecision:
    host = host_from_url(application_url)
    ats = _classify_ats_from_host(host)
    trusted_hosts = _normalize_trusted_hosts(trusted_hosts)
    if not host:
        return HostPolicyDecision(mode="supervised", reason="invalid_or_missing_host", host=host, ats=ats)
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
