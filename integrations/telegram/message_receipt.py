"""Read-only verification of one already submitted message. No retries or writes."""


async def verify_message_receipt(
    client, entity, message_id, expected_text, *, reply_to=None, media=False
):
    result = {"sent": True, "message_id": int(message_id), "verified": False}
    try:
        observed = await client.get_messages(entity, ids=int(message_id))
        result["verified"] = bool(
            observed
            and observed.id == int(message_id)
            and getattr(observed, "out", False)
            and (getattr(observed, "message", "") or "") == (expected_text or "")
            and (reply_to is None or getattr(observed, "reply_to_msg_id", None) == reply_to)
            and (not media or getattr(observed, "media", None) is not None)
        )
    except Exception:
        result["reason"] = "readonly_receipt_check_failed"
    return result
