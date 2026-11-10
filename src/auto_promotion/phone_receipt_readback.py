"""Consume a fleet-verified sidecar while preserving the original unknown execution."""

from urllib.parse import quote

import httpx
from phone_fleet_protocol import fingerprint
from phone_fleet_protocol.receipt_readback import ReceiptReadbackProof
from phone_fleet_protocol.social import receipt_matches

from .backends.common import BackendError


def reconcile_x_receipt(service, request, snapshot):
    result = snapshot.get("result") or {}
    original = result.get("social") or {}
    context = request.social
    if (
        context.platform != "x"
        or snapshot.get("status") != "unknown"
        or result.get("simulated") is not False
        or original.get("submission_started") is not True
        or not original.get("submitted_at")
    ):
        return None
    route = "/executions/" + quote(snapshot["id"], safe="") + "/receipt-readback"
    try:
        value = service.request("POST", route, {})
    except (BackendError, httpx.HTTPError, ValueError):
        # A cooldown, lost response or exhausted read budget may still have a
        # durable prior witness. Retrieve it without scheduling any new read.
        try:
            value = service.request("GET", route, {})
        except (BackendError, httpx.HTTPError, ValueError):
            return None
    if not isinstance(value, dict):
        return None
    pending = {
        "status": "unknown",
        "verified": False,
        "submission_started": True,
        "receipt_pending": True,
        "reason": "PhoneXOriginalReceiptReadbackPending",
        "original_runtime_status": "unknown",
        "reconciliation_source": "x_native_readback",
    }
    if value.get("phase") != "confirmed":
        proof = value.get("proof") or {}
        return {**pending, "receipt_rejection": proof.get("reason", "pending")}
    try:
        proof = ReceiptReadbackProof.model_validate(value["proof"])
        receipt = proof.receipt
        valid = (
            receipt
            and proof.execution_id == snapshot["id"]
            and proof.command_id == snapshot["command_id"]
            and proof.device_id == snapshot["device_id"]
            and proof.node_id == snapshot["node_id"]
            and proof.context_sha256 == fingerprint(context)
            and proof.nonce == value.get("nonce")
            and proof.original_result_sha256 == fingerprint(original)
            and proof.baseline_observed_at <= original["submitted_at"] + 1
            and receipt.created_at <= request.deadline + 2
            and receipt_matches(receipt, context, original["submitted_at"])
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        return {**pending, "reason": "PhoneXOriginalReceiptReadbackMismatch"}
    return {
        "status": "confirmed",
        "verified": True,
        "external_reference": receipt.permalink,
        "native_id": receipt.native_id,
        "native_comment_id": receipt.native_id,
        "publish_id": receipt.native_id,
        "reply_id": receipt.native_id,
        "published_at": receipt.created_at,
        "submission_started": True,
        "reconciliation_source": "x_native_readback",
        "original_runtime_status": "unknown",
        "device_reconciliation_required": not snapshot.get("resource_released", False),
    }
