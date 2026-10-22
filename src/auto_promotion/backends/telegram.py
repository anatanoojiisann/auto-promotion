"""Telegram user-account backend. No external MCP server or sibling checkout."""

from __future__ import annotations

import asyncio
import getpass
import math
import time
from datetime import datetime, timedelta, timezone

from .common import (
    BackendError,
    language_evidence,
    observed_timestamp,
    profile_path,
    read_profile,
    save_profile,
)


def make_client(label, settings, *, for_login=False):
    from telethon import TelegramClient

    proxy = settings.get("proxy")
    return TelegramClient(
        str(profile_path("telegram", label) / "telegram.session"),
        int(settings["api_id"]),
        settings["api_hash"],
        proxy=proxy,
        # Login must follow PHONE_MIGRATE/NETWORK_MIGRATE to the account's DC.
        # Limit this to authentication; outbound clients keep a single attempt.
        request_retries=2 if for_login else 0,
        # Human authentication can outlast an idle proxy connection. Restore only
        # login transports; promotion clients must never replay pending writes.
        connection_retries=2 if for_login else 0,
        auto_reconnect=for_login,
        flood_sleep_threshold=0,
        raise_last_call_error=True,
        receive_updates=False,
    )


async def login(label):
    """Interactive authentication only; never prints phone, codes, hashes or session strings."""
    try:
        settings = read_profile("telegram", label)
    except BackendError as exc:
        if exc.safe_code != "AccountNotConfigured":
            raise
        settings = {
            "api_id": int(getpass.getpass("Telegram API ID (hidden): ")),
            "api_hash": getpass.getpass("Telegram API hash (hidden): ").strip(),
        }
        proxy_url = input("SOCKS5 proxy host:port (blank for direct connection): ").strip()
        if proxy_url:
            host, port = proxy_url.rsplit(":", 1)
            settings["proxy"] = {"proxy_type": "socks5", "addr": host, "port": int(port)}
    root = profile_path("telegram", label)
    root.mkdir(parents=True, exist_ok=True)
    client = make_client(label, settings, for_login=True)

    async def prompt(text):
        # Keep the network loop alive while the person reads a code or password.
        return await asyncio.to_thread(getpass.getpass, text)

    try:
        await client.start(
            phone=lambda: prompt("Telegram phone number (hidden): "),
            code_callback=lambda: prompt("Telegram login code (hidden): "),
            password=lambda: prompt("Telegram two-step password (hidden): "),
        )
        me = await client.get_me()
        if not me or getattr(me, "bot", False):
            raise BackendError("TelegramUserAccountRequired")
        settings["identity"] = str(me.id)
        save_profile("telegram", label, settings)
        return {"platform": "telegram", "account": label, "authenticated": True}
    finally:
        await client.disconnect()


def message_matches(message, item, *, scheduled=False):
    if not message or not getattr(message, "out", False):
        return False
    if (getattr(message, "message", "") or "") != item["text"]:
        return False
    if item.get("reply_to") is not None:
        if getattr(message, "reply_to_msg_id", None) != item["reply_to"]:
            return False
    if item["media"] and not getattr(message, "media", None):
        return False
    if scheduled:
        expected = datetime.fromisoformat(item["scheduled_at"].replace("Z", "+00:00"))
        observed = getattr(message, "date", None)
        if not observed or abs((observed - expected).total_seconds()) > 1:
            return False
    return True


class TelegramBackend:
    def __init__(self, label, *, client_factory=make_client):
        self.label = label
        self.settings = read_profile("telegram", label)
        self.client_factory = client_factory

    async def run(self, operation, payload, checkpoint, authorize):
        # native_bridge has already loaded the durable account-local receipt.
        # Without exact IDs no read can conclusively recover an uncertain send.
        if operation == "verify":
            receipt = payload.get("receipt") or payload.get("result") or {}
            ids = receipt.get("message_ids")
            if (
                not isinstance(ids, list)
                or not ids
                or any(
                    not isinstance(value, int) or isinstance(value, bool) or value <= 0
                    for value in ids
                )
            ):
                return {"status": "unknown", "verified": False, "reason": "no_durable_receipt"}
        client = self.client_factory(self.label, self.settings)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return self.health(False, "TelegramLoginRequired")
            me = await client.get_me()
            if (
                not me
                or str(me.id) != self.settings["identity"]
                or getattr(me, "bot", False)
                or getattr(me, "restricted", False)
                or getattr(me, "deleted", False)
            ):
                return self.health(False, "TelegramIdentityMismatchOrRestricted")
            if operation == "health":
                return {
                    **self.health(True, "account_read_verified"),
                    "account_identity": str(me.id),
                }
            if operation == "preflight":
                item = payload["payload"]
                target = await client.get_entity(self.target(item["target"]))
                permissions = await client.get_permissions(target, "me")
                allowed = self.can_send_messages(target, permissions)
                return {
                    "allowed": allowed,
                    **({"reason": "TargetWriteForbidden"} if not allowed else {}),
                }
            if operation == "discover":
                return await self.discover(client, payload["policy"])
            if operation in {"join", "verify_join"}:
                return await self.membership(client, operation, payload, checkpoint, authorize)
            if operation == "feedback":
                return await self.feedback(client, payload)
            if operation == "content":
                return await self.content(client, payload)
            return await self.outbound(client, operation, payload, checkpoint, authorize)
        except Exception as exc:
            from telethon import errors

            if isinstance(exc, (errors.FloodWaitError, errors.SlowModeWaitError)):
                return {
                    "status": "failed_known",
                    "verified": False,
                    "reason": type(exc).__name__,
                    "rate_limited": True,
                    "retry_after_seconds": max(1, int(exc.seconds)),
                }
            if isinstance(
                exc,
                (errors.UserDeactivatedBanError, errors.PeerFloodError, errors.UserRestrictedError),
            ):
                return {
                    "status": "failed_known",
                    "verified": False,
                    "reason": type(exc).__name__,
                    "risk_warning": True,
                    "retry_after_seconds": 86400,
                }
            raise
        finally:
            await client.disconnect()

    @staticmethod
    def health(healthy, reason):
        from ..models import CAPABILITIES

        return {
            "healthy": healthy,
            "service_ready": True,
            "identity_verified": healthy,
            "permissions": sorted(CAPABILITIES["telegram"]) if healthy else [],
            "reason": reason,
        }

    @staticmethod
    def target(value):
        # Central validation handles outgoing targets; the provider also rejects private invites.
        value = value.removeprefix("https://t.me/").removeprefix("http://t.me/")
        if value.startswith(("+", "joinchat/")):
            raise BackendError("PrivateInviteNotSupported")
        return value

    async def discover(self, client, policy):
        from telethon import functions

        rows = {}
        maximum = policy["max_candidates"]
        for query in policy["queries"]:
            response = await client(functions.contacts.SearchRequest(q=query, limit=maximum))
            for chat in response.chats:
                username = getattr(chat, "username", None)
                if not username or not getattr(chat, "megagroup", False):
                    continue
                key = "@" + username
                if key in rows:
                    continue
                full = await client(functions.channels.GetFullChannelRequest(chat))
                about = getattr(full.full_chat, "about", "") or ""
                recent = await client.get_messages(chat, limit=20)
                cutoff = datetime.now(timezone.utc) - timedelta(days=1)
                activity = sum(bool(m.date and m.date >= cutoff) for m in recent)
                evidence = (
                    chat.title + " " + about + " " + " ".join((m.message or "") for m in recent)
                )
                language, confidence = language_evidence(evidence)
                rows[key] = {
                    "target": key,
                    "title": chat.title,
                    "text": about,
                    "language": language,
                    "confidence": confidence,
                    "members": getattr(full.full_chat, "participants_count", 0) or 0,
                    "activity": activity,
                    "is_public": True,
                }
                if len(rows) >= maximum:
                    break
            if len(rows) >= maximum:
                break
        return {
            "candidates": list(rows.values()),
            "source_note": "公开群资料及最近24小时样本；语言证据不足的候选保守拒绝",
        }

    async def content(self, client, payload):
        from telethon import utils

        entity = await client.get_entity(self.target(payload["target"]))
        if not getattr(entity, "username", None) or not getattr(entity, "megagroup", False):
            raise BackendError("PublicTelegramGroupRequired")
        limit = int(payload.get("limit", 200))
        if not 1 <= limit <= 1000:
            raise BackendError("InvalidContentLimit")
        messages = await client.get_messages(entity, limit=limit + 1)
        items = []
        for message in messages[:limit]:
            text = getattr(message, "message", "") or ""
            if not text.strip() or getattr(message, "out", False):
                continue
            language, confidence = language_evidence(text)
            items.append(
                {
                    "platform": "telegram",
                    "external_id": str(message.id),
                    "native_target_id": str(utils.get_peer_id(entity)),
                    "target": "@" + entity.username,
                    "action": "reply",
                    "reply_to": message.id,
                    "author_id": str(getattr(message, "sender_id", "") or ""),
                    "title": text[:100],
                    "context": text[:40000],
                    "language": language,
                    "confidence": confidence,
                    "is_public": True,
                    "permission": "unknown",
                    "published_at": message.date.isoformat()
                    if getattr(message, "date", None)
                    else None,
                }
            )
        return {"content_items": items, "truncated": len(messages) > limit}

    async def membership(self, client, operation, payload, checkpoint, authorize):
        from telethon import errors, functions

        entity = await client.get_entity(self.target(payload["target"]))
        if not getattr(entity, "username", None) or not getattr(entity, "megagroup", False):
            return {"status": "failed_known", "reason": "public_group_required"}
        already = False
        try:
            observed = await client(functions.channels.GetParticipantRequest(entity, "me"))
            already = self.is_member(observed)
        except errors.UserNotParticipantError:
            pass
        if not already and operation == "verify_join":
            return {"status": "unknown", "verified": False}
        if not already:
            authorize()
            try:
                await client(functions.channels.JoinChannelRequest(entity))
                checkpoint({"status": "unknown", "verified": False})
            except errors.InviteRequestSentError:
                return {"status": "pending_admin_approval", "verified": False}
            observed = await client(functions.channels.GetParticipantRequest(entity, "me"))
            if not self.is_member(observed):
                return {"status": "unknown", "verified": False}
        permission = await client.get_permissions(entity, "me")
        return {
            "status": "already_joined" if already and operation == "join" else "joined",
            "verified": True,
            "can_send": self.can_send_messages(entity, permission),
            "outbound_eligible_at_utc": (
                datetime.now(timezone.utc) + timedelta(hours=1)
            ).isoformat(),
            "external_reference": "telegram:membership:" + str(entity.id),
        }

    @staticmethod
    def is_member(observed):
        return type(getattr(observed, "participant", None)).__name__ in {
            "ChannelParticipant",
            "ChannelParticipantSelf",
            "ChannelParticipantAdmin",
            "ChannelParticipantCreator",
        }

    @staticmethod
    def can_send_messages(entity, permissions):
        """Resolve group write access from Telegram's ban rights and membership state."""
        if getattr(entity, "broadcast", False):
            return bool(getattr(permissions, "post_messages", False))
        participant = getattr(permissions, "participant", None)
        default_ban = getattr(entity, "default_banned_rights", None)
        individual_ban = getattr(participant, "banned_rights", None)
        return bool(
            not permissions.is_banned
            and not permissions.has_left
            and not getattr(default_ban, "send_messages", False)
            and not getattr(individual_ban, "send_messages", False)
        )

    async def outbound(self, client, operation, payload, checkpoint, authorize):
        from telethon import functions

        item = payload["payload"]
        entity = await client.get_entity(self.target(item["target"]))
        native = item["action"] in {"schedule_message", "schedule_file"}
        result = payload.get("receipt") or payload.get("result", {})
        if operation == "execute":
            if item["action"] not in {
                "send_message",
                "reply",
                "send_file",
                "schedule_message",
                "schedule_file",
            }:
                raise BackendError("UnsupportedTelegramAction")
            # Permissions may change after the queue's preflight. Recheck using
            # this exact session before the durable submission boundary. The
            # server still decides races between this read and the final RPC.
            from telethon import errors

            try:
                permissions = await client.get_permissions(entity, "me")
            except (
                errors.ChannelPrivateError,
                errors.UserNotParticipantError,
                errors.ChatWriteForbiddenError,
                errors.ChatAdminRequiredError,
            ) as exc:
                return {"status": "failed_known", "verified": False, "reason": type(exc).__name__}
            if not self.can_send_messages(entity, permissions):
                return {
                    "status": "failed_known",
                    "verified": False,
                    "reason": "TargetWriteForbidden",
                }
            schedule = (
                datetime.fromisoformat(item["scheduled_at"].replace("Z", "+00:00"))
                if native
                else None
            )
            progress = getattr(checkpoint, "progress", None)
            if progress:
                progress("platform_submission", True)
            else:
                checkpoint({"execution_step": "platform_submission", "submission_started": True})
            authorize()
            if item["media"]:
                sent = await client.send_file(
                    entity,
                    item["media"][0]["path"],
                    caption=item["text"],
                    parse_mode=None,
                    schedule=schedule,
                )
            else:
                sent = await client.send_message(
                    entity,
                    item["text"],
                    parse_mode=None,
                    link_preview=False,
                    reply_to=item.get("reply_to"),
                    schedule=schedule,
                )
            ids = [sent.id] if sent and getattr(sent, "id", None) else []
            result = {
                "status": "unknown",
                "verified": False,
                "message_ids": ids,
                "external_reference": f"telegram:{item['target']}:{','.join(map(str, ids))}",
            }
            # Save the accepted ID before any network verification, even if verification crashes.
            checkpoint(result)
        ids = result.get("message_ids", [])
        if not ids:
            return {"status": "unknown", "verified": False, "reason": "no_durable_receipt"}
        if native:
            first = await client(functions.messages.GetScheduledHistoryRequest(entity, 0))
            found = {m.id: m for m in first.messages}
            matched = all(
                i in found and message_matches(found[i], item, scheduled=True) for i in ids
            )
            second = await client(functions.messages.GetScheduledHistoryRequest(entity, 0))
            lookup = [m.id for m in second.messages if message_matches(m, item, scheduled=True)]
            active = matched and set(ids) <= set(lookup)
            expected = datetime.fromisoformat(item["scheduled_at"].replace("Z", "+00:00"))
            if operation == "verify" and not active and expected <= datetime.now(timezone.utc):
                return await self.scheduled_delivery(client, entity, item, result, expected)
            return {
                **result,
                "status": "scheduled" if active else "unknown",
                "scheduled": active,
                "active": active,
                "verified": active,
                "lookup_message_ids": lookup,
                "reason": "scheduled_receipt_verified"
                if active
                else "schedule_not_conclusively_verified",
            }
        observed = await client.get_messages(entity, ids=ids)
        if not isinstance(observed, (list, tuple)):
            observed = [observed]
        by_id = {m.id: m for m in observed if m}
        verified = all(i in by_id and message_matches(by_id[i], item) for i in ids)
        dates = [observed_timestamp(getattr(m, "date", None)) for m in by_id.values()]
        published = min(dates) if dates and all(d is not None for d in dates) else None
        return {
            **result,
            "status": "confirmed" if verified else "unknown",
            "verified": verified,
            "published_at": published if verified else None,
        }

    async def scheduled_delivery(self, client, entity, item, receipt, expected):
        # Queue IDs belong to a different ID sequence. Confirm one unique sent
        # message, including the server's from_scheduled flag, in a bounded window.
        if item["media"]:
            return {
                **receipt,
                "status": "unknown",
                "verified": False,
                "reason": "scheduled_media_delivery_requires_receipt_mapping",
            }
        lower, upper = expected - timedelta(minutes=2), expected + timedelta(minutes=2)
        history = await client.get_messages(entity, limit=201, offset_date=upper)
        if len(history) >= 201 and getattr(history[-1], "date", upper) > lower:
            return {
                **receipt,
                "status": "unknown",
                "verified": False,
                "reason": "scheduled_delivery_lookup_truncated",
            }
        candidates = [
            m
            for m in history
            if message_matches(m, item)
            and getattr(m, "from_scheduled", False)
            and getattr(m, "date", None)
            and lower <= m.date < upper
        ]
        if len(candidates) != 1:
            return {
                **receipt,
                "status": "unknown",
                "verified": False,
                "reason": "scheduled_delivery_not_unique",
            }
        message_id = candidates[0].id
        observed = await client.get_messages(entity, ids=[message_id])
        matches = [
            m
            for m in observed
            if m
            and m.id == message_id
            and message_matches(m, item)
            and getattr(m, "from_scheduled", False)
            and getattr(m, "date", None)
            and lower <= m.date < upper
        ]
        if len(matches) != 1:
            return {
                **receipt,
                "status": "unknown",
                "verified": False,
                "reason": "scheduled_delivery_not_verified",
            }
        return {
            "status": "confirmed",
            "verified": True,
            "scheduled_message_ids": receipt["message_ids"],
            "message_ids": [message_id],
            "published_at": observed_timestamp(matches[0].date),
            "external_reference": f"telegram:{item['target']}:{message_id}",
            "reason": "scheduled_delivery_verified",
        }

    async def feedback(self, client, payload):
        item, result = payload["payload"], payload["result"]
        entity = await client.get_entity(self.target(item["target"]))
        ids = result.get("message_ids", [])
        if len(ids) != 1:
            return {"verified": False, "reason": "feedback_requires_verified_message_id"}
        parent_id = ids[0]
        observed = await client.get_messages(entity, ids=[parent_id])
        parent = observed[0] if len(observed) == 1 else None
        parent_missing = parent is None or type(parent).__name__ == "MessageEmpty"
        published = observed_timestamp(result.get("published_at"))
        # A removed parent does not erase an already verified publication. Keep
        # observing direct replies using its exact durable receipt, while leaving
        # coverage partial because the parent can no longer be independently read.
        durable_parent = (
            result.get("verified") is True
            and result.get("status") == "confirmed"
            and isinstance(parent_id, int)
            and not isinstance(parent_id, bool)
            and parent_id > 0
            and result.get("external_reference") == f"telegram:{item['target']}:{parent_id}"
            and isinstance(published, (int, float))
            and math.isfinite(published)
            and 0 < published <= time.time()
        )
        if parent_missing and not durable_parent:
            return {"verified": False, "reason": "feedback_parent_unavailable"}
        if not parent_missing and not message_matches(parent, item):
            return {"verified": False, "reason": "feedback_parent_not_verified"}
        checked_through = time.time()
        cursor = max(int(payload.get("feedback_cursor", 0)), parent_id)
        messages = [
            m async for m in client.iter_messages(entity, min_id=cursor, reverse=True, limit=1001)
        ]
        complete = len(messages) < 1001 and not parent_missing
        events = []
        for message in messages[:1000]:
            occurred = observed_timestamp(getattr(message, "date", None))
            if occurred is None:
                complete = False
                break
            if occurred > checked_through:
                break
            cursor = max(cursor, message.id)
            if (
                getattr(message, "reply_to_msg_id", None) == parent_id
                and not getattr(message, "out", False)
                and str(getattr(message, "sender_id", "")) != self.settings["identity"]
                and occurred >= (published or 0)
            ):
                events.append({"event_id": f"{entity.id}:{message.id}", "occurred_at": occurred})
        return {
            "verified": True,
            "feedback_events": events,
            "feedback_checked_through": checked_through,
            "feedback_cursor": cursor,
            "feedback_status": "complete" if complete else "partial",
            **(
                {"status": "unknown", "reason": "feedback_parent_unavailable"}
                if parent_missing
                else {}
            ),
        }


def call(label, operation, payload, checkpoint, authorize):
    return asyncio.run(TelegramBackend(label).run(operation, payload, checkpoint, authorize))
