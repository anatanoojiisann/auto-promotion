"""Isolated execution of the backends bundled with this package."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import sqlite3
import sys
import time

from .account_blocks import FIELDS as BLOCK_FIELDS
from .backends.common import BackendError, Receipts, account_lock, verify_claim
from .execution_diagnostics import FIELDS, exception_details
from .models import PLATFORMS, canonical
from .runtime_identity import result_runtime
from .store import Store

RUNTIME = result_runtime("native_bridge")

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
    "message_chat_id",
}
RESULT_FIELDS.update(FIELDS)
RESULT_FIELDS.update(BLOCK_FIELDS)
RESULT_FIELDS.add("progress_mirror_deferred")
RESULT_FIELDS.update(
    {
        "coverage_complete",
        "eligibility",
        "source",
        "source_status",
        "actual_cost",
        "confirmed_not_submitted",
    }
)


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
                from .final_receipts import capture

                receipt_binding = capture(request) if operation == "execute" else None
                previous = receipt.begin(key, payload, binding=receipt_binding)
                if previous is not None:
                    return {k: v for k, v in previous.items() if k in RESULT_FIELDS}
            else:
                payload["receipt"] = receipt.read(key) or payload.get("result", {})

        pending_guard = {}

        def checkpoint(result):
            nonlocal pending_guard
            guard_evidence = any(
                field in result
                for field in ("account_block_reason", "account_restriction", "browser_page_anomaly")
            )
            event = None
            if guard_evidence:
                from .account_blocks import clear_private_stop, observe, save_private_stop

                pending_guard = result
                journal_saved = central_saved = False
                try:
                    event = save_private_stop(platform, label, result)
                    journal_saved = True
                except (OSError, sqlite3.Error):
                    result["progress_mirror_deferred"] = True
                if receipt:
                    try:
                        receipt.save(key, {**(receipt.read(key) or {}), **result})
                    except (OSError, sqlite3.Error):
                        result["progress_mirror_deferred"] = True
                if request.get("database") and request.get("account_id"):
                    try:
                        with Store(request["database"], initialize=False).connect(write=True) as db:
                            observe(
                                db,
                                request["account_id"],
                                result,
                                now=time.time(),
                                operation=operation,
                                payload=payload,
                                task_id=payload.get("task_id"),
                            )
                        central_saved = True
                    except (OSError, sqlite3.Error):
                        result["progress_mirror_deferred"] = True
                if not journal_saved and not central_saved:
                    # No code can promise restart durability while every guard
                    # store is unwritable. Keep an explicit stop for the caller.
                    result["guard_persistence_failed"] = True
                    result["account_block_reason"] = "account_guard_unavailable"
                if receipt:
                    try:
                        receipt.save(key, {**(receipt.read(key) or {}), **result})
                    except (OSError, sqlite3.Error):
                        result["progress_mirror_deferred"] = True
                if central_saved and event:
                    try:
                        clear_private_stop(platform, label, event)
                    except OSError:
                        result["progress_mirror_deferred"] = True
                return
            if receipt:
                receipt.save(key, {**(receipt.read(key) or {}), **result})
            # Keep progress visible even if the provider is terminated before returning.
            if write and operation == "execute" and request.get("database"):
                details = {
                    k: result[k]
                    for k in FIELDS + BLOCK_FIELDS + ("external_reference", "reason")
                    if k in result
                }
                mirror = Store(request["database"], initialize=False)
                mirror.lock_timeout = 0.2
                mirror.lock_retry_delays = (0.05, 0.1, 0.2)
                try:
                    with mirror.connect(write=True) as db:
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
                except sqlite3.OperationalError as exc:
                    if receipt is None or not Store.locked(exc):
                        raise
                    # The synchronous, account-local receipt above is already
                    # durable. A busy shared progress mirror must not abort a
                    # reply. Submission still performs every normal claim,
                    # stop, permission, quota and lease check through authorize.
                    receipt.save(
                        key, {**(receipt.read(key) or {}), "progress_mirror_deferred": True}
                    )

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

                backend_payload = dict(payload)
                if request.get("database") and request.get("account_id"):
                    from .health_checks import probe_revision
                    from .telegram_reading import read_policy_context

                    context = {
                        "database": request["database"],
                        "account_id": request["account_id"],
                        "backend_label": label,
                    }
                    policy = read_policy_context(context)
                    with Store(request["database"], initialize=False).connect() as db:
                        account = dict(
                            db.execute(
                                "SELECT * FROM accounts WHERE id=?", (request["account_id"],)
                            ).fetchone()
                        )
                    context["identity_hash"] = account["identity_hash"]
                    backend_payload.update(
                        read_policy=policy["policy"],
                        read_policy_context=context,
                        read_connection_revision=probe_revision(account, request["database"]),
                    )
                result = call(label, operation, backend_payload, checkpoint, authorize)
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
                    **exception_details(exc),
                }
            if write:
                result = {
                    **(receipt.read(key) or {}),
                    **pending_guard,
                    "status": pending_guard.get("status", "unknown"),
                    "verified": False,
                    "reason": pending_guard.get("reason", code),
                    **exception_details(exc),
                }
            else:
                error = BackendError(code)
                details = exception_details(exc)
                error.rpc_counts = details.get("rpc_counts")
                error.rpc_count_source = details.get("rpc_count_source")
                error.read_diagnostics = {key: details[key] for key in FIELDS if key in details}
                raise error from None
        result = {**RUNTIME, **result}
        if write:
            progress_fields = {
                field: value
                for field, value in (receipt.read(key) or {}).items()
                if field in FIELDS
            }
            result = {**progress_fields, **result}
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
                    result.pop("confirmed_not_submitted", None)
            checkpoint(result)
            if operation == "execute" and receipt_binding:
                from .models import digest

                # A provider checkpoint is not a final result. Publish final proof
                # only after the provider has returned and normalization completed.
                final_result = {k: v for k, v in result.items() if k in RESULT_FIELDS}
                finalized_at = time.time()
                receipt.save(
                    key,
                    {
                        **(receipt.read(key) or {}),
                        "receipt_binding": receipt_binding,
                        "receipt_finalized_at": finalized_at,
                        "receipt_final_result": final_result,
                        "receipt_final_hash": digest(
                            {
                                "binding": receipt_binding,
                                "result": final_result,
                                "finalized_at": finalized_at,
                            }
                        ),
                    },
                )
        return {k: v for k, v in result.items() if k in RESULT_FIELDS}


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
            else "BackendError",
            **exception_details(exc),
        }
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
