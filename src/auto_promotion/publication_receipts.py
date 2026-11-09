"""Consume v2 evidence from an authenticated original execution, never a public receipt upload."""


def outcome_v2(snapshot, context, proof):
    receipt = getattr(proof, "receipt_v2", None)
    if receipt is None:
        return None
    pending = {
        "status": "unknown",
        "verified": False,
        "submission_started": proof.submission_started,
        "receipt_pending": proof.submission_started,
        "reason": "PhonePublicationReceiptPending",
        "resource_state": "released" if snapshot.get("resource_released") else "fenced",
    }
    from phone_fleet_protocol import ExecutionSnapshot
    from phone_fleet_protocol.receipts import expectation_for, matches

    result = snapshot.get("result") or {}
    try:
        typed = ExecutionSnapshot.model_validate(snapshot)
        request = typed.request
        if (
            not typed.command_id
            or not typed.result
            or typed.result.execution_id != typed.id
            or typed.result.command_id != typed.command_id
            or typed.result.device_id != request.device_id
        ):
            return pending
        actual = request.social if receipt.platform != "reddit" else request.reddit
        if actual is None or actual.model_dump(mode="json") != context.model_dump(mode="json"):
            return pending
        expected = expectation_for(request, typed.id, proof.submitted_at)
    except (KeyError, TypeError, ValueError):
        return pending
    if (
        snapshot.get("status") != "succeeded"
        or result.get("simulated") is not False
        or result.get("external_effect") != "confirmed"
        or not proof.submission_started
        or not matches(receipt, expected)
    ):
        return pending
    if receipt.platform != "reddit" and proof.client_id != context.client_id:
        return pending
    identifier = receipt.native_identifier()
    return {
        "status": "confirmed",
        "verified": True,
        "external_reference": receipt.reference.url,
        "native_id": identifier,
        "native_comment_id": identifier,
        "published_at": receipt.created_at,
        "submission_started": True,
        "receipt_contract": receipt.version,
        "receipt_reader": receipt.reader,
        "receipt_evidence_sha256": receipt.evidence_sha256,
        **({"message_id": identifier} if receipt.platform == "telegram" else {}),
        **({"comment_id": identifier} if receipt.platform == "reddit" else {}),
    }
