"""Credential-free execution routes for every supported platform and connection."""

import re
from dataclasses import asdict, dataclass

from .backends.common import BackendError, read_profile
from .backends.tiktok_desktop import tabbit_candidate_enabled
from .models import PLATFORMS, digest

TRANSPORTS = {
    "telegram": "mtproto",
    "discord": "bot",
    "instagram": "graph",
    "facebook": "desktop",
    "tiktok": "organic",
    "reddit": "manual",
    "x": "playwright",
}
POOLS = frozenset(
    [f"{p}.{t}" for p, t in TRANSPORTS.items()]
    + ["x.edge", "x.tabbit", "x.browser_use", "x.api"]
    + ["instagram.desktop", "tiktok.desktop"]
    + ["tiktok.tabbit"]
    + [f"{p}.manual" for p in PLATFORMS]
    + [f"simulation.{p}" for p in PLATFORMS]
    + [f"{p}.phone" for p in PLATFORMS]
)


def tabbit_resource(profile):
    """One resource identity across all platforms on a bound instance."""
    instance = profile.get("instance_id") or profile.get("tabbit_instance", "")
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
    execution_method: str = "inherit"
    node_os: str = "any"

    def public(self):
        value = asdict(self)
        if self.transport != "phone":
            value.pop("execution_method")
            value.pop("node_os")
        return value


def task_route(account, payload, method, registry):
    default = route_for(account, payload)
    if method != "phone" or default.transport in {"phone", "simulation", "manual"}:
        return default
    # Keep explicit phone work in the native pool even when a binding is missing.
    service = getattr(registry, "phone_social", None)
    binding = service.shared_account_binding(account) if service else None
    return ExecutionRoute(account["platform"], "phone", account["platform"] + ".phone", default.identity,
                          "phone:" + digest(binding.device_id) if binding else None,
                          "phone", binding.node_os if binding else "any")


def route_for(account, payload=None, *, profile=None):
    platform = account["platform"]
    if platform not in PLATFORMS:
        raise ValueError("UnsupportedPlatform")
    identity = f"{platform}:" + (account.get("identity_hash") or account["id"])
    if account["mode"] == "simulation":
        return ExecutionRoute(platform, "simulation", f"simulation.{platform}", identity)
    payload = payload or {}
    if profile is None:
        try:
            profile = read_profile(platform, account["backend_label"])
        except BackendError:
            profile = {}
    if profile.get("connection_type") == "phone_fleet":
        binding = profile.get("phone_fleet", {})
        if not binding.get("device_id") or binding.get("node_os", "any") not in {"any", "windows", "macos"}:
            raise BackendError("PhoneFleetBindingInvalid")
        return ExecutionRoute(platform, "phone", platform + ".phone", identity,
            "phone:" + digest(binding["device_id"]), "phone", binding.get("node_os", "any"))
    transport = TRANSPORTS[platform]
    resource = None
    if platform in {"facebook", "instagram", "tiktok"}:
        if profile is None:
            try:
                profile = read_profile(platform, account["backend_label"])
            except BackendError:
                profile = {}
        if profile.get("connection_type") in {"meta_desktop", "tiktok_desktop"}:
            transport = "desktop"
            resource = (
                "desktop:"
                + profile["browser_channel"]
                + ":"
                + digest(str(profile.get("instance_id") or profile.get("browser") or "default"))[
                    :24
                ]
            )
        elif platform == "tiktok" and tabbit_candidate_enabled(profile):
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
        else:
            resource = "profile:" + digest([platform, account["backend_label"]])
    if payload.get("execution_mode") == "manual" or payload.get("action") == "manual_comment":
        transport, resource = "manual", None
    return ExecutionRoute(platform, transport, f"{platform}.{transport}", identity, resource)


def supports_independent_local(account, *, profile=None):
    """Reviewed execution routes, separate from platform/action capabilities."""
    if profile is None:
        try:
            profile = read_profile(account["platform"], account["backend_label"])
        except BackendError:
            profile = {}
    try:
        route = route_for(account, profile=profile)
    except (KeyError, TypeError, ValueError):
        return False
    return (
        account["platform"] == "x"
        and route.transport in {"playwright", "edge", "tabbit", "browser_use", "api"}
    ) or (
        profile.get("connection_type")
        == {
            "facebook": "meta_desktop",
            "instagram": "meta_desktop",
            "tiktok": "tiktok_desktop",
        }.get(account["platform"], "unsupported")
        and route.transport == "desktop"
    )
