"""Read-only MTProto observer for phone actions, independent of the Android client's UI."""
import asyncio
import re
import time

from pydantic import BaseModel, ConfigDict, Field

from .backends.common import BackendError, account_lock, profile_path, read_profile
from .backends.telegram import TelegramBackend
from .backends.telegram_reads import TelegramReads


class TelegramObserverConfig(BaseModel):
    model_config = ConfigDict(extra="forbid",frozen=True)
    backend_label: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    user_id: int = Field(gt=0,strict=True)
    accounts_dir: str | None = None
    api_account_id: str | None = None
    api_database: str | None = None


def make_observer_client(config,settings):
    from telethon import TelegramClient

    return TelegramClient(str(profile_path("telegram",config.backend_label,accounts_dir=config.accounts_dir)/"telegram.session"),
        int(settings["api_id"]),settings["api_hash"],proxy=settings.get("proxy"),request_retries=0,
        connection_retries=0,auto_reconnect=False,flood_sleep_threshold=0,raise_last_call_error=True,receive_updates=False)


def same_native_account(me,identity):
    from phone_fleet_protocol import fingerprint

    if identity.startswith("telegram-phone-sha256:"):
        phone = re.sub(r"\D","",str(getattr(me,"phone","") or ""))
        return bool(phone and identity=="telegram-phone-sha256:"+fingerprint({"telegram_phone":phone}))
    return identity.casefold()==("@"+str(getattr(me,"username","") or "")).casefold()


class TelegramPhoneObserver:
    READ_RPCS = {"GetStateRequest","GetUsersRequest","ResolveUsernameRequest","GetChannelsRequest","GetChatsRequest",
        "GetParticipantRequest","GetFullChannelRequest","GetFullChatRequest","GetHistoryRequest","GetMessagesRequest",
        "GetConfigRequest","GetNearestDcRequest"}

    def __init__(self,configs,*,client_factory=make_observer_client,settings_loader=None):
        self.configs = configs
        self.client_factory = client_factory
        self.settings_loader = settings_loader or (lambda c:read_profile("telegram",c.backend_label,accounts_dir=c.accounts_dir))

    def baseline(self,account_id,identity,target_url):
        return asyncio.run(self.query(account_id,identity,target_url))

    def receipt(self,account_id,context,baseline,since):
        return asyncio.run(self.query(account_id,context.identity,context.target_url,baseline=baseline,context=context,since=since))

    async def query(self,account_id,identity,target_url,*,baseline=None,context=None,since=None):
        from phone_fleet_protocol import fingerprint
        from phone_fleet_protocol.social import SocialReceipt, target_parts
        try:
            from telethon import errors, utils
        except ImportError:
            raise BackendError("PhoneTelegramObserverDependencyUnavailable") from None

        config = self.configs.get(account_id)
        if config is None:
            raise BackendError("PhoneTelegramObserverNotConfigured")
        config = TelegramObserverConfig.model_validate(config)
        settings = self.settings_loader(config)
        if str(settings.get("identity"))!=str(config.user_id) or not settings.get("api_id") or not settings.get("api_hash"):
            raise BackendError("PhoneTelegramObserverAuthorizationRequired")
        username = target_parts("telegram",target_url)[0]
        # This is a separate API authorization. It never opens/copies Android session data.
        with account_lock("telegram",config.backend_label,accounts_dir=config.accounts_dir):
            client = self.client_factory(config,settings)
            context_read = {"database":config.api_database,"account_id":config.api_account_id} if config.api_account_id and config.api_database else None
            reads = TelegramReads(config.backend_label,str(config.user_id),str(settings.get("identity")),scan_limit=16,context=context_read,
                ledger_root=profile_path("telegram",config.backend_label,accounts_dir=config.accounts_dir).parent)
            original = getattr(client,"_call",None)
            if original:
                async def readonly(sender,request,*args,**kwargs):
                    requests = request if isinstance(request,(tuple,list)) else [request]
                    for rpc in requests:
                        while type(rpc).__name__ in {"InvokeWithLayerRequest","InitConnectionRequest","InvokeWithoutUpdatesRequest"}:
                            rpc = rpc.query
                        if type(rpc).__name__ not in self.READ_RPCS:
                            raise BackendError("PhoneTelegramObserverMutationForbidden")
                    reads.reserve(len(requests))
                    return await original(sender,request,*args,**kwargs)
                client._call = readonly
            try:
                async with asyncio.timeout(12):
                    await client.connect()
                    if not await client.is_user_authorized():
                        raise BackendError("PhoneTelegramObserverAuthorizationRequired")
                    me = await client.get_me()
                    if (not me or me.id!=config.user_id or getattr(me,"bot",False) or getattr(me,"restricted",False)
                            or getattr(me,"deleted",False) or not same_native_account(me,identity)):
                        raise BackendError("PhoneTelegramObserverIdentityMismatch")
                    entity = await client.get_entity(username)
                    if not getattr(entity,"megagroup",False) or not getattr(entity,"username",None) or entity.username.casefold()!=username.casefold():
                        raise BackendError("PhoneTelegramPublicGroupRequired")
                    peer_id = str(utils.get_peer_id(entity))
                    try:
                        permissions = await client.get_permissions(entity,"me")
                        joined = not permissions.has_left
                        can_send = joined and TelegramBackend.can_send_messages(entity,permissions)
                        participant = getattr(permissions,"participant",None)
                        if getattr(getattr(entity,"default_banned_rights",None),"send_plain",False) or getattr(getattr(participant,"banned_rights",None),"send_plain",False):
                            can_send = False
                    except errors.UserNotParticipantError:
                        joined,can_send = False,False
                    if baseline is None:
                        newest = await client.get_messages(entity,limit=1)
                        return {"api_user_id":str(me.id),"identity_sha256":fingerprint({"account":identity}),
                            "peer_id":peer_id,"peer_username":entity.username,"peer_title":entity.title,
                            "joined":joined,"can_send":can_send,"last_message_id":max([m.id for m in newest],default=0),"observed_at":time.time()}
                    if (peer_id!=baseline["peer_id"] or str(me.id)!=baseline["api_user_id"]
                            or baseline["identity_sha256"]!=fingerprint({"account":identity})):
                        raise BackendError("PhoneTelegramObserverPeerChanged")
                    found = []
                    for delay in (0,.5,1,2):
                        if delay:
                            await asyncio.sleep(delay)
                        messages = await client.get_messages(entity,limit=64,min_id=baseline["last_message_id"])
                        found = [m for m in messages if getattr(m,"out",False) and getattr(m,"sender_id",None)==me.id
                            and m.id>baseline["last_message_id"] and (getattr(m,"message","") or "")==context.text
                            and not getattr(m,"media",None) and not getattr(m,"reply_to_msg_id",None) and not getattr(m,"fwd_from",None)
                            and getattr(m,"date",None) and since<=m.date.timestamp()<=time.time()+2]
                        if found:
                            break
                    if len(found)!=1:
                        return None
                    message = found[0]
                    return SocialReceipt(platform="telegram",action="send_message",native_id=str(message.id),target_id=context.target_id,
                        author=identity,text_sha256=fingerprint({"text":context.text}),permalink=f"https://t.me/{entity.username}/{message.id}",
                        created_at=message.date.timestamp(),observed_at=time.time(),source="telegram_api")
            except BackendError:
                raise
            except Exception as error:
                code = "PhoneTelegramObserverFloodWait" if type(error).__name__=="FloodWaitError" else "PhoneTelegramObserverReadUnavailable"
                failure = BackendError(code)
                if type(error).__name__=="FloodWaitError":
                    failure.retry_after_seconds = max(1,int(getattr(error,"seconds",60)))
                raise failure from None
            finally:
                try:
                    await asyncio.wait_for(client.disconnect(),2)
                except Exception:
                    pass
