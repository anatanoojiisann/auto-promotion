"""TikTok UI tasks on the shared desktop queue. No credentials or hidden API calls.

The desktop agent observes the real page, claims one explicitly started item,
uses the one-use guard immediately before submitting, then reads its receipt.
"""

import json
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

from pydantic import Field, field_validator, model_validator

from .backends.common import BackendError, account_lock, read_profile, save_profile
from .meta_browser import MetaBrowser, fresh
from .models import AccountInput, Model, canonical
from .store import Store
from .tiktok_messages import DM_ACTIONS, TikTokMessages, inbox_url, validate_dm_item

USERNAME = r"[A-Za-z0-9_][A-Za-z0-9_.]{0,23}"
IDENTIFIER = r"[0-9]{1,30}"


def video_target(value):
    """Require a canonical video URL; resolve short links in the browser first."""
    u = urlparse(value)
    match = re.fullmatch(r"/@(" + USERNAME + r")/video/(" + IDENTIFIER + r")/?", u.path)
    if (
        u.scheme != "https"
        or u.hostname not in {"www.tiktok.com", "tiktok.com"}
        or u.netloc != u.hostname
        or u.fragment
        or not match
    ):
        raise ValueError(
            "需要完整 TikTok HTTPS 视频链接：https://www.tiktok.com/@用户名/video/视频ID"
        )
    query = parse_qs(u.query, keep_blank_values=True)
    comments = query.get("comment_id", [])
    if comments and (len(comments) != 1 or not re.fullmatch(IDENTIFIER, comments[0])):
        raise ValueError("TikTok 评论链接需要唯一的数字 comment_id")
    return {
        "video_id": match[2],
        "author": match[1],
        "comment_id": comments[0] if comments else "",
        "url": "https://www.tiktok.com/@" + match[1] + "/video/" + match[2],
    }


def validate_browser_item(item):
    if item["action"] in DM_ACTIONS:
        return validate_dm_item(item)
    target = video_target(item["target"])
    if item["action"] not in {"comment", "reply"}:
        raise ValueError("TikTok 网页首版仅支持文字评论和回复")
    if item.get("execution_mode", "automatic") != "automatic":
        raise ValueError("TikTok 网页队列使用桌面执行与独立回执核验")
    if (
        item.get("media")
        or item.get("title")
        or item.get("tiktok_options")
        or item.get("reply_to")
        or item.get("discord_reply_to")
        or item.get("recipient_id")
        or item.get("tiktok_conversation_id")
        or item.get("tiktok_message_id")
    ):
        raise ValueError("TikTok 网页评论仅支持精确文字和视频／评论目标")
    # This is a conservative local cap, not a statement about every TikTok account.
    if not item.get("text", "").strip() or len(item["text"]) > 150:
        raise ValueError("TikTok 网页首版评论需为 1–150 字符；提交前仍检查页面限制")
    if item.get("tiktok_video_id") != target["video_id"]:
        raise ValueError("视频 ID 必须与 TikTok 目标链接一致")
    parent = item.get("tiktok_comment_id", "")
    if item["action"] == "reply":
        if not re.fullmatch(IDENTIFIER, parent) or target["comment_id"] != parent:
            raise ValueError("回复需要带 comment_id 的评论链接，并与目标评论 ID 一致")
    elif parent or target["comment_id"]:
        raise ValueError("发表顶层评论请使用视频链接；回复指定评论请选择回复动作")
    return target


class TikTokBrowserConnect(Model):
    label: str = Field(min_length=1, max_length=80)
    backend_label: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    username: str = Field(pattern="^" + USERNAME + "$")
    browser: Literal["tabbit", "chrome", "edge"]
    instance_id: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9_.:-]*$")

    @field_validator("label")
    @classmethod
    def clean_label(cls, value):
        if not value.strip() or any(c in value for c in "\r\n\x00"):
            raise ValueError("账号名称不能为空或包含控制字符")
        return value.strip()

    @model_validator(mode="after")
    def require_instance(self):
        if self.browser == "tabbit" and not self.instance_id:
            raise ValueError("Tabbit 连接需要当前浏览器实例 ID")
        return self


class TikTokBrowserGuard(Model):
    owner: str = Field(min_length=1, max_length=100)
    proof: dict


class TikTokBrowserResult(Model):
    owner: str | None = None
    receipt: dict


class TikTokBrowser(MetaBrowser):
    # The shared desktop tables hold no platform secrets; Meta views filter by platform.
    platforms = ("tiktok",)

    @property
    def messages(self):
        return TikTokMessages(self)

    def sync(self):
        return {"updated": 0}

    def release_pending(self):
        return False

    def connection(self, account_id):
        row = self.store.one("SELECT platform FROM accounts WHERE id=?", (account_id,))
        if not row or row["platform"] != "tiktok":
            raise ValueError("请选择 TikTok 网页账号")
        return super().connection(account_id)

    def status(self):
        result = super().status()
        for account in result["accounts"]:
            account["instance_id"] = self.connection(account["account_id"])["details"].get(
                "instance_id", ""
            )
            account["direct_messages_enabled"] = (
                "send_direct_message"
                in self.engine.account(account["account_id"])["automatic_actions"]
            )
            account["dm_collection"] = self.store.one(
                "SELECT collected_at,coverage FROM tiktok_dm_collections WHERE account_id=?",
                (account["account_id"],),
            )
        return result

    def pending(self):
        result = super().pending()
        result["feedback_jobs"] = [
            j for j in result["feedback_jobs"] if j["payload"]["action"] not in DM_ACTIONS
        ]
        present = {i["item_id"] for i in result["unresolved"]}
        rows = self.store.rows(
            "SELECT i.id item_id,i.task_id,i.account_id,i.status,i.external_reference,i.result,i.payload FROM items i JOIN accounts a ON a.id=i.account_id LEFT JOIN meta_browser_checks c ON c.item_id=i.id WHERE a.platform='tiktok' AND (i.status='unknown' OR (i.status='cancelled' AND json_extract(i.result,'$.reason')='UserSkippedUnknownSubmission' AND EXISTS (SELECT 1 FROM meta_browser_claims mc WHERE mc.item_id=i.id AND mc.submission_started=1))) AND json_extract(i.payload,'$.action') IN ('send_direct_message','reply_direct_message') AND COALESCE(c.checked_at,0)<=? LIMIT 30",
            (self.clock() - 60,),
        )
        for row in rows:
            if row["item_id"] in present:
                continue
            row["payload"] = json.loads(row["payload"])
            row["result"] = json.loads(row["result"])
            claim = self.store.one(
                "SELECT owner FROM meta_browser_claims WHERE item_id=?", (row["item_id"],)
            )
            row["owner"] = claim["owner"] if claim else None
            row["connection"] = {
                k: self.connection(row["account_id"])[k]
                for k in ("identity", "profile_url", "connector", "browser")
            }
            result["unresolved"].append(row)
        for item in result["unresolved"]:
            if item["payload"]["action"] in DM_ACTIONS:
                row = self.store.one(
                    "SELECT proof FROM meta_browser_claims WHERE item_id=?", (item["item_id"],)
                )
                if row:
                    saved = json.loads(row["proof"])
                    item["private_attempt"] = {
                        k: saved[k]
                        for k in (
                            "recipient_id",
                            "conversation_id",
                            "source_url",
                            "before_message_ids",
                        )
                    }
        return result

    def feedback(self, item_id, evidence):
        item = self.store.one("SELECT payload FROM items WHERE id=?", (item_id,))
        if item and json.loads(item["payload"])["action"] in DM_ACTIONS:
            raise ValueError("私信消息通过独立收件箱采集，不冒充公开评论的直接回复覆盖")
        return super().feedback(item_id, evidence)

    def connect(self, value: TikTokBrowserConnect):
        """Enroll only metadata. Identity must subsequently be observed in the real UI."""
        root = os.getenv("AP_ACCOUNTS_DIR", str(Path(self.store.path).parent / "accounts"))
        with account_lock("tiktok", "__browser_registration__", accounts_dir=root):
            return self._connect(value, root)

    def _connect(self, value, root):
        from .core import Conflict

        username = value.username.casefold()
        profile_url = "https://www.tiktok.com/@" + username
        expected = {
            "identity": username,
            "connection_type": "tiktok_desktop",
            "browser_channel": "tabbit" if value.browser == "tabbit" else "browser_use",
            "browser": value.browser,
            "instance_id": value.instance_id,
            "automatic_actions": ["comment", "reply"],
        }
        try:
            previous = read_profile("tiktok", value.backend_label, accounts_dir=root)
        except BackendError as exc:
            if exc.safe_code != "AccountNotConfigured":
                raise
            previous = None
        if previous and any(previous.get(k) != v for k, v in expected.items()):
            raise Conflict("已有账号身份、浏览器或 API 连接不可覆盖，请使用新的本机标签")
        account = self.store.one(
            "SELECT * FROM accounts WHERE platform='tiktok' AND backend_label=? AND mode='live'",
            (value.backend_label,),
        )
        duplicate = self.store.one(
            "SELECT b.account_id FROM meta_browser_accounts b JOIN accounts a ON a.id=b.account_id "
            "WHERE a.platform='tiktok' AND b.identity=?",
            (username,),
        )
        if duplicate and (not account or account["id"] != duplicate["account_id"]):
            raise Conflict("该 TikTok 网页身份已登记，不创建重复执行账号")
        if account and not self.store.one(
            "SELECT 1 FROM meta_browser_accounts WHERE account_id=?", (account["id"],)
        ):
            raise Conflict("已有账号连接不会改为网页通道，请使用新的本机标签")
        if not account:
            account = self.engine.add_account(
                AccountInput(
                    platform="tiktok",
                    label=value.label,
                    backend_label=value.backend_label,
                    daily_limit=5,
                    hourly_limit=2,
                    min_interval=600,
                )
            )
        save_profile("tiktok", value.backend_label, expected, accounts_dir=root)
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO meta_browser_accounts VALUES(?,?,?,?,?,?,?,'{}',?) "
                "ON CONFLICT(account_id) DO NOTHING",
                (
                    account["id"],
                    "tiktok-web:" + username,
                    username,
                    profile_url,
                    "tabbit" if value.browser == "tabbit" else "browser_use",
                    value.browser,
                    canonical(
                        {
                            "minimum_interval": account["min_interval"],
                            "maximum_interval": account["min_interval"],
                            "instance_id": value.instance_id,
                        }
                    ),
                    self.clock(),
                ),
            )
            Store.audit(
                db, "tiktok_browser.enrolled", {"account_id": account["id"]}, now=self.clock()
            )
        return {"account": self.engine.account(account["id"]), "execution_started": False}

    def observe(self, account_id, proof):
        connection = self.connection(account_id)
        if proof.get("instance_id", "") != connection["details"].get("instance_id", ""):
            raise ValueError("浏览器实例与登记连接不一致")
        if not fresh(proof, self.clock()):
            raise ValueError("TikTok 身份观察已过期")
        blocked = proof.get("blocked_reason")
        if blocked:
            if blocked not in {
                "BrowserLoginRequired",
                "BrowserVerificationRequired",
                "BrowserAccountRestricted",
                "BrowserRateLimited",
                "BrowserAccessDenied",
            }:
                raise ValueError("无效的浏览器状态")
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE meta_browser_accounts SET observation=?,updated_at=? WHERE account_id=?",
                    (
                        canonical({"blocked_reason": blocked, "observed_at": self.clock()}),
                        self.clock(),
                        account_id,
                    ),
                )
            self.engine.check_account(account_id, force=True)
            return {"account_id": account_id, "blocked_reason": blocked, "healthy": False}
        # A public profile page alone cannot establish the logged-in identity.
        if proof.get("authenticated_menu_verified") is not True:
            raise ValueError("需要从当前登录菜单与个人主页共同核实 TikTok 身份")
        return super().observe(account_id, proof)

    def backend(self, account, operation, payload):
        if operation == "health":
            observation = self.connection(account["id"])["observation"]
            if observation.get("blocked_reason"):
                return {
                    "healthy": False,
                    "service_ready": True,
                    "identity_verified": False,
                    "permissions": [],
                    "reason": observation["blocked_reason"],
                }
        result = super().backend(account, operation, payload)
        if operation == "health" and result.get("healthy"):
            from .backends.common import automatic_actions

            result["permissions"] = automatic_actions(account)
        return result

    def _context(self, item_id, proof):
        item = self.store.one("SELECT payload,account_id FROM items WHERE id=?", (item_id,))
        if not item:
            raise ValueError("执行项不存在")
        connection = self.connection(item["account_id"])
        payload = json.loads(item["payload"])
        validate_browser_item(payload)
        if payload["action"] in DM_ACTIONS:
            self._dm_context(item_id, payload, connection, proof)
            return
        if payload["action"] == "reply" and (
            proof.get("parent_id_verified") is not True
            or not re.fullmatch(r"/@" + USERNAME + r"/?", proof.get("parent_author_profile", ""))
        ):
            raise ValueError("回复需要独立核实父评论 ID 与作者；页面未提供 ID 时保持等待")
        if not (
            fresh(proof, self.clock())
            and proof.get("identity") == connection["identity"]
            and proof.get("browser") == connection["browser"]
            and proof.get("payload_hash")
            == self.store.one("SELECT payload_hash FROM items WHERE id=?", (item_id,))[
                "payload_hash"
            ]
            and proof.get("target") == payload["target"]
            and all(
                proof.get(k) is True
                for k in ("identity_verified", "context_verified", "can_reply", "no_duplicate")
            )
            and proof.get("blocked_reason") in {None, ""}
            and proof.get("video_id") == payload["tiktok_video_id"]
            and proof.get("parent_comment_id", "") == payload["tiktok_comment_id"]
            and proof.get("authenticated_menu_verified") is True
            and proof.get("instance_id", "") == connection["details"].get("instance_id", "")
            and proof.get("editor_text") == payload["text"]
        ):
            raise ValueError("提交前需核对 TikTok 视频、父评论、当前登录身份与输入框全文")

    def begin(self, item_id, proof):
        self._context(item_id, proof)
        return super().begin(item_id, proof)

    def guard(self, item_id, owner, proof):
        # Fresh UI proof is required immediately before each one-use permit.
        self._context(item_id, proof)
        permit = super().guard(item_id, owner)
        row = self.store.one(
            "SELECT expires_at FROM meta_browser_claims WHERE item_id=?", (item_id,)
        )
        return {
            **permit,
            "platform": "tiktok",
            "payload_hash": proof["payload_hash"],
            "target": proof["target"],
            "identity": proof["identity"],
            "browser": proof["browser"],
            "instance_id": proof.get("instance_id", ""),
            "expires_at": row["expires_at"],
        }

    def _dm_context(self, item_id, payload, connection, proof):
        peer = validate_dm_item(payload)
        row = self.store.one("SELECT payload_hash FROM items WHERE id=?", (item_id,))
        before = proof.get("before_message_ids")
        if not (
            fresh(proof, self.clock())
            and proof.get("identity") == connection["identity"]
            and proof.get("browser") == connection["browser"]
            and proof.get("instance_id", "") == connection["details"].get("instance_id", "")
            and proof.get("payload_hash") == row["payload_hash"]
            and proof.get("target") == payload["target"]
            and proof.get("recipient_id") == peer
            and proof.get("editor_text") == payload["text"]
            and inbox_url(proof.get("source_url", ""))
            and re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", proof.get("conversation_id", ""))
            and (
                not payload.get("tiktok_conversation_id")
                or proof["conversation_id"] == payload["tiktok_conversation_id"]
            )
            and all(
                proof.get(k) is True
                for k in (
                    "identity_verified",
                    "authenticated_menu_verified",
                    "recipient_verified",
                    "conversation_verified",
                    "context_verified",
                    "can_reply",
                    "can_message",
                    "no_duplicate",
                )
            )
            and not proof.get("blocked_reason")
            and isinstance(before, list)
            and len(before) <= 200
            and all(isinstance(i, str) and re.fullmatch(IDENTIFIER, i) for i in before)
        ):
            raise ValueError("私信提交前需核对当前账号、收件人、会话、输入全文及页面发送权限")
        if peer == connection["identity"]:
            raise ValueError("不能向当前账号自己发送私信")
        if payload["action"] == "reply_direct_message":
            parent = self.store.one(
                "SELECT direction,id_verified FROM tiktok_dm_messages WHERE account_id=? AND conversation_id=? AND message_id=?",
                (
                    payload["account_id"],
                    payload["tiktok_conversation_id"],
                    payload["tiktok_message_id"],
                ),
            )
            if (
                not parent
                or parent["direction"] != "inbound"
                or not parent["id_verified"]
                or proof.get("parent_message_id") != payload["tiktok_message_id"]
            ):
                raise ValueError("回复需要已采集并核实的对方原私信消息")
        claim = self.store.one("SELECT proof FROM meta_browser_claims WHERE item_id=?", (item_id,))
        if claim and set(json.loads(claim["proof"])["before_message_ids"]) != set(before):
            raise ValueError("领取后会话的外发消息已变化，请保持未决并重新核对")

    def extra_proof(self, payload, proof):
        if payload["action"] not in DM_ACTIONS:
            return {}
        return {
            k: proof[k]
            for k in ("recipient_id", "conversation_id", "source_url", "before_message_ids")
        }

    def same_receipt_target(self, account, payload, reference, receipt):
        if payload["action"] in DM_ACTIONS:
            return inbox_url(reference)
        return super().same_receipt_target(account, payload, reference, receipt)

    def receipt_details(self, payload, receipt):
        if payload["action"] not in DM_ACTIONS:
            return {}
        return {
            "interaction_channel": "direct_message",
            "message_id": receipt["message_id"],
            "conversation_id": receipt["conversation_id"],
            "recipient_id": receipt["recipient_id"],
        }

    def persist_receipt(self, db, item, result, receipt):
        if result.get("interaction_channel") != "direct_message":
            return
        from .core import Conflict

        previous = db.execute(
            "SELECT item_id FROM tiktok_dm_sent_receipts WHERE account_id=? AND conversation_id=? AND message_id=?",
            (item["account_id"], result["conversation_id"], result["message_id"]),
        ).fetchone()
        if previous and previous["item_id"] != item["id"]:
            raise Conflict("此私信原生回执已绑定其他条目，不重复计数")
        db.execute(
            "INSERT OR IGNORE INTO tiktok_dm_sent_receipts VALUES(?,?,?,?,?)",
            (
                item["id"],
                item["account_id"],
                result["conversation_id"],
                result["message_id"],
                self.clock(),
            ),
        )

    def result(self, item_id, receipt, owner=None):
        item = self.store.one(
            "SELECT account_id,payload,claimed_at,status FROM items WHERE id=?", (item_id,)
        )
        if (
            item
            and json.loads(item["payload"])["action"] in DM_ACTIONS
            and receipt.get("status") == "confirmed"
        ):
            from .core import Conflict

            row = self.store.one(
                "SELECT proof FROM meta_browser_claims WHERE item_id=?", (item_id,)
            )
            proof = json.loads(row["proof"]) if row else {}
            if (
                not re.fullmatch(IDENTIFIER, receipt.get("message_id", ""))
                or receipt["message_id"] in proof.get("before_message_ids", [])
                or receipt.get("conversation_id") != proof.get("conversation_id")
                or receipt.get("observed_at", 0) < (item["claimed_at"] or 0)
                or receipt.get("direction") != "outbound"
            ):
                raise Conflict("私信回执需为领取后独立读取的新外发消息，不能使用已有消息")
        return super().result(item_id, receipt, owner)

    def receipt_target(self, account, payload, reference, receipt):
        if payload["action"] in DM_ACTIONS:
            return bool(
                inbox_url(reference)
                and receipt.get("recipient_id") == payload["recipient_id"]
                and receipt.get("recipient_verified") is True
                and receipt.get("message_id_verified") is True
                and receipt.get("independent_read") is True
                and receipt.get("parent_message_id", "") == payload.get("tiktok_message_id", "")
            )
        try:
            target = video_target(reference)
        except ValueError:
            return False
        return bool(
            target["comment_id"]
            and target["comment_id"] != payload.get("tiktok_comment_id")
            and receipt.get("comment_id") == target["comment_id"]
            and receipt.get("video_id") == payload["tiktok_video_id"] == target["video_id"]
            and receipt.get("parent_comment_id", "") == payload["tiktok_comment_id"]
            and receipt.get("independent_read") is True
        )

    def _legacy_result(self, claim_row, item, result):
        # TikTok has no imported legacy coordinator to update.
        return
