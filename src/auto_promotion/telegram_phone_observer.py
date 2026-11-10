"""Read-only MTProto observer for phone actions, independent of the Android client's UI."""

import asyncio
import re
import time
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

from .backends.common import BackendError, account_lock, profile_path, read_profile
from .backends.telegram import TelegramBackend
from .backends.telegram_reads import TelegramReads
from .telegram_receipt_budget import reserve_receipt_reads, validate_receipt_reads

# Platform message dates have one-second resolution and a separate clock.
TELEGRAM_CLOCK_TOLERANCE = 2.0
RECEIPT_CREATION_WINDOW = 120.0


def bound_telegram_receipt(receipt, context, submitted_at):
    from phone_fleet_protocol.social import receipt_matches

    return bool(
        receipt
        and receipt.source == "telegram_api"
        and submitted_at
        and receipt.created_at <= submitted_at + RECEIPT_CREATION_WINDOW
        and receipt_matches(receipt, context, submitted_at)
    )


class TelegramObserverConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    backend_label: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    user_id: int = Field(gt=0, strict=True)
    accounts_dir: str | None = None
    api_account_id: str | None = None
    api_database: str | None = None


def make_observer_client(config, settings):
    from telethon import TelegramClient

    return TelegramClient(
        str(
            profile_path("telegram", config.backend_label, accounts_dir=config.accounts_dir)
            / "telegram.session"
        ),
        int(settings["api_id"]),
        settings["api_hash"],
        proxy=settings.get("proxy"),
        request_retries=0,
        connection_retries=0,
        auto_reconnect=False,
        flood_sleep_threshold=0,
        raise_last_call_error=True,
        receive_updates=False,
    )


def same_native_account(me, identity):
    from phone_fleet_protocol import fingerprint

    if identity.startswith("telegram-phone-sha256:"):
        phone = re.sub(r"\D", "", str(getattr(me, "phone", "") or ""))
        return bool(
            phone and identity == "telegram-phone-sha256:" + fingerprint({"telegram_phone": phone})
        )
    return identity.casefold() == ("@" + str(getattr(me, "username", "") or "")).casefold()


class TelegramPhoneObserver:
    READ_RPCS = {
        "GetStateRequest",
        "GetDifferenceRequest",
        "GetUsersRequest",
        "ResolveUsernameRequest",
        "GetChannelsRequest",
        "GetChatsRequest",
        "GetParticipantRequest",
        "GetFullChannelRequest",
        "GetFullChatRequest",
        "GetHistoryRequest",
        "GetMessagesRequest",
        "GetConfigRequest",
        "GetNearestDcRequest",
    }

    def __init__(self, configs, *, client_factory=make_observer_client, settings_loader=None):
        self.configs = configs
        self.client_factory = client_factory
        self.settings_loader = settings_loader or (
            lambda c: read_profile("telegram", c.backend_label, accounts_dir=c.accounts_dir)
        )

    def baseline(self, account_id, identity, target_url):
        return asyncio.run(self.query(account_id, identity, target_url))

    def inspect_target(self, account_id, identity, target_url):
        return asyncio.run(self.query(account_id, identity, target_url, inspect_target=True))

    def _configuration(self, account_id):
        config = self.configs.get(account_id)
        if config is None:
            raise BackendError("PhoneTelegramObserverNotConfigured")
        config = TelegramObserverConfig.model_validate(config)
        settings = self.settings_loader(config)
        if (
            str(settings.get("identity")) != str(config.user_id)
            or not settings.get("api_id")
            or not settings.get("api_hash")
        ):
            raise BackendError("PhoneTelegramObserverAuthorizationRequired")
        return config, settings

    @staticmethod
    def _reads(config, settings):
        context_read = (
            {"database": config.api_database, "account_id": config.api_account_id}
            if config.api_account_id and config.api_database
            else None
        )
        return TelegramReads(
            config.backend_label,
            str(config.user_id),
            str(settings.get("identity")),
            scan_limit=16,
            context=context_read,
            ledger_root=profile_path(
                "telegram", config.backend_label, accounts_dir=config.accounts_dir
            ).parent,
        )

    def reserve_receipt(self, account_id):
        config, settings = self._configuration(account_id)
        return reserve_receipt_reads(self._reads(config, settings))

    def check_receipt_reservation(self, account_id, reservation, *, minimum_seconds=0):
        config, settings = self._configuration(account_id)
        validate_receipt_reads(
            self._reads(config, settings), reservation, minimum_seconds=minimum_seconds
        )

    def receipt(self, account_id, context, baseline, since, *, consume=None):
        return asyncio.run(
            self.query(
                account_id,
                context.identity,
                context.target_url,
                baseline=baseline,
                context=context,
                since=since,
                consume=consume,
            )
        )

    async def query(
        self,
        account_id,
        identity,
        target_url,
        *,
        baseline=None,
        context=None,
        since=None,
        consume=None,
        inspect_target=False,
    ):
        from phone_fleet_protocol import fingerprint
        from phone_fleet_protocol.social import SocialReceipt, target_parts

        try:
            from telethon import errors, utils
        except ImportError:
            raise BackendError("PhoneTelegramObserverDependencyUnavailable") from None

        config, settings = self._configuration(account_id)
        username = target_parts("telegram", target_url)[0]
        # This is a separate API authorization. It never opens/copies Android session data.
        with account_lock("telegram", config.backend_label, accounts_dir=config.accounts_dir):
            client = self.client_factory(config, settings)
            reads = self._reads(config, settings)
            original = getattr(client, "_call", None)
            if not callable(original):
                raise BackendError("PhoneTelegramObserverReadGuardUnavailable")

            async def readonly(sender, request, *args, **kwargs):
                requests = request if isinstance(request, (tuple, list)) else [request]
                for rpc in requests:
                    while type(rpc).__name__ in {
                        "InvokeWithLayerRequest",
                        "InitConnectionRequest",
                        "InvokeWithoutUpdatesRequest",
                    }:
                        rpc = rpc.query
                    if type(rpc).__name__ not in self.READ_RPCS:
                        raise BackendError("PhoneTelegramObserverMutationForbidden")
                if consume is None:
                    from .telegram_receipt_budget import RECEIPT_READ_LIMIT

                    reads.reserve(len(requests), keep=RECEIPT_READ_LIMIT if baseline is None else 0)
                else:
                    consume(reads, len(requests))
                return await original(sender, request, *args, **kwargs)

            client._call = readonly
            try:
                async with asyncio.timeout(12):
                    await client.connect()
                    if not await client.is_user_authorized():
                        raise BackendError("PhoneTelegramObserverAuthorizationRequired")
                    me = await client.get_me()
                    if (
                        not me
                        or me.id != config.user_id
                        or getattr(me, "bot", False)
                        or getattr(me, "restricted", False)
                        or getattr(me, "deleted", False)
                        or not same_native_account(me, identity)
                    ):
                        raise BackendError("PhoneTelegramObserverIdentityMismatch")
                    entity = await client.get_entity(username)
                    if (
                        not getattr(entity, "megagroup", False)
                        or not getattr(entity, "username", None)
                        or entity.username.casefold() != username.casefold()
                    ):
                        raise BackendError("PhoneTelegramPublicGroupRequired")
                    # A group-root task cannot authorize a particular forum topic.
                    if baseline is None and not inspect_target and getattr(entity, "forum", False):
                        raise BackendError("PhoneTelegramForumTopicRequired")
                    peer_id = str(utils.get_peer_id(entity))
                    try:
                        permissions = await client.get_permissions(entity, "me")
                        joined = not permissions.has_left
                        can_send = joined and TelegramBackend.can_send_messages(entity, permissions)
                        participant = getattr(permissions, "participant", None)
                        if getattr(
                            getattr(entity, "default_banned_rights", None), "send_plain", False
                        ) or getattr(
                            getattr(participant, "banned_rights", None), "send_plain", False
                        ):
                            can_send = False
                    except errors.UserNotParticipantError:
                        joined, can_send = False, False
                    if baseline is None:
                        from telethon import functions

                        full = (
                            await client(functions.channels.GetFullChannelRequest(entity))
                        ).full_chat
                        sender = getattr(full, "default_send_as", None)
                        anonymous = (
                            bool(
                                getattr(
                                    getattr(
                                        getattr(permissions, "participant", None),
                                        "admin_rights",
                                        None,
                                    ),
                                    "anonymous",
                                    False,
                                )
                            )
                            if joined
                            else False
                        )
                        sends_as_self = (
                            sender.user_id == me.id
                            if getattr(sender, "user_id", None) is not None
                            else sender is None and not anonymous
                        )
                        if not inspect_target and not sends_as_self:
                            raise BackendError("PhoneTelegramSenderIdentityMismatch")
                    if inspect_target:
                        pinned_id = getattr(full, "pinned_msg_id", None)
                        pinned = (
                            await client.get_messages(entity, ids=pinned_id) if pinned_id else None
                        )
                        return {
                            "peer_username": entity.username,
                            "peer_title": entity.title,
                            "joined": joined,
                            "can_send": can_send,
                            "sends_as_self": sends_as_self,
                            "forum": bool(getattr(entity, "forum", False)),
                            "about": str(getattr(full, "about", "") or "")[:1500],
                            "pinned_text": str(getattr(pinned, "message", "") or "")[:2000],
                            "observed_at": time.time(),
                        }
                    if baseline is None:
                        newest = await client.get_messages(entity, limit=1)
                        return {
                            "api_user_id": str(me.id),
                            "identity_sha256": fingerprint({"account": identity}),
                            "peer_id": peer_id,
                            "peer_username": entity.username,
                            "peer_title": entity.title,
                            "joined": joined,
                            "can_send": can_send,
                            "sends_as_self": sends_as_self,
                            "last_message_id": max([m.id for m in newest], default=0),
                            "observed_at": time.time(),
                        }
                    if (
                        peer_id != baseline["peer_id"]
                        or str(me.id) != baseline["api_user_id"]
                        or baseline["identity_sha256"] != fingerprint({"account": identity})
                    ):
                        raise BackendError("PhoneTelegramObserverPeerChanged")
                    lower = since - TELEGRAM_CLOCK_TOLERANCE
                    upper = min(since + RECEIPT_CREATION_WINDOW, time.time() + 2)
                    # Telegram's history offset is exclusive, with whole-second timestamps.
                    cutoff = datetime.fromtimestamp(int(upper) + 1, timezone.utc)

                    def matches(message):
                        return (
                            message
                            and getattr(message, "out", False)
                            and getattr(message, "sender_id", None) == me.id
                            and message.id > baseline["last_message_id"]
                            and (getattr(message, "message", "") or "") == context.text
                            and not getattr(message, "media", None)
                            and not getattr(message, "reply_to_msg_id", None)
                            and not getattr(message, "fwd_from", None)
                            and not getattr(message, "edit_date", None)
                            and getattr(message, "date", None)
                            and lower <= message.date.timestamp() <= upper
                        )

                    for delay in (0, 0.5, 1, 2):
                        if delay:
                            await asyncio.sleep(delay)
                        messages = await client.get_messages(
                            entity, limit=65, min_id=baseline["last_message_id"], offset_date=cutoff
                        )
                        if len(messages) >= 65:
                            return None  # The bounded page cannot prove uniqueness.
                        found = [m for m in messages if matches(m)]
                        if found:
                            break
                    if len(found) != 1:
                        return None
                    reread = await client.get_messages(entity, ids=[found[0].id])
                    if (
                        len(reread) != 1
                        or not matches(reread[0])
                        or reread[0].id != found[0].id
                        or reread[0].date != found[0].date
                    ):
                        return None
                    message = reread[0]
                    return SocialReceipt(
                        platform="telegram",
                        action="send_message",
                        native_id=str(message.id),
                        target_id=context.target_id,
                        author=identity,
                        text_sha256=fingerprint({"text": context.text}),
                        permalink=f"https://t.me/{entity.username}/{message.id}",
                        created_at=message.date.timestamp(),
                        observed_at=time.time(),
                        source="telegram_api",
                    )
            except BackendError:
                raise
            except Exception as error:
                code = (
                    "PhoneTelegramObserverFloodWait"
                    if type(error).__name__ == "FloodWaitError"
                    else "PhoneTelegramObserverReadUnavailable"
                )
                failure = BackendError(code)
                if type(error).__name__ == "FloodWaitError":
                    failure.retry_after_seconds = max(1, int(getattr(error, "seconds", 60)))
                raise failure from None
            finally:
                try:
                    await asyncio.wait_for(client.disconnect(), 2)
                except Exception:
                    pass
