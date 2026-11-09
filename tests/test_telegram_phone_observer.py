"""Synthetic MTProto reads only; phone UI actions are never replaced by API writes."""
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from phone_fleet_protocol import fingerprint
from phone_fleet_protocol.social import SocialInteraction
from telethon import types

from auto_promotion.backends.common import BackendError
from auto_promotion.telegram_phone_observer import (
    TelegramObserverConfig,
    TelegramPhoneObserver,
    same_native_account,
)


class Client:
    def __init__(self):
        self.calls=[]
        self.me=SimpleNamespace(id=44,username="test_sender",phone="15551234567")
        self.entity=types.Channel(id=123,title="Test group",photo=types.ChatPhotoEmpty(),date=datetime.now(timezone.utc),megagroup=True,username="test_group")
        self.messages=[]
        self.attack=False
        self.raw_mutations=0
    async def _call(self,sender,request,*args,**kwargs):
        if type(request).__name__=="SendMessageRequest":
            self.raw_mutations+=1
    async def __call__(self, request):
        await self._call(None,request)
        return SimpleNamespace(full_chat=SimpleNamespace(default_send_as=getattr(self,"send_as",None)))
    async def connect(self):
        self.calls.append("connect")
        # Telethon initializes an authorized session's empty message box with this read.
        await self._call(None,type("GetDifferenceRequest",(),{})())
    async def disconnect(self):
        self.calls.append("disconnect")
    async def is_user_authorized(self):
        return True
    async def get_me(self):
        self.calls.append("me")
        await self._call(None,type("GetUsersRequest",(),{})())
        return self.me
    async def get_entity(self,username):
        self.calls.append("peer")
        return self.entity
    async def get_permissions(self,*args):
        return SimpleNamespace(has_left=False,is_banned=False,participant=SimpleNamespace())
    async def get_messages(self,*args,**kwargs):
        self.calls.append("history")
        if self.attack:
            await self._call(None,type(self.attack,(),{})())
        if "ids" in kwargs:
            self.calls.append(("exact_ids", kwargs["ids"]))
            if getattr(self, "changed_reread", False):
                return []
            return [m for m in self.messages if m.id in kwargs["ids"]]
        return self.messages


def setup_reader(tmp_path,monkeypatch):
    monkeypatch.setenv("AP_ACCOUNTS_DIR",str(tmp_path/"accounts"))
    config=TelegramObserverConfig(backend_label="synthetic-observer",user_id=44)
    clients=[]
    def factory(*args):
        client=Client()
        clients.append(client)
        return client
    reader=TelegramPhoneObserver({"phone":config},client_factory=factory,settings_loader=lambda _: {"identity":"44","api_id":1,"api_hash":"synthetic"})
    return reader,clients


@pytest.mark.asyncio
async def test_forum_group_root_requires_a_frozen_topic_before_drafting(tmp_path, monkeypatch):
    reader, clients = setup_reader(tmp_path, monkeypatch)
    original_factory = reader.client_factory
    def forum(*args):
        client = original_factory(*args)
        client.entity.forum = True
        return client
    reader.client_factory = forum
    with pytest.raises(BackendError, match="PhoneTelegramForumTopicRequired"):
        await reader.query("phone", "@test_sender", "https://t.me/test_group")
    assert "history" not in clients[0].calls and clients[0].raw_mutations == 0


@pytest.mark.asyncio
async def test_personal_send_cannot_use_a_group_channel_alias(tmp_path, monkeypatch):
    reader, clients = setup_reader(tmp_path, monkeypatch)
    original_factory = reader.client_factory
    def alias(*args):
        client = original_factory(*args)
        client.send_as = types.PeerChannel(channel_id=100)
        return client
    reader.client_factory = alias
    with pytest.raises(BackendError, match="PhoneTelegramSenderIdentityMismatch"):
        await reader.query("phone", "@test_sender", "https://t.me/test_group")
    assert clients[0].raw_mutations == 0


def test_phone_number_and_handle_identity_must_match_same_api_account():
    me=SimpleNamespace(username="Test_Sender",phone="+1 555 123 4567")
    digest="telegram-phone-sha256:"+fingerprint({"telegram_phone":"15551234567"})
    assert same_native_account(me,digest) and same_native_account(me,"@test_sender")
    assert not same_native_account(me,"@different")


@pytest.mark.parametrize("change",("valid","real_clock_delta","too_early","old_message","wrong_user","duplicate","changed_reread","incomplete_page","api_write","api_join"))
@pytest.mark.asyncio
async def test_only_new_unique_own_native_message_can_be_receipt(tmp_path,monkeypatch,change):
    reader,clients=setup_reader(tmp_path,monkeypatch)
    now=time.time()
    context=SocialInteraction(platform="telegram",identity="@test_sender",receipt_reader="telegram_api",action="send_message",
        target_url="https://t.me/test_group",target_id="test_group",target_author="@test_group",target_text="Test group",text="hi")
    baseline={"api_user_id":"44","peer_id":"-1000000000123","identity_sha256":fingerprint({"account":"@test_sender"}),"last_message_id":10}
    original=reader.client_factory
    def factory(*args):
        client=original(*args)
        message=SimpleNamespace(id=11,out=True,sender_id=44,message="hi",date=datetime.fromtimestamp(now,timezone.utc),media=None,reply_to_msg_id=None,fwd_from=None)
        client.messages=[message]
        if change in {"real_clock_delta", "too_early"}:
            delta = 1.574 if change == "real_clock_delta" else 3
            message.date = datetime.fromtimestamp(now-1-delta, timezone.utc)
        if change == "changed_reread":
            client.changed_reread = True
        if change == "incomplete_page":
            client.messages.extend(SimpleNamespace(**{**vars(message), "id": i, "message": "other"}) for i in range(12, 76))
        if change=="wrong_user":
            client.me.id=45
        if change=="duplicate":
            client.messages.append(SimpleNamespace(**{**vars(message),"id":12}))
        if change=="old_message":
            message.id=10
        if change=="api_write":
            client.attack="SendMessageRequest"
        if change=="api_join":
            client.attack="JoinChannelRequest"
        return client
    reader.client_factory=factory
    async def no_wait(*args):
        pass
    monkeypatch.setattr("auto_promotion.telegram_phone_observer.asyncio.sleep",no_wait)
    if change in {"wrong_user","api_write","api_join"}:
        with pytest.raises(BackendError,match="IdentityMismatch|MutationForbidden"):
            await reader.query("phone",context.identity,context.target_url,baseline=baseline,context=context,since=now-1)
    else:
        receipt=await reader.query("phone",context.identity,context.target_url,baseline=baseline,context=context,since=now-1)
        if change in {"valid", "real_clock_delta"}:
            assert ("exact_ids", [11]) in clients[0].calls
            assert receipt.native_id=="11" and receipt.source=="telegram_api" and receipt.author==context.identity
        else:
            assert receipt is None
    assert clients[-1].raw_mutations==0 and clients[-1].calls[-1]=="disconnect"


@pytest.mark.asyncio
async def test_prepaid_observer_charges_each_rpc_before_read_without_double_charging_account(tmp_path,monkeypatch):
    from auto_promotion.phone_telegram import JOIN_SCHEMA, PhoneTelegram
    from auto_promotion.store import Store

    reader,clients=setup_reader(tmp_path,monkeypatch)
    reservation=reader.reserve_receipt("phone")
    store=Store(tmp_path/"receipt.db")
    with store.connect(write=True) as db:
        db.execute("CREATE TABLE phone_social_authorizations(id TEXT PRIMARY KEY,used_at REAL)")
        for statement in JOIN_SCHEMA.split(";"):
            if statement.strip():
                db.execute(statement)
        db.execute("INSERT INTO phone_social_authorizations VALUES(?,?)",("auth",time.time()))
        db.execute("INSERT INTO phone_telegram_receipt_budgets VALUES(?,?,?,?,?,?,0)",
            ("auth","phone","observer",reservation["read_identity"],reservation["window_end"],reservation["reserved"]))
    original=reader.client_factory
    now=time.time()
    def factory(*args):
        client=original(*args)
        client.messages=[SimpleNamespace(id=11,out=True,sender_id=44,message="hi",date=datetime.fromtimestamp(now,timezone.utc),
            media=None,reply_to_msg_id=None,fwd_from=None)]
        return client
    reader.client_factory=factory
    context=SocialInteraction(platform="telegram",identity="@test_sender",receipt_reader="telegram_api",action="send_message",
        target_url="https://t.me/test_group",target_id="test_group",target_author="@test_group",target_text="Test group",text="hi")
    baseline={"api_user_id":"44","peer_id":"-1000000000123","identity_sha256":fingerprint({"account":"@test_sender"}),"last_message_id":10}
    phone=SimpleNamespace(store=store)
    def consume(reads,count):
        PhoneTelegram.consume_receipt_budget(phone,"auth","phone","observer",reads,count)
    receipt=await reader.query("phone",context.identity,context.target_url,baseline=baseline,context=context,since=now-1,consume=consume)
    assert receipt.native_id=="11"
    assert store.one("SELECT used FROM phone_telegram_receipt_budgets WHERE authorization_id='auth'")["used"]==2
    config,settings=reader._configuration("phone")
    reads=reader._reads(config,settings)
    with reads.connect() as db:
        assert db.execute("SELECT used FROM budgets WHERE identity=?",(reads.identity,)).fetchone()[0]==16
    assert clients[-1].raw_mutations==0


@pytest.mark.asyncio
async def test_observer_without_rpc_guard_fails_before_connecting(tmp_path,monkeypatch):
    reader,_=setup_reader(tmp_path,monkeypatch)
    client=SimpleNamespace()
    reader.client_factory=lambda *args:client
    with pytest.raises(BackendError,match="ReadGuardUnavailable"):
        await reader.query("phone","@test_sender","https://t.me/test_group")
