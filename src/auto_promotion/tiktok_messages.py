"""Private TikTok inbox observations. Collection never starts or sends a task."""

import math
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator

from .backends.common import read_profile, save_profile
from .meta_browser import fresh
from .models import Model, digest
from .store import Store

DM_ACTIONS = {"send_direct_message", "reply_direct_message"}
HANDLE = r"[A-Za-z0-9_][A-Za-z0-9_.]{0,23}"
NATIVE_ID = r"[0-9]{1,30}"
CONVERSATION_ID = r"0:1:[0-9]{1,30}:[0-9]{1,30}"


def recipient_target(value):
    u = urlparse(value)
    m = re.fullmatch(r"/@(" + HANDLE + r")/?", u.path)
    if (
        u.scheme != "https"
        or u.hostname not in {"tiktok.com", "www.tiktok.com"}
        or u.netloc != u.hostname
        or u.query
        or u.fragment
        or not m
    ):
        raise ValueError("私信需要明确的 TikTok 收件人主页链接")
    return m[1].casefold()


def inbox_url(value):
    u = urlparse(value)
    return (
        u.scheme == "https"
        and u.hostname in {"tiktok.com", "www.tiktok.com"}
        and u.netloc == u.hostname
        and not u.fragment
        and u.path in {"/messages", "/business-suite/messages"}
    )


def dm_event_key(payload):
    key = "dm-recipient:" + recipient_target(payload["target"])
    if payload.get("action") == "reply_direct_message":
        key += ":reply:" + payload.get("tiktok_message_id", "")
    return key


def validate_dm_item(item):
    peer = recipient_target(item["target"])
    if item.get("action") not in DM_ACTIONS or item.get("recipient_id") != peer:
        raise ValueError("私信动作及收件人用户名必须与目标主页一致")
    if item.get("execution_mode", "automatic") != "automatic":
        raise ValueError("TikTok 私信使用已启动的本机桌面任务")
    if (
        item.get("media")
        or item.get("title")
        or item.get("reply_to")
        or item.get("discord_reply_to")
        or item.get("tiktok_video_id")
        or item.get("tiktok_comment_id")
        or item.get("tiktok_options")
    ):
        raise ValueError("TikTok 私信首版仅支持精确文字，不混用视频／评论字段")
    if not item.get("text", "").strip() or len(item["text"]) > 1000:
        raise ValueError("TikTok 私信首版需要 1–1000 字符，页面限制仍需现场核实")
    conversation = item.get("tiktok_conversation_id", "")
    if conversation and not re.fullmatch(CONVERSATION_ID, conversation):
        raise ValueError("私信会话 ID 无效")
    parent = item.get("tiktok_message_id", "")
    if item["action"] == "reply_direct_message":
        if not conversation or not re.fullmatch(NATIVE_ID, parent):
            raise ValueError("私信回复需要已核实的会话 ID 和原消息 ID")
    elif parent:
        raise ValueError("主动私信不使用回复消息 ID")
    return peer


class DMMessage(Model):
    message_id: str = Field(default="", pattern=r"^(?:|[0-9]{1,30})$")
    id_verified: bool = False
    direction: Literal["inbound", "outbound", "unknown"] = "unknown"
    sender_username: str = Field(default="", max_length=24)
    text: str = Field(default="", max_length=5000)
    occurred_at: float | None = None
    timestamp_verified: bool = False
    timestamp_label: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def coherent(self):
        if self.id_verified and not self.message_id:
            raise ValueError("已核实消息需要平台原生 ID")
        if self.occurred_at is not None and (
            not self.timestamp_verified
            or not math.isfinite(self.occurred_at)
            or self.occurred_at <= 0
        ):
            raise ValueError("平台时间缺失或未核实时不得补造时间")
        return self


class DMConversation(Model):
    conversation_id: str = Field(pattern="^" + CONVERSATION_ID + "$")
    peer_username: str = Field(pattern="^" + HANDLE + "$")
    peer_verified: bool = False
    source_url: str = Field(max_length=1000)
    messages: list[DMMessage] = Field(default_factory=list, max_length=200)


class DMCollection(Model):
    identity: str
    browser: str
    instance_id: str = ""
    observed_at: float
    authenticated_menu_verified: bool
    identity_verified: bool
    evidence: str = Field(min_length=1, max_length=2000)
    source_url: str = Field(max_length=1000)
    inbox_verified: bool
    coverage: Literal["partial", "complete"] = "partial"
    conversations: list[DMConversation] = Field(default_factory=list, max_length=50)


class TikTokMessages:
    def __init__(self, browser):
        self.browser, self.store, self.clock = browser, browser.store, browser.clock

    def collect(self, account_id, snapshot):
        connection = self.browser.connection(account_id)
        value = DMCollection.model_validate(snapshot)
        if not (
            fresh(value.model_dump(), self.clock())
            and value.identity_verified
            and value.authenticated_menu_verified
            and value.inbox_verified
            and value.identity == connection["identity"]
            and value.browser == connection["browser"]
            and value.instance_id == connection["details"].get("instance_id", "")
            and inbox_url(value.source_url)
        ):
            raise ValueError("私信采集需要当前账号、浏览器实例和真实收件箱依据")
        # Validate the whole batch before writing any private observation.
        peers, ids = {}, {}
        for conversation in value.conversations:
            peer = conversation.peer_username.casefold()
            if (
                conversation.conversation_id in peers
                and peers[conversation.conversation_id] != peer
            ):
                raise ValueError("同一采集批次中的会话身份冲突")
            peers[conversation.conversation_id] = peer
            if (
                not conversation.peer_verified
                or peer == value.identity
                or not inbox_url(conversation.source_url)
            ):
                raise ValueError("私信会话必须核实对方用户名及来源页面")
            old = self.store.one(
                "SELECT peer_username FROM tiktok_dm_conversations WHERE account_id=? AND conversation_id=?",
                (account_id, conversation.conversation_id),
            )
            if old and old["peer_username"] != peer:
                raise ValueError("已有私信会话身份不可变化")
            for message in conversation.messages:
                if message.id_verified:
                    previous = self.store.one(
                        "SELECT conversation_id,direction,sender_username FROM tiktok_dm_messages WHERE account_id=? AND message_id=? AND id_verified=1",
                        (account_id, message.message_id),
                    )
                    if (
                        previous and previous["conversation_id"] != conversation.conversation_id
                    ) or (
                        message.message_id in ids
                        and ids[message.message_id] != conversation.conversation_id
                    ):
                        raise ValueError("私信原生消息 ID 不能迁移到其他会话")
                    if previous and (
                        previous["direction"] != message.direction
                        or previous["sender_username"] != message.sender_username.casefold()
                    ):
                        raise ValueError("已有私信消息的作者和方向不能变化")
                    ids[message.message_id] = conversation.conversation_id
                expected = value.identity if message.direction == "outbound" else peer
                if (
                    message.direction != "unknown"
                    and message.sender_username.casefold() != expected
                ):
                    raise ValueError("私信发送方向与消息作者不一致")
                if message.occurred_at is not None and message.occurred_at > value.observed_at + 60:
                    raise ValueError("私信平台时间不能晚于实际观察")
        seen = 0
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO tiktok_dm_collections VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET collected_at=excluded.collected_at,coverage=excluded.coverage,source_url=excluded.source_url",
                (account_id, self.clock(), self.clock(), value.coverage, value.source_url),
            )
            for conversation in value.conversations:
                peer = conversation.peer_username.casefold()
                db.execute(
                    "INSERT INTO tiktok_dm_conversations VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id,conversation_id) DO UPDATE SET source_url=excluded.source_url,coverage=excluded.coverage,last_seen=excluded.last_seen",
                    (
                        account_id,
                        conversation.conversation_id,
                        peer,
                        conversation.source_url,
                        value.coverage,
                        self.clock(),
                        self.clock(),
                    ),
                )
                for message in conversation.messages:
                    key = (
                        message.message_id
                        if message.id_verified
                        else "unverified:"
                        + digest(
                            [
                                message.sender_username,
                                message.text,
                                message.timestamp_label,
                                message.direction,
                            ]
                        )
                    )
                    db.execute(
                        "INSERT INTO tiktok_dm_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,conversation_id,message_key) DO UPDATE SET text=excluded.text,last_seen=excluded.last_seen",
                        (
                            account_id,
                            conversation.conversation_id,
                            key,
                            message.message_id,
                            message.direction,
                            message.sender_username.casefold(),
                            message.text,
                            message.occurred_at,
                            message.timestamp_label,
                            int(message.id_verified),
                            self.clock(),
                            self.clock(),
                        ),
                    )
                    seen += 1
            Store.audit(
                db,
                "tiktok_dm.collected",
                {
                    "account_id": account_id,
                    "conversations": len(value.conversations),
                    "observed_messages": seen,
                    "coverage": value.coverage,
                },
                now=self.clock(),
            )
        return {
            "account_id": account_id,
            "connected": True,
            "conversations_observed": len(value.conversations),
            "messages_observed": seen,
            "coverage": value.coverage,
            "execution_started": False,
        }

    def inbox(self, account_id):
        self.browser.connection(account_id)
        return {
            "collection": self.store.one(
                "SELECT collected_at,coverage FROM tiktok_dm_collections WHERE account_id=?",
                (account_id,),
            ),
            "conversations": self.store.rows(
                "SELECT c.*,count(m.message_key) message_count FROM tiktok_dm_conversations c LEFT JOIN tiktok_dm_messages m ON m.account_id=c.account_id AND m.conversation_id=c.conversation_id WHERE c.account_id=? GROUP BY c.account_id,c.conversation_id ORDER BY c.last_seen DESC LIMIT 100",
                (account_id,),
            ),
        }

    def messages(self, account_id, conversation_id, limit=100):
        self.browser.connection(account_id)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("单次最多读取 200 条私信")
        return {
            "messages": self.store.rows(
                "SELECT message_id,id_verified,direction,sender_username,text,occurred_at,timestamp_label,first_seen FROM tiktok_dm_messages WHERE account_id=? AND conversation_id=? ORDER BY first_seen,message_key LIMIT ?",
                (account_id, conversation_id, limit),
            ),
            "coverage": "partial",
        }

    def enable(self, account_id, enabled=True):
        self.browser.connection(account_id)
        state = self.store.one(
            "SELECT collected_at FROM tiktok_dm_collections WHERE account_id=?", (account_id,)
        )
        if enabled and (not state or self.clock() - state["collected_at"] > 300):
            raise ValueError("请先采集并核实当前账号的真实私信收件箱")
        account = self.browser.engine.account(account_id)
        root = os.getenv("AP_ACCOUNTS_DIR", str(Path(self.store.path).parent / "accounts"))
        profile = read_profile("tiktok", account["backend_label"], accounts_dir=root)
        profile["direct_messages_enabled"] = bool(enabled)
        save_profile("tiktok", account["backend_label"], profile, accounts_dir=root)
        with self.store.connect(write=True) as db:
            Store.audit(
                db,
                "tiktok_dm.capability",
                {"account_id": account_id, "enabled": bool(enabled), "live_switch_changed": False},
                now=self.clock(),
            )
        return {"account": self.browser.engine.account(account_id), "execution_started": False}
