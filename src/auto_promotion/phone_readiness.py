"""Read configured fleet readiness. An absent phone is an explicit waiting state."""

from .backends.common import BackendError
from .phone_protocol import capability_status, expected_version


def _phone_binding_file(path, model, parser):
    if path is None:
        return None
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Validation errors can contain raw OAuth credentials.
        parser.error(f"Unable to load the supplied {model.__name__} file")


def load_bindings(social_path, reddit_path, parser):
    # Installations without phone configuration do not need to import a phone backend.
    social = reddit = None
    if social_path is not None:
        from .phone_social import SocialPhoneConfig

        social = _phone_binding_file(social_path, SocialPhoneConfig, parser)
    if reddit_path is not None:
        from .phone_reddit import RedditPhoneConfig

        reddit = _phone_binding_file(reddit_path, RedditPhoneConfig, parser)
    return social, reddit


def readiness(engine, *, platform=None):
    from .core import NotFound
    from .phone_rounds import PLATFORMS

    if platform is not None and platform not in PLATFORMS:
        raise NotFound("PhonePlatformNotSupported")
    selected = sorted(PLATFORMS) if platform is None else [platform]
    services, checked = [], {}
    for name in ("phone_social", "phone_reddit"):
        service = getattr(engine.adapters, name, None)
        relevant = [p for p in selected if (p == "reddit") == (name == "phone_reddit")]
        if not relevant or service is None:
            continue
        try:
            item = capability_status(service.request("GET", "/capabilities"))
        except BackendError as exc:
            item = {
                "state": "authorization_required"
                if exc.safe_code == "PhoneFleetAuthorizationRequired"
                else "service_unavailable",
                "software_ready": False,
                "device_ready": False,
                "reason": exc.safe_code,
                "expected_protocol": expected_version(),
            }
        except Exception:
            item = {
                "state": "service_unavailable",
                "software_ready": False,
                "device_ready": False,
                "reason": "PhoneFleetReadinessUnavailable",
                "expected_protocol": expected_version(),
            }
        services.append(item)
        for app in relevant:
            checked[app] = (service, item)
    stop = engine.store.one("SELECT value FROM meta WHERE key='global_stop'")
    stopped = not stop or stop["value"] != "0"
    platforms = []
    for app in selected:
        service, remote = checked.get(app, (None, {}))
        bindings = {
            key: b
            for key, b in (service.config.bindings.items() if service else ())
            if app == "reddit" or b.platform == app
        }
        enabled = bool(service and service.config.submit_enabled)
        supported = (
            remote.get("operations", {}).get(
                "reddit_submit" if app == "reddit" else "social_submit"
            )
            == "supported"
        )
        reasons = []
        if stopped:
            reasons.append("GlobalStop")
        if not engine.live_enabled:
            reasons.append("LiveWritesDisabled")
        if not bindings:
            reasons.append("PhoneBindingRequired")
        if not enabled:
            reasons.append("PhoneSubmissionDisabled")
        if not supported:
            reasons.append(remote.get("reason") or "PhoneFleetSubmissionUnsupported")
        if app == "reddit" and bindings and not service.config.reddit_read_access_token:
            reasons.append("OfficialReadAuthorizationRequired")
        if (
            app == "telegram"
            and bindings
            and all(service.telegram_queue_blocker(key) for key in bindings)
        ):
            reasons.append("PhoneTelegramAuthoritativeQueueRequired")
        platforms.append(
            {
                "platform": app,
                "bound_account_count": len(bindings),
                "sender_configured": enabled,
                "fleet_submit_supported": supported,
                "blockers": reasons,
                "submission_authorized": False,
            }
        )
    return {
        "platforms": platforms,
        "global_stop": stopped,
        "live_writes_enabled": bool(engine.live_enabled),
        "configured": bool(services),
        "state": "not_configured"
        if not services
        else "software_ready"
        if all(s["software_ready"] for s in services)
        else "setup_required",
        "services": services,
        "hardware_validation": "not_started",
        "publication_verified": False,
        "submission_authorized": False,
    }
