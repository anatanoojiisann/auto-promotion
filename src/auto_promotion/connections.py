"""Local sign-in jobs. Public status and audit entries never contain credentials."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator

from .backends.common import BackendError, account_lock, profile_path, save_profile
from .core import Conflict, NotFound
from .models import AccountInput, Model, canonical
from .store import Store

ACTIVE = {"connecting", "awaiting_login", "checking"}


class ConnectionInput(Model):
    platform: Literal["instagram", "x", "discord"]
    username: str = Field(default="", max_length=80)
    backend_label: str | None = Field(default=None, min_length=1, max_length=100)
    access_token: SecretStr | None = Field(default=None, max_length=8192)
    bot_token: SecretStr | None = Field(default=None, max_length=8192)
    api_version: str = ""
    browser_channel: Literal["chromium", "msedge", "chrome"] = "chromium"

    @model_validator(mode="after")
    def validate_login(self):
        if self.platform == "discord":
            if (
                self.access_token
                or self.api_version
                or self.browser_channel != "chromium"
                or self.username
            ):
                raise ValueError("Discord 连接只需 Bot Token 和本机标签")
            if not self.bot_token or not self.bot_token.get_secret_value().strip():
                raise ValueError("请在本机输入 Discord Bot Token")
            self.backend_label = AccountInput(
                platform="discord",
                label="Discord Bot",
                backend_label=self.backend_label or "discord-bot",
            ).backend_label
            return self
        if self.bot_token:
            raise ValueError("Bot Token 仅用于 Discord")
        self.username = self.username.strip().removeprefix("@").casefold()
        pattern = r"[a-z0-9_]{1,15}" if self.platform == "x" else r"[a-z0-9_.]{1,30}"
        if not re.fullmatch(pattern, self.username):
            raise ValueError("请填写正确的平台用户名")
        self.backend_label = AccountInput(
            platform=self.platform,
            label=self.username,
            backend_label=self.backend_label or f"{self.platform}-{self.username}",
        ).backend_label
        if self.platform == "instagram":
            if self.browser_channel != "chromium":
                raise ValueError("浏览器选择仅用于 X 登录")
            if not self.access_token or not self.access_token.get_secret_value().strip():
                raise ValueError("请在本机输入 Instagram 用户访问令牌")
            if not re.fullmatch(r"v\d+\.\d+", self.api_version):
                raise ValueError("请填写 Meta 应用使用的 API 版本")
        elif self.access_token or self.api_version:
            raise ValueError("X 连接只需要用户名，登录在独立浏览器中完成")
        return self


class ConnectionManager:
    def __init__(self, engine, *, login_timeout=900):
        self.engine = engine
        self.accounts_dir = Path(
            os.getenv("AP_ACCOUNTS_DIR", Path(engine.store.path).parent / "accounts")
        ).resolve()
        self.login_timeout = login_timeout
        self.lock = threading.Lock()
        self.jobs = {}
        self.cancellations = {}
        self.closed = False
        with self.engine.store.connect(write=True) as db:
            for row in db.execute("SELECT * FROM connection_jobs ORDER BY created_at"):
                job = {
                    "id": row["id"],
                    "platform": row["platform"],
                    "username": row["username"],
                    "backend_label": row["backend_label"],
                    "stage": row["stage"],
                    "created_at": row["created_at"],
                    **json.loads(row["details"]),
                }
                if job["stage"] in ACTIVE:
                    job.update(stage="interrupted", reason="AppRestarted")
                    db.execute(
                        "UPDATE connection_jobs SET stage=?,details=? WHERE id=?",
                        (job["stage"], canonical(self._job_details(job)), job["id"]),
                    )
                self.jobs[job["id"]] = job

    @staticmethod
    def _job_details(job):
        return {
            key: value
            for key, value in job.items()
            if key
            in {"browser_channel", "account_id", "reason", "instagram_user_id", "discord_user_id"}
        }

    def _save_job(self, job):
        with self.engine.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO connection_jobs(id,platform,username,backend_label,stage,created_at,details) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,"
                "stage=excluded.stage,details=excluded.details",
                (
                    job["id"],
                    job["platform"],
                    job["username"],
                    job["backend_label"],
                    job["stage"],
                    job["created_at"],
                    canonical(self._job_details(job)),
                ),
            )

    def list(self):
        with self.lock:
            return [dict(job) for job in self.jobs.values()]

    def get(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise NotFound("连接记录不存在；如工作台已重启，请重新连接")
            return dict(self.jobs[job_id])

    def update(self, job_id, stage, **values):
        with self.lock:
            self.jobs[job_id].update(stage=stage, **values)
            self._save_job(self.jobs[job_id])

    def start(self, value):
        if os.getenv("AP_" + value.platform.upper() + "_BACKEND", "builtin") != "builtin":
            raise Conflict("网页连接使用内置后端，请先切换到 builtin")
        with self.lock:
            if self.closed:
                raise Conflict("工作台正在关闭")
            active = [j for j in self.jobs.values() if j["stage"] in ACTIVE]
            if any(
                j["platform"] == value.platform and j["backend_label"] == value.backend_label
                for j in active
            ):
                raise Conflict("该账号正在连接，请完成现有登录或取消后重试")
            if len(active) >= 2:
                raise Conflict("已有两个账号正在连接，请先完成其中一个")
            # Bound public in-memory history; no token is put into a job or audit record.
            for job_id in list(self.jobs):
                if len(self.jobs) < 20:
                    break
                if self.jobs[job_id]["stage"] not in ACTIVE:
                    self.jobs.pop(job_id)
                    self.cancellations.pop(job_id)
                    with self.engine.store.connect(write=True) as db:
                        db.execute("DELETE FROM connection_jobs WHERE id=?", (job_id,))
            job_id = "conn-" + uuid.uuid4().hex[:12]
            self.jobs[job_id] = {
                "id": job_id,
                "platform": value.platform,
                "username": value.username,
                "backend_label": value.backend_label,
                "stage": "connecting",
                "created_at": time.time(),
                **({"browser_channel": value.browser_channel} if value.platform == "x" else {}),
            }
            cancel = self.cancellations[job_id] = threading.Event()
            self._save_job(self.jobs[job_id])
        threading.Thread(target=self.run, args=(job_id, value, cancel), daemon=True).start()
        return self.get(job_id)

    def cancel(self, job_id):
        job = self.get(job_id)
        if job["stage"] in ACTIVE:
            self.cancellations[job_id].set()
        return job

    def close(self):
        with self.lock:
            self.closed = True
            for event in self.cancellations.values():
                event.set()

    @staticmethod
    def check_cancel(cancel):
        if cancel.is_set():
            raise BackendError("ConnectionCancelled")

    def run(self, job_id, value, cancel):
        try:
            with account_lock(value.platform, value.backend_label, accounts_dir=self.accounts_dir):
                self.check_cancel(cancel)
                if value.platform == "instagram":
                    identity = self.instagram(value, cancel)
                elif value.platform == "discord":
                    identity = self.discord(value, cancel)
                else:
                    identity = self.x_login(job_id, value, cancel)
            self.check_cancel(cancel)
            self.update(job_id, "checking", **identity)
            account = self.register(value)
            self.update(
                job_id,
                "connected" if account["status"] == "healthy" else "needs_check",
                account_id=account["id"],
                reason=account["health"].get("reason", ""),
            )
        except Exception as exc:
            code = exc.safe_code if isinstance(exc, BackendError) else type(exc).__name__
            if not isinstance(code, str) or not code.isidentifier() or len(code) > 80:
                code = "ConnectionFailed"
            self.update(
                job_id, "cancelled" if code == "ConnectionCancelled" else "error", reason=code
            )
        finally:
            value.access_token = None
            value.bot_token = None
            with self.lock:
                stage = self.jobs.get(job_id, {}).get("stage", "error")
            with self.engine.store.connect(write=True) as db:
                Store.audit(
                    db,
                    "account.connection",
                    {"id": job_id, "platform": value.platform, "stage": stage},
                )

    def instagram(self, value, cancel):
        from .backends.instagram import Graph

        settings = {
            "access_token": value.access_token.get_secret_value().strip(),
            "api_version": value.api_version,
        }
        try:
            observed = Graph(settings).request("me", fields={"fields": "user_id,username"})
            user_id = str(observed.get("user_id") or observed.get("id") or "")
            username = str(observed.get("username", ""))
            if not user_id.isdigit() or not username:
                raise BackendError("InstagramIdentityUnavailable")
            if username.casefold() != value.username:
                raise BackendError("InstagramIdentityMismatch")
            self.check_cancel(cancel)
            settings.update(identity=user_id, username=username)
            save_profile("instagram", value.backend_label, settings, accounts_dir=self.accounts_dir)
            return {"instagram_user_id": user_id}
        finally:
            settings.clear()

    def discord(self, value, cancel):
        from .backends.discord import configure

        self.check_cancel(cancel)
        token = value.bot_token.get_secret_value().strip()
        result = configure(
            value.backend_label,
            token=token,
            accounts_dir=self.accounts_dir,
            before_save=lambda: self.check_cancel(cancel),
        )
        value.username = result["username"]
        return {"discord_user_id": result["discord_user_id"], "username": value.username}

    def x_login(self, job_id, value, cancel):
        from playwright.sync_api import sync_playwright

        from .backends.browser import (
            HOME,
            check_response,
            identity,
            launch_context,
            save_browser_login,
        )

        root = profile_path("x", value.backend_label, accounts_dir=self.accounts_dir)
        with sync_playwright() as runtime:
            context = launch_context(
                runtime, root, browser_channel=value.browser_channel, headless=False
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(15000)
                check_response(
                    page.goto(HOME["x"] + "/i/flow/login", wait_until="domcontentloaded")
                )
                self.update(job_id, "awaiting_login")
                deadline = time.monotonic() + self.login_timeout
                while time.monotonic() < deadline:
                    self.check_cancel(cancel)
                    if page.is_closed():
                        raise BackendError("LoginBrowserClosed")
                    switcher = page.get_by_test_id("SideNav_AccountSwitcher_Button")
                    if switcher.count() and switcher.first.is_visible():
                        observed = identity("x", context, page)
                        if observed != value.username:
                            raise BackendError("BrowserIdentityMismatch")
                        self.check_cancel(cancel)
                        save_browser_login(
                            "x",
                            value.backend_label,
                            observed,
                            value.browser_channel,
                            accounts_dir=self.accounts_dir,
                        )
                        return {}
                    page.wait_for_timeout(500)
                raise BackendError("LoginWaitingTimedOut")
            finally:
                context.close()

    def register(self, value):
        existing = self.engine.store.one(
            "SELECT id FROM accounts WHERE platform=? AND backend_label=? AND mode='live'",
            (value.platform, value.backend_label),
        )
        if existing:
            account_id = existing["id"]
        else:
            account_id = self.engine.add_account(
                AccountInput(
                    platform=value.platform,
                    label={"instagram": "Instagram", "x": "X", "discord": "Discord Bot"}[
                        value.platform
                    ]
                    + " · @"
                    + value.username,
                    backend_label=value.backend_label,
                )
            )["id"]
        return self.engine.check_account(account_id)
