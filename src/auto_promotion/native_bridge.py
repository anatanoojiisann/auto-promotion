"""Isolated execution of the backends bundled with this package."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sys

from .backends.common import BackendError, Receipts, account_lock, verify_claim
from .models import PLATFORMS

OPERATIONS = {
    "health",
    "preflight",
    "discover",
    "execute",
    "verify",
    "join",
    "verify_join",
    "targets",
}
RESULT_FIELDS = {
    "status",
    "healthy",
    "service_ready",
    "identity_verified",
    "account_identity",
    "permissions",
    "reason",
    "allowed",
    "candidates",
    "source_note",
    "verified",
    "external_reference",
    "message_ids",
    "lookup_message_ids",
    "scheduled",
    "active",
    "can_send",
    "outbound_eligible_at_utc",
    "rate_limited",
    "risk_warning",
    "retry_after_seconds",
    "target_verified",
    "container_id",
    "media_id",
    "targets",
    "truncated",
}


def dispatch(request):
    platform, label, operation = request["platform"], request["account"], request["operation"]
    if operation not in OPERATIONS or platform not in PLATFORMS:
        raise BackendError("UnsupportedPlatformOperation")
    if operation == "join" and platform != "telegram":
        raise BackendError("UnsupportedPlatformOperation")
    payload = dict(request.get("payload", {}))
    write = operation in {"execute", "join"}

    def authorize():
        return verify_claim(request)

    if write:
        authorize()
    with account_lock(platform, label):
        receipt = None
        key = ("join:" if operation in {"join", "verify_join"} else "execute:") + str(
            payload.get("candidate_id")
            if operation in {"join", "verify_join"}
            else payload.get("item_id")
        )
        if write or operation == "verify":
            receipt = Receipts(platform, label)
            if write:
                previous = receipt.begin(key, payload)
                if previous is not None:
                    return previous
            else:
                payload["receipt"] = receipt.read(key) or payload.get("result", {})

        def checkpoint(result):
            if receipt:
                receipt.save(key, result)

        try:
            if platform == "telegram":
                from .backends.telegram import call

                result = call(label, operation, payload, checkpoint, authorize)
            elif platform == "instagram":
                from .backends.instagram import call

                result = call(label, operation, payload, checkpoint, authorize)
            elif platform == "discord":
                from .backends.discord import call

                result = call(label, operation, payload, checkpoint, authorize)
            else:
                from .backends.browser import call

                result = call(platform, label, operation, payload, checkpoint, authorize)
        except Exception as exc:
            code = getattr(exc, "safe_code", type(exc).__name__)
            if not isinstance(code, str) or not code.isidentifier():
                code = "BackendError"
            if operation == "health":
                return {
                    "healthy": False,
                    "service_ready": False,
                    "identity_verified": False,
                    "reason": code,
                }
            if write:
                result = {
                    **(receipt.read(key) or {}),
                    "status": "unknown",
                    "verified": False,
                    "reason": code,
                }
            else:
                raise BackendError(code) from None
        result = {key: value for key, value in result.items() if key in RESULT_FIELDS}
        if write:
            accepted = receipt.read(key) or {}
            if (
                accepted.get("message_ids")
                or accepted.get("media_id")
                or accepted.get("external_reference")
            ):
                result = {**accepted, **result}
                if result.get("status") == "failed_known":
                    result["status"] = "unknown"
            checkpoint(result)
        return result


def main():
    logging.disable(logging.CRITICAL)
    request = json.load(sys.stdin)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = dispatch(request)
    except Exception as exc:
        code = getattr(exc, "safe_code", type(exc).__name__)
        result = {
            "bridge_error": code
            if isinstance(code, str) and code.isidentifier()
            else "BackendError"
        }
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
