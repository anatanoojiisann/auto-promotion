"""Version checks and read-only legacy receipt boundaries; never retry a mutation."""

import re

from .backends.common import BackendError


def expected_version():
    from phone_fleet_protocol import VERSION

    return VERSION


def capability_status(value):
    expected = expected_version()
    if not isinstance(value, dict):
        return {
            "state": "invalid_response",
            "software_ready": False,
            "device_ready": False,
            "expected_protocol": expected,
            "reason": "PhoneFleetInvalidCapabilities",
        }
    version = value.get("version")
    remote = (
        version
        if isinstance(version, str) and re.fullmatch(r"fleet[.]internal/[0-9]+[.][0-9]+", version)
        else "invalid"
    )
    base = {
        "expected_protocol": expected,
        "remote_protocol": remote,
        "device_ready": False,
        "submission_authorized": False,
    }
    if version != expected:
        return {
            **base,
            "state": "upgrade_required",
            "software_ready": False,
            "reason": "PhoneFleetProtocolMismatch",
        }
    operations = value.get("operations")
    if not isinstance(operations, dict) or (
        not isinstance(operations.get("observe"), str)
        or operations.get("observe") not in {"supported", "unsupported"}
    ):
        return {
            **base,
            "state": "invalid_response",
            "software_ready": False,
            "reason": "PhoneFleetInvalidCapabilities",
        }
    ready = operations["observe"] == "supported"
    return {
        **base,
        "state": "device_ready" if ready else "waiting_for_device",
        "software_ready": True,
        "device_ready": ready,
        "operations": {
            k: v
            for k, v in operations.items()
            if k
            in {
                "observe",
                "initialize",
                "draft_validate",
                "social_identity",
                "social_prepare",
                "social_submit",
                "reddit_identity",
                "reddit_prepare",
                "reddit_submit",
                "social_membership",
                "social_join",
            }
            and isinstance(v, str)
            and v in {"supported", "unsupported"}
        },
        "reason": None if ready else "DeviceObservationNotAvailable",
    }


def require_capabilities(value, operation):
    status = capability_status(value)
    if not status["software_ready"]:
        raise BackendError(status["reason"])
    if status.get("operations", {}).get(operation) != "supported":
        raise BackendError("PhoneFleetDeviceNotReady")


def is_legacy(request):
    return request.get("version") != expected_version()


def legacy_result(snapshot, request, execution_id=None):
    """Only expose the original native state, never upgrade old proof to confirmation."""
    from .models import digest

    if (
        not isinstance(snapshot, dict)
        or snapshot.get("request") != request
        or (execution_id and snapshot.get("id") != execution_id)
        or not isinstance(snapshot.get("id"), str)
    ):
        raise BackendError("PhoneFleetLegacyCorrelationMismatch")
    return {
        "status": "unknown",
        "verified": False,
        "reason": "PhoneFleetLegacyRecordReadOnly",
        "legacy_read_only": True,
        "original_execution_id": snapshot["id"],
        "original_request_sha256": digest(request),
        "reported_state": snapshot.get("status")
        if isinstance(snapshot.get("status"), str)
        and snapshot.get("status")
        in {
            "queued",
            "leased",
            "running",
            "succeeded",
            "failed",
            "unknown",
            "cancelled",
            "waiting_for_human",
        }
        else "unknown",
    }
