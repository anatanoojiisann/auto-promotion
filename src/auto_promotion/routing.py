"""Credential-free execution routes for every supported platform and connection."""

import re
from dataclasses import asdict, dataclass

from .backends.common import BackendError, read_profile
from .models import PLATFORMS, digest

TRANSPORTS = {
    "telegram": "mtproto",
    "discord": "bot",
    "instagram": "graph",
    "tiktok": "organic",
    "reddit": "manual",
    "x": "playwright",
}
POOLS = frozenset(
    [f"{p}.{t}" for p, t in TRANSPORTS.items()]
    + ["x.edge", "x.tabbit", "x.browser_use", "x.api", "tiktok.tabbit"]
    + [f"{p}.manual" for p in PLATFORMS]
    + [f"simulation.{p}" for p in PLATFORMS]
)


def tabbit_resource(profile):
    """One resource identity across all platforms on a bound instance."""
    instance = profile.get("tabbit_instance", "")
    if not isinstance(instance, str) or not re.fullmatch(r"[0-9A-F]{16}", instance):
        return "desktop:tabbit:unbound"
    return "desktop:tabbit:" + digest(instance)[:24]


@dataclass(frozen=True)
class ExecutionRoute:
    platform: str
    transport: str
    pool: str
    identity: str
    resource: str | None = None

    def public(self):
        return asdict(self)


def route_for(account, payload=None, *, profile=None):
    platform = account["platform"]
    if platform not in PLATFORMS:
        raise ValueError("UnsupportedPlatform")
    identity = f"{platform}:" + (account.get("identity_hash") or account["id"])
    if account["mode"] == "simulation":
        return ExecutionRoute(platform, "simulation", f"simulation.{platform}", identity)
    payload = payload or {}
    transport = TRANSPORTS[platform]
    resource = None
    if platform == "tiktok":
        if profile is None:
            try:
                profile = read_profile(platform, account["backend_label"])
            except BackendError:
                profile = {}
        if profile.get("browser_channel") == "tabbit":
            transport = "tabbit"
            resource = tabbit_resource(profile)
    if platform == "x":
        if profile is None:
            try:
                profile = read_profile(platform, account["backend_label"])
            except BackendError:
                profile = {}
        channel = profile.get("browser_channel", "chromium")
        transport = {"tabbit": "tabbit", "browser_use": "browser_use", "msedge": "edge"}.get(
            channel, "playwright"
        )
        if profile.get("connection_type") == "x_api":
            transport = "api"
        if transport in {"tabbit", "browser_use"}:
            # These desktop integrations can share the active login/profile across accounts.
            resource = (
                "desktop:"
                + transport
                + ":"
                + digest(str(profile.get("instance_id") or profile.get("browser") or "default"))[
                    :24
                ]
            )
            if transport == "tabbit":
                resource = tabbit_resource(profile)
        else:
            resource = "profile:" + digest([platform, account["backend_label"]])
    if payload.get("execution_mode") == "manual" or payload.get("action") == "manual_comment":
        transport, resource = "manual", None
    return ExecutionRoute(platform, transport, f"{platform}.{transport}", identity, resource)
