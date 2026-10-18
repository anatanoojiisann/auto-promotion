"""Isolated execution of the backends bundled with this package."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sqlite3
import sys
import time

from .backends.common import BackendError, Receipts, account_lock, verify_claim
from .execution_diagnostics import FIELDS, exception_details
from .models import PLATFORMS, canonical

OPERATIONS = {
    "health",
    "preflight",
    "discover",
    "execute",
    "verify",
    "join",
    "verify_join",
    "targets",
    "feedback",
    "content",
    "insights",
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
    "published_at",
    "scheduled_message_ids",
    "feedback_events",
    "feedback_checked_through",
    "feedback_cursor",
    "feedback_status",
    "content_items",
    "publish_id",
    "post_ids",
    "comment_id",
    "platform_status",
    "insights",
    "next_cursor",
    "has_more",
    "mutation_accepted",
}
RESULT_FIELDS.update(FIELDS)


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
                receipt.save(key, {**(receipt.read(key) or {}), **result})
            # Keep progress visible even if the provider is terminated before returning.
            if write and operation == "execute" and request.get("database"):
                details = {
                    k: result[k] for k in FIELDS + ("external_reference", "reason") if k in result
                }
                with contextlib.closing(sqlite3.connect(request["database"], timeout=10)) as db:
                    with db:
                        row = db.execute(
                            "SELECT result FROM items WHERE id=? AND attempt_id=? AND status='executing'",
                            (payload.get("item_id"), payload.get("attempt_id")),
                        ).fetchone()
                        if row:
                            db.execute(
                                "UPDATE items SET result=?,external_reference=COALESCE(?,external_reference) "
                                "WHERE id=? AND attempt_id=? AND status='executing'",
                                (
                                    canonical({**json.loads(row[0]), **details}),
                                    details.get("external_reference"),
                                    payload.get("item_id"),
                                    payload.get("attempt_id"),
                                ),
                            )
                        else:
                            raise BackendError("ExecutionClaimNotActive")

        def progress(step, submission_started=None):
            details = {"execution_step": step, "step_started_at": time.time()}
            if submission_started is not None:
                details["submission_started"] = submission_started
            checkpoint(details)

        checkpoint.progress = progress
        if write:
            progress("backend_start", False)

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
            elif platform == "tiktok":
                from .backends.tiktok import call

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
                    **exception_details(exc),
                }
            else:
                raise BackendError(code) from None
        result = {key: value for key, value in result.items() if key in RESULT_FIELDS}
        if operation == "verify" and result.get("verified") is True:
            checkpoint(result)
        if write:
            accepted = receipt.read(key) or {}
            if (
                accepted.get("message_ids")
                or accepted.get("media_id")
                or accepted.get("external_reference")
                or accepted.get("publish_id")
            ):
                result = {**accepted, **result}
                if result.get("status") == "failed_known" and not (
                    platform == "tiktok" and result.get("verified") is True
                ):
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
