"""Synthetic MTProto reads only; phone UI actions are never replaced by API writes."""
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telethon import types

from auto_promotion.backends.common import BackendError
from auto_promotion.telegram_phone_observer import (
    TelegramObserverConfig,
    TelegramPhoneObserver,
    same_native_account,
)
from phone_fleet_protocol import fingerprint
from phone_fleet_protocol.social import SocialInteraction


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
    async def connect(self):
        self.calls.append("connect")
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
            await self._call(None,type("SendMessageRequest",(),{})())
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


def test_phone_number_and_handle_identity_must_match_same_api_account():
    me=SimpleNamespace(username="Test_Sender",phone="+1 555 123 4567")
    digest="telegram-phone-sha256:"+fingerprint({"telegram_phone":"15551234567"})
    assert same_native_account(me,digest) and same_native_account(me,"@test_sender")
    assert not same_native_account(me,"@different")


@pytest.mark.parametrize("change",("valid","wrong_user","duplicate","old_message","api_write"))
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
        if change=="wrong_user":
            client.me.id=45
        if change=="duplicate":
            client.messages.append(SimpleNamespace(**{**vars(message),"id":12}))
        if change=="old_message":
            message.id=10
        if change=="api_write":
            client.attack=True
        return client
    reader.client_factory=factory
    async def no_wait(*args):
        pass
    monkeypatch.setattr("auto_promotion.telegram_phone_observer.asyncio.sleep",no_wait)
    if change in {"wrong_user","api_write"}:
        with pytest.raises(BackendError,match="IdentityMismatch|MutationForbidden"):
            await reader.query("phone",context.identity,context.target_url,baseline=baseline,context=context,since=now-1)
    else:
        receipt=await reader.query("phone",context.identity,context.target_url,baseline=baseline,context=context,since=now-1)
        if change=="valid":
            assert receipt.native_id=="11" and receipt.source=="telegram_api" and receipt.author==context.identity
        else:
            assert receipt is None
    assert clients[-1].raw_mutations==0 and clients[-1].calls[-1]=="disconnect"
