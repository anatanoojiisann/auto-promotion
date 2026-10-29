"""Telegram user-account backend. No external MCP server or sibling checkout."""

from __future__ import annotations

import asyncio
import getpass
import math
import time
import uuid
from datetime import datetime, timedelta, timezone

from ..models import digest
from .common import (
    BackendError,
    language_evidence,
    observed_timestamp,
    profile_path,
    read_profile,
    save_profile,
)
from .telegram_reads import ReadBudgetExceeded, TelegramReads


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
        self.accepted_receipt = payload.get("receipt") or payload.get("result") or {}
        self.rpc_counts = {}
        self.known_rejection = False
        self.teardown_error = None
        self.rpc_count_source = "unavailable"
        self.reads = None
        self.submission_started = bool(self.accepted_receipt.get("submission_started"))
        self.read_identity_hash = None
        self.read_peer_id = None
        self.last_rpc_error = None
        self.mutation_started = False
        try:
            result = await self._run(operation, payload, checkpoint, authorize)
            if result.get("status") == "failed_known" and (
                self.accepted_receipt.get("message_ids")
                or self.accepted_receipt.get("external_reference")
            ):
                result = {**self.accepted_receipt, **result, "status": "unknown", "verified": False}
            return self.read_diagnostics(result, operation, payload)
        except ReadBudgetExceeded as exc:
            result = self.incomplete(operation, payload, "budget_exhausted", exc.safe_code)
            if (
                operation in {"execute", "join"}
                and self.rpc_count_source == "telethon_logical_call_attempt"
                and not self.mutation_started
                and not self.accepted_receipt
            ):
                # Telethon helpers can resolve a peer *inside* send_message.
                # A denied read before the first mutation _call proves this
                # attempt did not submit, despite its earlier durable marker.
                result.update(
                    status="failed_known", confirmed_not_submitted=True, submission_started=False
                )
            result.update(local_read_budget=True, read_retry_after_seconds=exc.retry_after)
            return self.read_diagnostics(result, operation, payload)
        except Exception as exc:
            exc.rpc_counts = dict(self.rpc_counts)
            exc.rpc_count_source = self.rpc_count_source
            if self.reads:
                exc.read_diagnostics = self.reads.diagnostics()
            if isinstance(exc, BackendError) and exc.safe_code in {
                "TelegramReadStateUnavailable",
                "TelegramReadPolicyUnavailable",
                "TelegramReadClockUnavailable",
                "TelegramReadCacheInvalid",
            }:
                return self.read_diagnostics(
                    self.incomplete(operation, payload, "read_state_unavailable", exc.safe_code),
                    operation,
                    payload,
                )
            if operation in {"content", "discover", "feedback"} and isinstance(
                exc, (ConnectionError, TimeoutError, OSError)
            ):
                return self.read_diagnostics(
                    self.incomplete(operation, payload, "network_error", type(exc).__name__),
                    operation,
                    payload,
                )
            raise

    def incomplete(self, operation, payload, state, reason):
        accepted = self.accepted_receipt
        result = {
            **accepted,
            "status": "unknown"
            if self.submission_started
            or accepted.get("message_ids")
            or accepted.get("external_reference")
            else "failed_known",
            "verified": False,
            "reason": reason,
            "read_state": state,
            "read_complete": False,
            "read_page_complete": False,
            "read_cursor_next": payload.get("read_cursor"),
        }
        if operation == "content":
            result.update(content_items=[], truncated=True)
        elif operation == "discover":
            result.update(candidates=[], source_note="读取未完成；不能当作完整目录")
        elif operation == "feedback":
            result.update(feedback_events=[], feedback_status="partial")
        elif operation == "preflight":
            result["allowed"] = False
        elif operation == "health":
            result.update(self.health(False, reason))
        if not self.submission_started and not (
            accepted.get("message_ids") or accepted.get("external_reference")
        ):
            result["confirmed_not_submitted"] = True
        return result

    def read_diagnostics(self, result, operation, payload):
        if operation in {"content", "discover"} and "read_complete" not in result:
            result.update(
                read_complete=False,
                read_page_complete=False,
                read_cursor_next=payload.get("read_cursor"),
                read_state="rate_limited" if result.get("rate_limited") else "incomplete",
            )
            result.setdefault("content_items" if operation == "content" else "candidates", [])
        if operation == "feedback" and "read_page_complete" not in result:
            result.update(
                read_page_complete=False,
                read_complete=False,
            )
            result.setdefault(
                "read_state", "rate_limited" if result.get("rate_limited") else "incomplete"
            )
        result.update(
            rpc_counts=dict(self.rpc_counts),
            rpc_count_source=self.rpc_count_source,
            read_rpc_attempts=sum(v for k, v in self.rpc_counts.items() if k != "mutation")
            if self.rpc_count_source != "unavailable"
            else None,
            read_rpc_count_source=self.rpc_count_source,
            read_rpc_used=sum(v for k, v in self.rpc_counts.items() if k != "mutation")
            if self.rpc_count_source != "unavailable"
            else None,
            read_rpc_count_scope="local_backend_logical_call_attempt",
            read_rpc_internal_retries="not_observed",
            read_rpc_wire_attempts="not_observed",
            read_identity_hash=self.read_identity_hash,
            read_identity_verified=bool(self.read_identity_hash),
        )
        if self.read_peer_id is not None:
            result["read_peer_id"] = self.read_peer_id
        if self.reads:
            result.update(self.reads.diagnostics())
        if self.teardown_error:
            result["disconnect_error"] = self.teardown_error
        return result

    async def _run(self, operation, payload, checkpoint, authorize):
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
                self.rpc_count_source = "skipped_no_durable_receipt"
                return {"status": "unknown", "verified": False, "reason": "no_durable_receipt"}
        self.reads = TelegramReads(
            self.label,
            self.settings["identity"],
            payload.get("read_connection_revision", digest(self.settings)),
            payload.get("read_policy"),
            context=payload.get("read_policy_context"),
            scan_limit=payload.get("read_scan_rpc_limit"),
        )
        client = self.client_factory(self.label, self.settings)
        original_call = getattr(client, "_call", None)
        if original_call:
            self.rpc_count_source = "telethon_logical_call_attempt"

            async def counted(sender, request, *args, **kwargs):
                categories = []
                for rpc in request if isinstance(request, (tuple, list)) else [request]:
                    name = type(rpc).__name__
                    category = (
                        "mutation"
                        if name.startswith(("Send", "Join", "ImportChatInvite"))
                        or name in {"SaveFilePartRequest", "SaveBigFilePartRequest"}
                        else "identity"
                        if name in {"GetUsersRequest", "GetStateRequest"}
                        else "permissions"
                        if name
                        in {"GetParticipantRequest", "GetFullChannelRequest", "GetFullChatRequest"}
                        else "discovery"
                        if name == "SearchRequest"
                        else "history"
                        if name
                        in {
                            "GetHistoryRequest",
                            "GetMessagesRequest",
                            "GetScheduledHistoryRequest",
                            "GetRepliesRequest",
                        }
                        else "entity"
                        if name
                        in {"ResolveUsernameRequest", "GetChannelsRequest", "GetChatsRequest"}
                        else "other"
                    )
                    categories.append(category)
                self.reads.reserve(sum(category != "mutation" for category in categories))
                for category in categories:
                    self.rpc_counts[category] = self.rpc_counts.get(category, 0) + 1
                if "mutation" in categories:
                    self.mutation_started = True
                try:
                    value = await original_call(sender, request, *args, **kwargs)
                except Exception as exc:
                    self.last_rpc_error = exc
                    raise
                self.last_rpc_error = None
                return value

            client._call = counted
        try:
            await client.connect()
            if not await client.is_user_authorized():
                # Telethon catches every RPCError in this helper, including
                # FloodWait. Preserve the real wait/restriction instead of
                # incorrectly treating that response as a lost login.
                from telethon import errors

                if self.last_rpc_error and not isinstance(
                    self.last_rpc_error, errors.UnauthorizedError
                ):
                    raise self.last_rpc_error
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
            self.read_identity_hash = digest(["telegram", str(me.id)])
            expected = (payload.get("read_policy_context") or {}).get("identity_hash")
            if expected and self.read_identity_hash != expected:
                self.read_identity_hash = None
                return self.health(False, "TelegramIdentityMismatchOrRestricted")
            if operation == "health":
                return {
                    **self.health(True, "account_read_verified"),
                    "account_identity": str(me.id),
                }
            if operation == "preflight":
                item = payload["payload"]
                target = await client.get_entity(self.target(item["target"]))
                if getattr(target, "bot", False):
                    allowed = self.is_spambot_diagnostic(target, item)
                else:
                    permissions = await client.get_permissions(target, "me")
                    allowed = self.can_send_messages(target, permissions)
                return {
                    "allowed": allowed,
                    **({"reason": "TargetWriteForbidden"} if not allowed else {}),
                }
            if operation == "discover":
                return await self.discover(client, payload["policy"], payload)
            if operation in {"join", "verify_join"}:
                return await self.membership(client, operation, payload, checkpoint, authorize)
            if operation == "feedback":
                return await self.feedback(client, payload)
            if operation == "content":
                return await self.content(client, payload)
            return await self.outbound(client, operation, payload, checkpoint, authorize)
        except Exception as exc:
            from telethon import errors

            if isinstance(exc, ReadBudgetExceeded):
                self.known_rejection = not self.submission_started or (
                    self.rpc_count_source == "telethon_logical_call_attempt"
                    and not self.mutation_started
                    and not self.accepted_receipt
                )
                raise

            if isinstance(exc, errors.UserBannedInChannelError):
                accepted = self.accepted_receipt
                known = operation == "execute" and not (
                    accepted.get("message_ids") or accepted.get("external_reference")
                )
                self.known_rejection = known
                result = {
                    **accepted,
                    "status": "failed_known" if known else "unknown",
                    "verified": False,
                    "reason": "UserBannedInChannelError",
                    "failure_class": "restricted",
                    "account_restriction": "telegram_speaking_restricted",
                }
                checkpoint(result)
                return result
            if isinstance(exc, (errors.FloodWaitError, errors.SlowModeWaitError)):
                return {
                    "status": "failed_known",
                    "verified": False,
                    "reason": type(exc).__name__,
                    "rate_limited": True,
                    "cooldown_source": "telegram_rpc_wait",
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
                    "cooldown_source": "telegram_rpc_restriction",
                    "retry_after_seconds": 86400,
                }
            raise
        finally:
            if original_call:
                client._call = original_call
            try:
                await client.disconnect()
            except Exception as exc:
                if not self.known_rejection:
                    raise
                self.teardown_error = type(exc).__name__

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

    @staticmethod
    def encode_entity(entity):
        # Telegram TL bytes are data, never pickle or executable Python objects.
        return {"tl": bytes(entity).hex()}

    @staticmethod
    def decode_entity(value):
        from telethon.extensions import BinaryReader

        try:
            with BinaryReader(bytes.fromhex(value["tl"])) as reader:
                entity = reader.tgread_object()
            if not getattr(entity, "username", None) or not getattr(entity, "megagroup", False):
                raise ValueError
            return entity
        except Exception:
            raise BackendError("TelegramReadCacheInvalid") from None

    async def read_entity(self, client, target, *, force_fresh=False):
        key = self.target(target).lstrip("@").casefold()
        cached = None if force_fresh else self.reads.get("entity", key)
        if cached:
            return self.decode_entity(cached[0])
        entity = await client.get_entity(self.target(target))
        if getattr(entity, "username", None) and getattr(entity, "megagroup", False):
            try:
                encoded = self.encode_entity(entity)
            except (TypeError, AttributeError):
                pass  # Synthetic clients need not implement Telegram TL encoding.
            else:
                self.reads.put("entity", key, encoded)
        return entity

    def read_observed_at(self):
        return min(time.time(), self.reads.cache_observed_at or time.time())

    async def discover(self, client, policy, payload=None):
        from telethon import functions

        payload = payload or {}
        rows, seen = {}, set()
        maximum = policy["max_candidates"]
        queries = list(dict.fromkeys(q.strip() for q in policy["queries"]))
        request_key = digest(["telegram-discover-v1", queries, maximum])
        cursor = payload.get("read_cursor")
        query_start, chat_start = 0, 0
        snapshot_id = uuid.uuid4().hex
        snapshot = {"directories": {}, "observed": time.time()}
        if cursor is not None:
            if not isinstance(cursor, dict) or cursor.get("scope") != self.reads.scope:
                return self.incomplete(
                    "discover", payload, "cursor_scope_changed", "TelegramReadCursorScopeChanged"
                )
            if (
                not self.reads.valid_cursor(cursor)
                or cursor.get("request") != request_key
                or cursor.get("kind") != "discover"
            ):
                return self.incomplete(
                    "discover", payload, "cursor_invalid", "TelegramReadCursorInvalid"
                )
            if not cursor["complete"]:
                query_start, chat_start = cursor["query"], cursor["index"]
                snapshot_id = cursor["snapshot"]
                snapshot = self.reads.get_snapshot(snapshot_id)
                seen = set(cursor["visited"])
                self.reads.cache_hits += 1
                self.reads.cache_observed_at = min(
                    self.reads.cache_observed_at or snapshot["observed"], snapshot["observed"]
                )
        groups = 0
        next_query, next_chat, complete = query_start, chat_start, True
        for query_index in range(query_start, len(queries)):
            query = queries[query_index]
            frozen = snapshot["directories"].get(str(query_index))
            directory = (
                None if frozen is not None else self.reads.get("directory", [query, maximum])
            )
            if frozen is not None:
                chats = [self.decode_entity(row) for row in frozen]
            elif directory:
                chats = [self.decode_entity(row) for row in directory[0]]
                snapshot["directories"][str(query_index)] = directory[0]
                snapshot["observed"] = min(snapshot["observed"], directory[1])
            else:
                response = await client(functions.contacts.SearchRequest(q=query, limit=maximum))
                chats = [
                    chat
                    for chat in response.chats
                    if getattr(chat, "username", None) and getattr(chat, "megagroup", False)
                ]
                encoded = [self.encode_entity(chat) for chat in chats]
                self.reads.put("directory", [query, maximum], encoded)
                for chat, value in zip(chats, encoded):
                    self.reads.put("entity", chat.username.casefold(), value)
                snapshot["directories"][str(query_index)] = encoded
            self.reads.put_snapshot(snapshot_id, snapshot)
            for chat_index in range(chat_start if query_index == query_start else 0, len(chats)):
                chat = chats[chat_index]
                next_query, next_chat = query_index, chat_index + 1
                if chat.id in seen:
                    continue
                if groups >= self.reads.policy["groups_per_scan"] or len(rows) >= maximum:
                    next_chat, complete = chat_index, False
                    break
                groups += 1
                seen.add(chat.id)
                metadata = self.reads.get("discovery_metadata", str(chat.id))
                key = "@" + chat.username
                if metadata:
                    rows[key] = {**metadata[0], "target": key}
                    continue
                full = await client(functions.channels.GetFullChannelRequest(chat))
                about = getattr(full.full_chat, "about", "") or ""
                recent = await client.get_messages(
                    chat, limit=min(20, self.reads.policy["messages_per_page"])
                )
                cutoff = datetime.now(timezone.utc) - timedelta(days=1)
                activity = sum(bool(m.date and m.date >= cutoff) for m in recent)
                evidence = (
                    chat.title + " " + about + " " + " ".join((m.message or "") for m in recent)
                )
                language, confidence = language_evidence(evidence)
                row = {
                    "target": key,
                    "title": chat.title,
                    "text": about,
                    "language": language,
                    "confidence": confidence,
                    "members": getattr(full.full_chat, "participants_count", 0) or 0,
                    "activity": activity,
                    "is_public": True,
                }
                self.reads.put("discovery_metadata", str(chat.id), row)
                rows[key] = row
            if not complete:
                break
            next_query, next_chat = query_index + 1, 0
        complete = complete and next_query >= len(queries)
        token = self.reads.issue_cursor(
            {
                "v": 1,
                "kind": "discover",
                "scope": self.reads.scope,
                "identity": self.reads.identity,
                "request": request_key,
                "query": next_query,
                "index": next_chat,
                "snapshot": snapshot_id,
                "visited": sorted(seen),
                "complete": complete,
            }
        )
        return {
            "candidates": list(rows.values()),
            "read_cursor_next": token,
            "read_complete": complete,
            "read_page_complete": True,
            "read_state": "complete" if complete else "group_limit",
            "read_observed_at": self.read_observed_at(),
            "source_note": "公开群资料及最近24小时样本；缓存沿用原观测时间，语言证据不足的候选保守拒绝",
        }

    async def content(self, client, payload):
        from telethon import utils

        cursor = payload.get("read_cursor")
        rebind = isinstance(cursor, dict) and cursor.get("scope") != self.reads.scope
        entity = await self.read_entity(client, payload["target"], force_fresh=rebind)
        if not getattr(entity, "username", None) or not getattr(entity, "megagroup", False):
            raise BackendError("PublicTelegramGroupRequired")
        requested = payload.get("limit", 200)
        if type(requested) is not int or not 1 <= requested <= 1000:
            raise BackendError("InvalidContentLimit")
        peer = str(utils.get_peer_id(entity))
        self.read_peer_id = peer
        target_key = digest(
            [
                "telegram-content-recent-v1",
                self.target(payload["target"]).lstrip("@").casefold(),
                requested,
            ]
        )
        cursor = payload.get("read_cursor")
        bootstrap = cursor is None
        page_size = min(requested, self.reads.policy["messages_per_page"])
        bootstrap_messages = None
        if cursor is not None:
            if not isinstance(cursor, dict):
                return self.incomplete(
                    "content", payload, "cursor_invalid", "TelegramReadCursorInvalid"
                )
            if rebind and not (self.read_identity_hash and self.reads.registered_cursor(cursor)):
                return self.incomplete(
                    "content", payload, "cursor_scope_changed", "TelegramReadCursorScopeChanged"
                )
            if (
                not (
                    self.reads.registered_cursor(cursor)
                    if rebind
                    else self.reads.valid_cursor(cursor)
                )
                or cursor.get("kind") != "content"
                or cursor.get("request") != target_key
            ):
                return self.incomplete(
                    "content", payload, "cursor_invalid", "TelegramReadCursorInvalid"
                )
            if cursor.get("peer_id") != peer:
                return self.incomplete(
                    "content", payload, "cursor_peer_changed", "TelegramReadCursorPeerChanged"
                )
            floor, head, last = cursor["floor"], cursor["head"], cursor["last"]
            if cursor["complete"]:
                newest = await client.get_messages(entity, limit=1)
                head = max(last, newest[0].id if newest else last)
                floor = last + 1
        elif requested <= page_size:
            # The bounded recent window fits one page; do not spend separate
            # head/floor/history requests to retrieve these same messages.
            bootstrap_messages = await client.get_messages(entity, limit=requested)
            head = max((message.id for message in bootstrap_messages), default=0)
            floor = min((message.id for message in bootstrap_messages), default=1)
            last = floor - 1
        else:
            newest = await client.get_messages(entity, limit=1)
            head = newest[0].id if newest else 0
            boundary = (
                await client.get_messages(
                    entity, limit=1, offset_id=head + 1, add_offset=requested - 1
                )
                if head
                else []
            )
            floor = boundary[0].id if boundary else 1
            last = floor - 1
        messages = (
            bootstrap_messages
            if bootstrap_messages is not None
            else await client.get_messages(
                entity, limit=page_size, min_id=last, max_id=head + 1, reverse=True
            )
            if head > last
            else []
        )
        # The entire page must return before advancing past even filtered rows.
        if any(
            type(getattr(m, "id", None)) is not int or not last < m.id <= head for m in messages
        ):
            raise BackendError("TelegramReadPageInvalid")
        messages = sorted(messages, key=lambda message: message.id)
        if messages:
            last = messages[-1].id
        complete = last >= head or len(messages) < page_size
        if complete:
            # Missing/deleted IDs do not require a second history traversal.
            last = head
        token = {
            "v": 1,
            "kind": "content",
            "scope": self.reads.scope,
            "identity": self.reads.identity,
            "request": target_key,
            "peer_id": peer,
            "floor": floor,
            "head": head,
            "last": last,
            "complete": complete,
            "initial_limit": requested,
        }
        items = []
        for message in messages:
            text = getattr(message, "message", "") or ""
            if not text.strip() or getattr(message, "out", False):
                continue
            language, confidence = language_evidence(text)
            items.append(
                {
                    "platform": "telegram",
                    "external_id": str(message.id),
                    "native_target_id": peer,
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
        self.reads.issue_cursor(token)
        return {
            "content_items": items,
            "read_cursor_rebound": rebind,
            "truncated": not complete,
            "read_cursor_next": token,
            "read_complete": complete,
            "read_page_complete": True,
            "read_state": "complete" if complete else "page_limit",
            "read_observed_at": self.read_observed_at(),
            "initial_window_scope": "recent_requested_limit"
            if bootstrap
            else "incremental_from_committed_cursor",
        }

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
            self.submission_started = True
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
    def is_spambot_diagnostic(entity, item):
        """Only an exact, identity-pinned diagnostic command may address a bot."""
        return bool(
            getattr(entity, "bot", False)
            and (getattr(entity, "username", "") or "").casefold() == "spambot"
            and item.get("target", "").casefold() == "@spambot"
            and item.get("recipient_id") == str(getattr(entity, "id", ""))
            and item.get("action") == "send_message"
            and item.get("text") == "/start"
            and not item.get("media")
            and not item.get("reply_to")
            and not item.get("title")
        )

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
        from telethon import functions, utils

        item = payload["payload"]
        entity = await client.get_entity(self.target(item["target"]))
        peer_id = str(utils.get_peer_id(entity))
        self.read_peer_id = peer_id
        native = item["action"] in {"schedule_message", "schedule_file"}
        result = payload.get("receipt") or payload.get("result", {})
        if result.get("message_chat_id") and str(result["message_chat_id"]) != peer_id:
            return {
                **result,
                "status": "unknown",
                "verified": False,
                "reason": "TelegramReceiptPeerMismatch",
            }
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

            if getattr(entity, "bot", False):
                if not self.is_spambot_diagnostic(entity, item):
                    return {
                        "status": "failed_known",
                        "verified": False,
                        "reason": "UnsupportedBotDiagnostic",
                    }
            else:
                try:
                    permissions = await client.get_permissions(entity, "me")
                except (
                    errors.ChannelPrivateError,
                    errors.UserNotParticipantError,
                    errors.ChatWriteForbiddenError,
                    errors.ChatAdminRequiredError,
                ) as exc:
                    return {
                        "status": "failed_known",
                        "verified": False,
                        "reason": type(exc).__name__,
                    }
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
            self.submission_started = True
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
                "message_chat_id": peer_id,
            }
            self.accepted_receipt = result
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
        from telethon import utils

        item, result = payload["payload"], payload["result"]
        entity = await client.get_entity(self.target(item["target"]))
        self.read_peer_id = str(utils.get_peer_id(entity))
        expected_peers = [
            value for value in (payload.get("read_peer_id"), result.get("message_chat_id")) if value
        ]
        if any(str(value) != self.read_peer_id for value in expected_peers):
            return {
                "verified": False,
                "feedback_status": "partial",
                "reason": "TelegramReadCursorPeerChanged",
                "read_state": "cursor_peer_changed",
            }
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
            and bool(expected_peers)
        )
        if parent_missing and not durable_parent:
            return {"verified": False, "reason": "feedback_parent_unavailable"}
        if not parent_missing and not message_matches(parent, item):
            return {"verified": False, "reason": "feedback_parent_not_verified"}
        checked_through = time.time()
        cursor = max(int(payload.get("feedback_cursor", 0)), parent_id)
        page_size = self.reads.policy["messages_per_page"]
        messages = [
            m
            async for m in client.iter_messages(
                entity, min_id=cursor, reverse=True, limit=page_size
            )
        ]
        complete = len(messages) < page_size and not parent_missing
        events = []
        for message in messages:
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
            "read_page_complete": True,
            "read_complete": complete,
            "read_state": "complete" if complete else "partial",
            "read_observed_at": checked_through,
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
