"""TikTok application settings, single-use OAuth and read-only account tools."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from pydantic import Field, SecretStr, field_validator, model_validator

from .backends.common import BackendError, account_lock, read_profile, save_profile
from .backends.tiktok import (
    APP_LABEL,
    COMMENT_ACTIONS,
    TikTokHTTP,
    comment_binding,
    scope_set,
    secure_url,
    token_settings,
)
from .campaigns import Campaigns
from .core import Conflict
from .models import AccountInput, Model, OpportunityInput, canonical
from .store import Store


class TikTokAppInput(Model):
    client_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")
    client_secret: SecretStr = Field(min_length=1, max_length=8192)
    redirect_uri: str = Field(min_length=10, max_length=512)
    authorization_url: str = Field(max_length=4000)
    media_url_prefixes: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def valid(self):
        redirect = secure_url(self.redirect_uri)
        if redirect.query or redirect.port is not None or not redirect.path.endswith("/"):
            raise ValueError("回调地址不能带查询参数")
        auth = secure_url(self.authorization_url)
        if auth.hostname != "www.tiktok.com" or auth.path.rstrip("/") != "/v2/auth/authorize":
            raise ValueError("请使用开发者后台提供的 TikTok 账号持有人授权 URL")
        params = parse_qs(auth.query, keep_blank_values=True)
        if (
            params.get("client_key") != [self.client_id]
            or params.get("redirect_uri") != [self.redirect_uri]
            or params.get("response_type") != ["code"]
            or not params.get("scope")
        ):
            raise ValueError("授权 URL 的应用 ID、回调地址或权限不匹配")
        if any(
            k in params
            for k in ("client_secret", "access_token", "refresh_token", "auth_code", "code")
        ):
            raise ValueError("授权 URL 不能包含凭据")
        for prefix in self.media_url_prefixes:
            parsed = secure_url(prefix)
            if parsed.query or not parsed.path.endswith("/"):
                raise ValueError("素材前缀必须以 / 结尾，且不带查询参数")
        return self


class TikTokAuthorizeInput(Model):
    backend_label: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=1, max_length=24)

    @field_validator("username")
    @classmethod
    def username_format(cls, value):
        import re

        value = value.strip().removeprefix("@").lower()
        if not re.fullmatch(r"[a-z0-9_.]{1,24}", value):
            raise ValueError("请填写 TikTok 用户名")
        return value


class TikTokCompleteInput(Model):
    callback_url: SecretStr = Field(min_length=1, max_length=8192)


class TikTokReadInput(Model):
    cursor: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=30)
    video_id: str = Field(default="", pattern=r"^(?:|[0-9]{1,30})$")
    import_to_pool: bool = False
    comment_id: str = Field(default="", pattern=r"^(?:|[1-9][0-9]{0,29})$")
    comment_url: str = Field(default="", max_length=1000)


class TikTokInteractionInput(Model):
    task_id: str = Field(default="", max_length=100)
    opportunity_id: str = Field(default="", max_length=100)
    action: str = Field(max_length=40)
    video_id: str = Field(default="", max_length=30)
    parent_comment_id: str = Field(default="", max_length=30)
    comment_url: str = Field(default="", max_length=1000)
    target_owned: bool = False


class TikTokWebhookInput(Model):
    event_type: Literal["VIDEO", "COMMENT"]


class TikTokManager:
    def __init__(self, engine, *, http_factory=TikTokHTTP):
        self.engine, self.store, self.clock = engine, engine.store, engine.clock
        self.http_factory = http_factory
        self.accounts_dir = Path(
            os.getenv("AP_ACCOUNTS_DIR", Path(self.store.path).parent / "accounts")
        ).resolve()

    def app(self):
        try:
            return read_profile("tiktok", APP_LABEL, accounts_dir=self.accounts_dir)
        except BackendError as exc:
            if exc.safe_code == "AccountNotConfigured":
                raise Conflict("请先配置 TikTok 开发者应用") from None
            raise

    def status(self):
        try:
            app = self.app()
        except Conflict:
            return {"configured": False}
        return {
            "configured": True,
            "client_id": app["identity"],
            "redirect_uri": app["redirect_uri"],
            "media_url_prefixes": app["media_url_prefixes"],
            "authorization_url": app["authorization_url"],
        }

    def save_app(self, value):
        with account_lock("tiktok", APP_LABEL, accounts_dir=self.accounts_dir):
            save_profile(
                "tiktok",
                APP_LABEL,
                {
                    "identity": value.client_id,
                    "client_secret": value.client_secret.get_secret_value(),
                    "redirect_uri": value.redirect_uri,
                    "authorization_url": value.authorization_url,
                    "media_url_prefixes": value.media_url_prefixes,
                },
                accounts_dir=self.accounts_dir,
            )
        with self.store.connect(write=True) as db:
            Store.audit(
                db, "tiktok.app.configured", {"client_id": value.client_id}, now=self.clock()
            )
        return self.status()

    def authorize(self, value):
        app = self.app()
        if value.backend_label == APP_LABEL or value.backend_label.startswith("__tiktok_"):
            raise Conflict("请使用普通账号标签")
        AccountInput(platform="tiktok", label=value.username, backend_label=value.backend_label)
        state = secrets.token_urlsafe(32)
        identifier = hashlib.sha256(state.encode()).hexdigest()
        with self.store.connect(write=True) as db:
            db.execute("DELETE FROM tiktok_oauth WHERE expires_at<?", (self.clock() - 86400,))
            db.execute(
                "INSERT INTO tiktok_oauth VALUES(?,?,?,?,?,0)",
                (
                    identifier,
                    self.clock() + 600,
                    app["identity"],
                    app["redirect_uri"],
                    canonical(value.model_dump()),
                ),
            )
        url = urlparse(app["authorization_url"])
        params = parse_qs(url.query)
        params["state"] = [state]
        return {
            "authorization_url": urlunparse(url._replace(query=urlencode(params, doseq=True))),
            "expires_at": self.clock() + 600,
        }

    def configure_webhook(self, value):
        app = self.app()
        callback = urlunparse(
            urlparse(app["redirect_uri"])._replace(path="/webhooks/tiktok", query="", fragment="")
        )
        result = self.http_factory().request(
            "business/webhook/update/",
            body={
                "app_id": app["identity"],
                "secret": app["client_secret"],
                "event_type": value.event_type,
                "callback_url": callback,
            },
        )
        if (
            str(result.get("app_id")) != app["identity"]
            or result.get("event_type") != value.event_type
            or result.get("callback_url") != callback
        ):
            raise BackendError("TikTokWebhookConfigurationUnverified")
        safe = {
            k: result[k]
            for k in ("app_id", "event_type", "callback_url", "item_list")
            if k in result
        }
        with self.store.connect(write=True) as db:
            Store.audit(db, "tiktok.webhook.configured", safe, now=self.clock())
        return safe

    def complete(self, callback_url):
        app = self.app()
        try:
            url = urlparse(callback_url)
            expected = urlparse(app["redirect_uri"])
            if (url.scheme, url.netloc, url.path) != (
                expected.scheme,
                expected.netloc,
                expected.path,
            ) or url.fragment:
                raise ValueError
            params = parse_qs(url.query, keep_blank_values=True)
            if any(len(v) != 1 for v in params.values()):
                raise ValueError
            state = params.get("state", [""])[0]
            if not state or len(state) > 200:
                raise ValueError
        except (ValueError, TypeError):
            raise Conflict("TikTok 回调地址或 state 无效") from None
        identifier = hashlib.sha256(state.encode()).hexdigest()
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM tiktok_oauth WHERE id=?", (identifier,)).fetchone()
            if (
                not row
                or row["used"]
                or row["expires_at"] <= self.clock()
                or row["client_id"] != app["identity"]
                or row["redirect_uri"] != app["redirect_uri"]
            ):
                raise Conflict("TikTok 授权已失效或已使用，请重新发起授权")
            db.execute("UPDATE tiktok_oauth SET used=1 WHERE id=? AND used=0", (identifier,))
            details = json.loads(row["details"])
        if params.get("error"):
            raise Conflict("TikTok 授权未完成，请重新授权")
        codes = params.get("auth_code") or params.get("code")
        if not codes or not codes[0]:
            raise Conflict("TikTok 回调缺少授权码，请重新授权")
        # Mark before exchange. An uncertain one-time code exchange must not be retried.
        data = self.http_factory().request(
            "tt_user/oauth2/token/",
            body={
                "client_id": app["identity"],
                "client_secret": app["client_secret"],
                "redirect_uri": app["redirect_uri"],
                "grant_type": "authorization_code",
                "auth_code": codes[0],
            },
        )
        profile = token_settings(data, now=self.clock())
        profile["client_id"] = app["identity"]
        http = self.http_factory(profile["access_token"])
        identity = http.request(
            "business/get/",
            params={"business_id": profile["identity"], "fields": ["username", "display_name"]},
        )
        if str(identity.get("username", "")).lower() != details["username"]:
            raise BackendError("TikTokIdentityMismatch")
        profile["username"] = identity["username"]
        inspector = self.http_factory().request(
            "tt_user/token_info/get/",
            body={"app_id": app["identity"], "access_token": profile["access_token"]},
        )
        if (
            inspector.get("creator_id") != profile["identity"]
            or str(inspector.get("app_id")) != app["identity"]
        ):
            raise BackendError("TikTokIdentityMismatch")
        if not {"user.info.basic", "user.info.username"} <= scope_set(inspector.get("scope")):
            raise BackendError("TikTokPermissionDenied")
        label = details["backend_label"]
        with account_lock("tiktok", label, accounts_dir=self.accounts_dir):
            save_profile("tiktok", label, profile, accounts_dir=self.accounts_dir)
        existing = self.store.one(
            "SELECT id FROM accounts WHERE platform='tiktok' AND backend_label=? AND mode='live'",
            (label,),
        )
        account = (
            self.engine.account(existing["id"])
            if existing
            else self.engine.add_account(
                AccountInput(
                    platform="tiktok", label="TikTok · @" + profile["username"], backend_label=label
                )
            )
        )
        checked = self.engine.check_account(account["id"])
        return {
            "account_id": account["id"],
            "username": profile["username"],
            "target": profile["identity"],
            "status": checked["status"],
            "permissions": checked["health"].get("permissions", []),
        }

    def read(self, account_id, kind, value):
        account = self.engine.account(account_id)
        if account["platform"] != "tiktok" or account["mode"] != "live" or account["disabled"]:
            raise Conflict("请选择已接入的 TikTok 真实账号")
        if account["cooldown_until"] > self.clock():
            raise Conflict("TikTok 账号仍在冷却")
        if kind == "comments" and not value.video_id:
            raise Conflict("读取评论需要自有视频 ID")
        payload = value.model_dump()
        if kind == "comments":
            try:
                payload = comment_binding(payload)
            except (BackendError, ValueError) as exc:
                raise Conflict(getattr(exc, "safe_code", "TikTokCommentURLInvalid")) from None
        from .backends.tiktok_desktop import desktop_profile

        try:
            settings = read_profile(
                "tiktok", account["backend_label"], accounts_dir=self.accounts_dir
            )
        except BackendError:
            settings = {}
        if desktop_profile(settings):
            raise Conflict(
                "TikTokDesktopReadCapabilityRequired：此工具只读取自有内容API；请使用已绑定的桌面agent读取能力，不代表需要安装执行器"
            )
        operation = {"videos": "targets", "comments": "content", "insights": "insights"}[kind]
        result = self.engine.call(account, operation, {"account_id": account_id, **payload})
        self.engine.apply_cooldown(account_id, result)
        if result.get("rate_limited") or result.get("healthy") is False:
            raise Conflict(
                "TikTok 读取未完成：" + str(result.get("reason", "TikTokReadUnavailable"))
            )
        if kind == "comments" and value.import_to_pool:
            result["imported"] = Campaigns(self.engine).import_content(
                [OpportunityInput.model_validate(v) for v in result.get("content_items", [])],
                preserve_review=True,
            )
        return result

    def check_interaction(self, account_id, value):
        """Local preparation contract, never execution permission or an API call."""
        account = self.engine.account(account_id)
        if account["platform"] != "tiktok":
            raise Conflict("请选择 TikTok 账号")
        result = {
            "execution_ready": False,
            "input_valid": False,
            "state": "blocked",
            "capability_scope": "owned_content_api",
            "platform_probe_performed": False,
        }
        if value.action not in COMMENT_ACTIONS:
            return {
                **result,
                "state": "not_applicable",
                "reason": "TikTokActionRequiresItsOwnProvider",
            }
        if not re.fullmatch(r"[1-9][0-9]{0,29}", value.video_id):
            return {**result, "reason": "TikTokVideoRequired"}
        try:
            bound = comment_binding(
                {
                    "video_id": value.video_id,
                    "comment_id": value.parent_comment_id,
                    "comment_url": value.comment_url,
                }
            )
        except (BackendError, ValueError) as exc:
            return {**result, "reason": getattr(exc, "safe_code", "TikTokCommentURLInvalid")}
        parent = bound.get("comment_id", "")
        if value.action != "comment" and not re.fullmatch(r"[1-9][0-9]{0,29}", parent):
            return {**result, "reason": "TikTokParentCommentRequired"}
        if value.action == "comment" and parent:
            return {**result, "reason": "TikTokTopLevelCommentCannotReply"}
        result.update(input_valid=True, video_id=value.video_id, parent_comment_id=parent)
        try:
            profile = read_profile(
                "tiktok", account["backend_label"], accounts_dir=self.accounts_dir
            )
        except BackendError:
            profile = {}
        from .backends.tiktok_desktop import tabbit_candidate_enabled

        registered = profile.get("connection_type") == "tiktok_desktop"
        candidate = tabbit_candidate_enabled(profile)
        if (registered or candidate) and value.action in {"comment", "reply"}:
            instance = profile.get("instance_id") or profile.get("tabbit_instance", "")
            if candidate and not re.fullmatch(r"[0-9A-F]{16}", instance):
                return {**result, "reason": "TikTokDesktopBindingUnavailable"}
            from .tiktok_targets import resolve_video_target

            try:
                if value.comment_url:
                    from .backends.tiktok_desktop import video_target

                    resolved = {"target": video_target(value.comment_url, value.video_id)}
                else:
                    resolved = resolve_video_target(
                        self.engine,
                        account,
                        value.video_id,
                        task_id=value.task_id,
                        opportunity_id=value.opportunity_id,
                        action=value.action,
                        parent_comment_id=parent,
                    )
            except (Conflict, BackendError, ValueError) as exc:
                return {**result, "reason": getattr(exc, "safe_code", str(exc))}
            return {
                **result,
                "resolved_target": resolved["target"],
                "evidence_sources": resolved.get("evidence_sources", []),
                "capability_scope": "registered_desktop_video"
                if registered
                else "bound_tabbit_video",
                "state": "needs_desktop_preflight",
                "reason": "TikTokDesktopRequiresTargetAndIdentityCheck",
            }
        if not value.target_owned:
            return {
                **result,
                "reason": "TikTokDesktopVideoCapabilityUnverified",
                "required_evidence": [
                    "native_target_binding",
                    "independent_result_readback",
                    "existing_desktop_provider",
                ],
            }
        return {
            **result,
            "state": "needs_platform_preflight",
            "reason": "TikTokOwnedAPIRequiresLivePermissionCheck",
        }

    def reconcile_due(self):
        """Poll only existing receipts, including while outbound work is paused."""
        now = self.clock()
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT value FROM meta WHERE key='tiktok_poll_at'").fetchone()
            if row and float(row[0]) > now - 180:
                return {"processed": []}
            db.execute(
                "INSERT INTO meta VALUES('tiktok_poll_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(now),),
            )
        rows = self.store.rows(
            "SELECT i.id FROM items i JOIN accounts a ON a.id=i.account_id WHERE a.platform='tiktok' AND a.mode='live' AND i.status='unknown' AND a.disabled=0 AND a.cooldown_until<=? ORDER BY CAST(COALESCE((SELECT value FROM meta WHERE key='tiktok_polled:'||i.id),'0') AS REAL),i.due_at LIMIT 20",
            (now,),
        )
        results = []
        for row in rows:
            with self.store.connect(write=True) as db:
                db.execute(
                    "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    ("tiktok_polled:" + row["id"], str(now)),
                )
            try:
                results.append({"item_id": row["id"], **self.engine.reconcile(row["id"])})
            except (Conflict, BackendError):
                continue
        return {"processed": results}


def verify_signature(raw, signature, secret, *, now=None):
    now = time.time() if now is None else now
    try:
        pairs = [p.strip().split("=", 1) for p in signature.split(",")]
        timestamps = [v for k, v in pairs if k == "t"]
        signatures = [v for k, v in pairs if k == "s"]
        if len(timestamps) != 1 or abs(now - int(timestamps[0])) > 300:
            return False
        expected = hmac.new(
            secret.encode(), timestamps[0].encode() + b"." + raw, hashlib.sha256
        ).hexdigest()
        return any(hmac.compare_digest(expected, value) for value in signatures)
    except (ValueError, TypeError):
        return False
