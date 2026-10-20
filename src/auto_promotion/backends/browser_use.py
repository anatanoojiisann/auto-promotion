"""Expiring identity observations from an interactive Browser Use session.

Browser Use belongs to the desktop agent, not the unattended worker. This
connection records an observed identity without exporting browser credentials.
It cannot submit platform actions or turn an old observation into a live check.
"""

import math
import re
import time
from uuid import UUID

from .common import BackendError

OBSERVATION_MAX_AGE = 300


def call(platform, settings, operation):
    if platform != "x":
        raise BackendError("UnsupportedPlatformOperation")
    if operation == "preflight":
        return {"allowed": False, "reason": "BrowserUseInteractiveSessionRequired"}
    if operation != "health":
        raise BackendError("BrowserUseInteractiveSessionRequired")

    expected = settings.get("identity", "")
    observation = settings.get("browser_use_observation")
    if not re.fullmatch(r"[a-z0-9_]{1,15}", expected) or not isinstance(observation, dict):
        raise BackendError("InvalidBrowserUseConfiguration")
    try:
        UUID(settings["browser_instance_id"])
        if settings["browser_family"] not in {"chrome", "edge"} or not re.fullmatch(
            r"[1-9][0-9]*", settings["browser_tab_id"]
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        raise BackendError("InvalidBrowserUseConfiguration") from None

    observed_at = observation.get("observed_at")
    fresh = (
        type(observed_at) in {int, float}
        and math.isfinite(observed_at)
        and 0 <= time.time() - observed_at <= OBSERVATION_MAX_AGE
    )
    matches = (
        observation.get("handles") == [expected]
        and str(observation.get("profile_href", "")).casefold() == "/" + expected
    )
    verified = bool(fresh and matches)
    return {
        "healthy": verified,
        "service_ready": verified,
        "identity_verified": verified,
        "account_identity": expected if verified else None,
        "permissions": [],
        "reason": (
            "browser_use_recent_identity_observation" if verified else "BrowserUseRecheckRequired"
        ),
    }
