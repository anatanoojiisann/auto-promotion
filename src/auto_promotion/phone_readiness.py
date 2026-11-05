"""Read configured fleet readiness. An absent phone is an explicit waiting state."""

from .backends.common import BackendError
from .phone_protocol import capability_status, expected_version


def readiness(engine):
    services, seen = [], set()
    for name in ("phone_social", "phone_reddit"):
        service = getattr(engine.adapters, name, None)
        if service is None or service.url in seen:
            continue
        seen.add(service.url)
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
    return {
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
