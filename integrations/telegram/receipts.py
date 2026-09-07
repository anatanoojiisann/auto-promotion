"""Small read-only receipt facade for the unified promotion control plane."""

from telegram_mcp.runtime import *
from telegram_mcp.message_receipt import verify_message_receipt


@mcp.tool(annotations=ToolAnnotations(title="Verify Sent Message", readOnlyHint=True))
@with_account(readonly=True)
@validate_id("chat_id")
async def verify_sent_message(
    chat_id: Union[int, str],
    message_id: int,
    expected_text: str,
    reply_to: Optional[int] = None,
    media: bool = False,
    account: str = None,
) -> str:
    client = get_client(account)
    entity = await resolve_entity(chat_id, client)
    result = await verify_message_receipt(
        client, entity, message_id, expected_text, reply_to=reply_to, media=media
    )
    return json.dumps(result)


@mcp.tool(annotations=ToolAnnotations(title="Promotion Account Health", readOnlyHint=True))
async def promotion_account_health(account: str = None) -> str:
    from telegram_mcp.task_executor import inspect_account_capabilities

    result = await inspect_account_capabilities(account or "default", "outbound")
    return json.dumps(
        {
            "identity_verified": bool(result["account_status"].get("ready")),
            "healthy": bool(
                result["account_status"].get("ready") and result["send_content"].get("available")
            ),
            "service_ready": True,
            "reason": result["send_content"].get("reason", "checked"),
        }
    )


@mcp.tool(
    annotations=ToolAnnotations(
        title="Join Public Promotion Group", openWorldHint=True, destructiveHint=True
    )
)
@with_account(readonly=False)
async def join_public_promotion_group(
    target: str, min_interval: int = 120, account: str = None
) -> str:
    """Transport for a centrally approved and already-claimed qualified public target."""
    from telegram_mcp.task_executor import join_task_item

    client = get_client(account)
    entity = await resolve_entity(target, client)
    if not getattr(entity, "username", None) or not getattr(entity, "megagroup", False):
        return json.dumps({"status": "failed_known", "reason": "public_group_required"})
    status, result = await join_task_item(
        {"target_ref": target}, account or "default", max(120, min_interval), 3600
    )
    safe = {
        key: result[key]
        for key in ("verified", "can_send", "outbound_eligible_at_utc")
        if key in result
    }
    safe["status"] = (
        "joined"
        if status == "suppressed" and result.get("verified")
        else status
        if status in {"joined", "already_joined", "pending_admin_approval", "unknown"}
        else "failed_known"
    )
    if result.get("retry_at_epoch"):
        import time

        safe.update(
            rate_limited=True,
            retry_after_seconds=max(1, int(result["retry_at_epoch"] - time.time())),
        )
    return json.dumps(safe)


@mcp.tool(annotations=ToolAnnotations(title="Verify Promotion Membership", readOnlyHint=True))
@with_account(readonly=True)
async def verify_promotion_membership(target: str, account: str = None) -> str:
    from telegram_mcp.task_executor import inspect_joined_target_permissions
    from datetime import datetime, timedelta, timezone

    client = get_client(account)
    entity = await resolve_entity(target, client)
    try:
        result = await client(
            functions.channels.GetParticipantRequest(channel=entity, participant="me")
        )
    except telethon.errors.rpcerrorlist.UserNotParticipantError:
        return json.dumps({"verified": False, "status": "unknown"})
    if type(getattr(result, "participant", None)).__name__ not in {
        "ChannelParticipant",
        "ChannelParticipantSelf",
        "ChannelParticipantAdmin",
        "ChannelParticipantCreator",
    }:
        return json.dumps({"verified": False, "status": "unknown"})
    permission = await inspect_joined_target_permissions(entity, client)
    return json.dumps(
        {
            "status": "joined",
            "verified": True,
            "can_send": permission.get("can_send", False),
            "outbound_eligible_at_utc": (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat(),
        }
    )


__all__ = [
    "verify_sent_message",
    "promotion_account_health",
    "join_public_promotion_group",
    "verify_promotion_membership",
]
