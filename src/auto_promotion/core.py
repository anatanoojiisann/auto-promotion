from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from . import quotas
from .backends.common import BackendError, automatic_actions, read_profile
from .dispatcher import AccountBusy, AccountLanes
from .execution_diagnostics import FIELDS, safe_code
from .models import (
    CAPABILITIES,
    AccountInput,
    AccountUpdate,
    CandidateInput,
    ExecutionSettings,
    ItemInput,
    TaskInput,
    canonical,
    digest,
    epoch,
)
from .readiness import DELIVERY_GRACE_SECONDS, REASONS, task_workflow, telegram_blocker
from .routing import route_for
from .statistics import ZONE, Statistics, publication_time, record_publication
from .store import Store, decode
from .workers import Workers


class Conflict(Exception):
    """An explicit, safe-to-display workflow failure."""


class NotFound(Conflict):
    pass


TERMINAL = {
    "confirmed",
    "scheduled",
    "failed_known",
    "unknown",
    "manual_attention",
    "missed",
    "cancelled",
}
RISK = re.compile(
    r"\b(?:nsfw|porn|adult|escort|dating|casino|gambling|scam)\b|18\+|色情|赌博|博彩|约炮|诈骗",
    re.I,
)
NON_ADULT = re.compile(r"(?<![\w-])non-adult(?![\w-])", re.I)


class Engine:
    def __init__(
        self, store: Store, adapters, *, clock=time.time, live_enabled=False, media_root=None
    ):
        self.store = store
        self.adapters = adapters
        self.adapters.database = store.path
        self.clock = clock
        self.live_enabled = live_enabled
        self.media_root = Path(media_root or Path(store.path).parent / "media").resolve()
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.lanes = AccountLanes(store)

    def execution_settings(self):
        with self.store.connect() as db:
            return quotas.settings(db)

    def configure_execution(self, value: ExecutionSettings):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE meta SET value=? WHERE key='execution_settings'",
                (canonical(value.model_dump()),),
            )
            Store.audit(db, "execution.settings", value.model_dump(), now=self.clock())
        from .cloud_control import queue_settings

        queue_settings(
            self,
            {
                k: v
                for k, v in value.model_dump().items()
                if k
                in {
                    "global_limit",
                    "max_workers",
                    "platform_workers",
                    "platform_limits",
                    "pool_workers",
                }
            },
        )
        return self.execution_settings()

    def account(self, account_id):
        row = self.store.one("SELECT * FROM accounts WHERE id=?", (account_id,))
        if not row:
            raise NotFound("账号不存在")
        value = decode(row, "health")
        value["capabilities"] = sorted(CAPABILITIES[value["platform"]])
        value["automatic_actions"] = automatic_actions(value)
        if value["platform"] == "tiktok":
            try:
                value["owned_target_id"] = (
                    read_profile(
                        "tiktok",
                        value["backend_label"],
                        accounts_dir=os.getenv(
                            "AP_ACCOUNTS_DIR", Path(self.store.path).parent / "accounts"
                        ),
                    )["identity"]
                    if value["mode"] == "live"
                    else "simulation-tiktok"
                )
            except BackendError:
                value["owned_target_id"] = ""
        value["writable"] = bool(
            value["status"] == "healthy"
            and not value["disabled"]
            and value["cooldown_until"] <= self.clock()
            and (value["mode"] == "simulation" or self.live_enabled)
            and self.adapters.commissioned(value)
            and (
                value["platform"] != "tiktok"
                or value["mode"] == "simulation"
                or value["health"].get("permissions")
            )
        )
        return value

    def accounts(self):
        return [
            self.account(r["id"])
            for r in self.store.rows("SELECT id FROM accounts ORDER BY platform,label")
        ]

    def add_account(self, value: AccountInput):
        if value.mode == "live" and value.min_interval < 120:
            raise Conflict("真实账号最小发送间隔为 120 秒")
        account_id = "acc-" + uuid.uuid4().hex[:12]
        with self.store.connect(write=True) as db:
            existing = db.execute(
                "SELECT id FROM accounts WHERE platform=? AND backend_label=? AND mode=?",
                (value.platform, value.backend_label, value.mode),
            ).fetchone()
            if existing:
                raise Conflict("该平台账号已存在")
            db.execute(
                "INSERT INTO accounts(id,platform,label,backend_label,mode,daily_limit,hourly_limit,min_interval) VALUES(?,?,?,?,?,?,?,?)",
                (
                    account_id,
                    value.platform,
                    value.label,
                    value.backend_label,
                    value.mode,
                    value.daily_limit,
                    value.hourly_limit,
                    value.min_interval,
                ),
            )
            Store.audit(
                db, "account.created", {"account_id": account_id, "platform": value.platform}
            )
        return self.account(account_id)

    def update_account(self, account_id, value: AccountUpdate):
        account = self.account(account_id)
        if account["mode"] == "live" and value.min_interval < 120:
            raise Conflict("真实账号最小发送间隔为 120 秒")
        changes = value.model_dump()
        if all(account[key] == val for key, val in changes.items()):
            return account
        daily_only = all(
            account[key] == val for key, val in changes.items() if key != "daily_limit"
        )
        with self.store.connect(write=True) as db:
            for task in db.execute(
                "SELECT id,account_ids,state,policy FROM tasks WHERE state NOT IN ('cancelled','completed')"
            ):
                if account_id in json.loads(task["account_ids"]):
                    # Old snapshots stay immutable; resuming them still requires
                    # a new task after this explicit account-budget change.
                    if daily_only and (
                        task["state"] == "paused"
                        or epoch(json.loads(task["policy"])["ends_at"]) <= self.clock()
                    ):
                        continue
                    raise Conflict("账号已被未结束任务使用；请先结束该任务并创建新任务")
            db.execute(
                "UPDATE accounts SET label=?,daily_limit=?,hourly_limit=?,min_interval=? WHERE id=?",
                (
                    value.label,
                    value.daily_limit,
                    value.hourly_limit,
                    value.min_interval,
                    account_id,
                ),
            )
            Store.audit(
                db,
                "account.updated",
                {
                    "account_id": account_id,
                    "before": {k: account[k] for k in changes},
                    "after": changes,
                },
            )
        return self.account(account_id)

    def call(self, account, operation, payload=None, task_id=None):
        route = route_for(account, (payload or {}).get("payload", {}))
        with self.lanes.acquire(account["id"], resource=route.resource):
            return self._call_unlocked(account, operation, payload, task_id)

    def _call_unlocked(self, account, operation, payload=None, task_id=None):
        started = time.monotonic()
        try:
            if operation in {"execute", "join"}:
                from .cloud_control import CloudControl

                context = getattr(self.adapters.context, "job", None)
                from .execution_channels import independent_active

                local_independent = bool(
                    context and context.get("channel") == "local" and independent_active(self)
                )
                cloud = None if local_independent else CloudControl.configured()
                required = self.store.one(
                    "SELECT value FROM meta WHERE key='cloud_control_required'"
                )
                if not cloud and required and required["value"] == "1" and not local_independent:
                    from .cloud_control import CloudUnavailable

                    raise CloudUnavailable("CloudControlConfigurationMissing")
                if context and context.get("channel") == "local":
                    from .execution_channels import guard_local

                    guard_local(self, context, cloud)
                elif cloud:
                    attempt_id = (payload or {}).get("attempt_id")
                    if operation == "join":
                        attempt = self.store.one(
                            "SELECT id FROM join_attempts WHERE candidate_id=?",
                            ((payload or {}).get("candidate_id"),),
                        )
                        attempt_id = attempt["id"] if attempt else None
                    cloud.guard(getattr(self.adapters.context, "job", None), attempt_id)
            result = self.adapters.call(account, operation, payload or {})
        except Exception as exc:
            # Never expose provider exceptions: they may include tokens, URLs or browser state.
            self.store.log_call(
                account["platform"],
                operation,
                "error",
                started,
                task_id,
                getattr(exc, "safe_code", type(exc).__name__),
            )
            error = Conflict("平台调用未完成；请查看 API 中心的错误类型并核验连接")
            error.safe_code = safe_code(
                getattr(exc, "safe_code", type(exc).__name__), "BackendError"
            )
            raise error from None
        outcome = result.get("status")
        status = outcome if outcome in {"unknown", "failed_known", "manual_attention"} else "ok"
        if (
            result.get("healthy") is False
            or result.get("allowed") is False
            or (operation == "feedback" and result.get("verified") is not True)
        ):
            status = "error"
        self.store.log_call(
            account["platform"],
            operation,
            status,
            started,
            task_id,
            safe_code(result.get("reason"), None) if status != "ok" else None,
            safe_code(result.get("execution_step"), None),
        )
        return result

    def check_account(self, account_id):
        account = self.account(account_id)
        if (
            account["platform"] in {"discord", "tiktok"}
            and account["cooldown_until"] > self.clock()
        ):
            return account
        try:
            result = self.call(account, "health")
        except AccountBusy:
            return self.account(account_id)
        except Conflict:
            result = {"healthy": False, "reason": "connection_unavailable"}
        # Health results are an allowlist; raw provider responses are not persisted.
        health = {
            key: result[key]
            for key in ("healthy", "service_ready", "identity_verified", "permissions", "reason")
            if key in result
        }
        observed_identity = result.get("account_identity")
        with self.store.connect(write=True) as db:
            current = db.execute(
                "SELECT identity_hash FROM accounts WHERE id=?", (account_id,)
            ).fetchone()
            identity_hash = current["identity_hash"]
            if health.get("identity_verified") and observed_identity:
                observed_hash = digest([account["platform"], str(observed_identity)])
                if identity_hash and identity_hash != observed_hash:
                    health.update(
                        healthy=False, identity_verified=False, reason="AccountIdentityChanged"
                    )
                elif not identity_hash:
                    other = db.execute(
                        "SELECT id FROM accounts WHERE platform=? AND mode=? AND identity_hash=? AND id<>?",
                        (account["platform"], account["mode"], observed_hash, account_id),
                    ).fetchone()
                    if other:
                        health.update(
                            healthy=False,
                            identity_verified=False,
                            reason="DuplicatePlatformIdentity",
                        )
                    else:
                        identity_hash = observed_hash
            elif identity_hash and health.get("identity_verified"):
                health.update(
                    healthy=False, identity_verified=False, reason="AccountIdentityUnavailable"
                )
            status = (
                "healthy"
                if health.get("healthy") and health.get("identity_verified")
                else "unavailable"
            )
            db.execute(
                "UPDATE accounts SET status=?,health=?,checked_at=?,identity_hash=? WHERE id=?",
                (status, canonical(health), self.clock(), identity_hash, account_id),
            )
            if result.get("rate_limited") or result.get("risk_warning"):
                db.execute(
                    "UPDATE accounts SET cooldown_until=max(cooldown_until,?),disabled=max(disabled,?) WHERE id=?",
                    (
                        self.clock() + max(1, int(result.get("retry_after_seconds") or 3600)),
                        int(bool(result.get("risk_warning"))),
                        account_id,
                    ),
                )
            Store.audit(db, "account.checked", {"account_id": account_id, "status": status})
        return self.account(account_id)

    def disable_account(self, account_id, disabled):
        self.account(account_id)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET disabled=? WHERE id=?", (int(disabled), account_id))
            Store.audit(db, "account.disabled", {"account_id": account_id, "disabled": disabled})
        return self.account(account_id)

    def account_targets(self, account_id):
        account = self.account(account_id)
        if account["platform"] != "discord":
            raise Conflict("此平台暂不提供频道目录")
        if account["cooldown_until"] > self.clock():
            raise Conflict("账号仍在冷却，请稍后读取频道")
        result = self.call(account, "targets")
        self.apply_cooldown(account_id, result)
        if result.get("rate_limited"):
            raise Conflict("Discord 请求受到限流，已记录冷却时间")
        return {"targets": result.get("targets", []), "truncated": bool(result.get("truncated"))}

    def apply_cooldown(self, account_id, result):
        if result.get("rate_limited") or result.get("risk_warning"):
            until = self.clock() + max(1, int(result.get("retry_after_seconds") or 60))
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE accounts SET cooldown_until=max(cooldown_until,?) WHERE id=?",
                    (until, account_id),
                )
                if result.get("risk_warning"):
                    db.execute("UPDATE accounts SET disabled=1 WHERE id=?", (account_id,))
                Store.audit(db, "account.cooldown", {"account_id": account_id, "until": until})

    def create_task(self, value: TaskInput):
        if len(set(value.account_ids)) != len(value.account_ids):
            raise Conflict("任务账号不能重复")
        accounts = [self.account(a) for a in value.account_ids]
        if any(a["health"].get("reason") == "DuplicatePlatformIdentity" for a in accounts):
            raise Conflict("任务包含重复绑定的平台身份，请使用已核验的原账号")
        if len({a["mode"] for a in accounts}) != 1:
            raise Conflict("模拟账号和真实账号必须分开创建任务")
        if accounts[0]["mode"] == "live" and value.policy.min_interval < 120:
            raise Conflict("真实任务最小发送间隔为 120 秒")
        if value.policy.max_joins and not any(a["platform"] == "telegram" for a in accounts):
            raise Conflict("只有 Telegram 支持入群")
        snapshot = [
            {
                k: a[k]
                for k in (
                    "id",
                    "platform",
                    "label",
                    "backend_label",
                    "mode",
                    "daily_limit",
                    "hourly_limit",
                    "min_interval",
                )
            }
            for a in accounts
        ]
        policy = value.policy.model_dump()
        # Only the server can capture limits; callers cannot pre-authorize a future increase.
        policy["quota_snapshot"] = {"execution": self.execution_settings()}
        envelope = {"accounts": snapshot, "policy": policy}
        task_id = "AP-" + uuid.uuid4().hex[:14].upper()
        now = self.clock()
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO tasks(id,name,mode,account_ids,account_snapshot,policy,policy_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    value.name,
                    accounts[0]["mode"],
                    canonical(value.account_ids),
                    canonical(snapshot),
                    canonical(policy),
                    digest(envelope),
                    now,
                    now,
                ),
            )
            Store.audit(db, "task.created", {"policy_hash": digest(envelope)}, task_id, now)
        return self.task(task_id)

    def task(self, task_id):
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not row:
            raise NotFound("任务不存在")
        result = decode(row, "policy", "account_ids", "account_snapshot", "manifest")
        result["approvals"] = self.store.rows(
            "SELECT stage,hash,phrase,actor,approved_at FROM approvals WHERE task_id=? ORDER BY approved_at",
            (task_id,),
        )
        result["items"] = [
            decode(r, "payload", "result")
            for r in self.store.rows(
                "SELECT * FROM items WHERE task_id=? ORDER BY position", (task_id,)
            )
        ]
        result["candidates"] = [
            decode(r, "payload", "reasons", "result")
            for r in self.store.rows(
                "SELECT * FROM candidates WHERE task_id=? ORDER BY id", (task_id,)
            )
        ]
        result["summary"] = {
            s: sum(i["status"] == s for i in result["items"])
            for s in {i["status"] for i in result["items"]}
        }
        result["execution"] = Workers(self.store, self.clock).snapshot(
            task_id, live=result["mode"] == "live"
        )
        from .queue_store import JobQueue

        result["execution"]["pools"] = JobQueue(self).pools(task_id)
        result["readiness"] = self.readiness(result)
        reply = self.store.one(
            "SELECT r.event_id,r.platform,r.account_id,i.payload FROM reply_inbox r "
            "JOIN items i ON i.id=r.item_id WHERE r.followup_task_id=? LIMIT 1",
            (task_id,),
        )
        if reply:
            message_id = reply["event_id"].rsplit(":", 1)[-1]
            result["reply_context"] = {
                "target": json.loads(reply["payload"])["target"],
                "account_id": reply["account_id"],
                "reply_to": int(message_id)
                if reply["platform"] == "telegram" and message_id.isdigit()
                else None,
                "discord_reply_to": message_id if reply["platform"] == "discord" else "",
            }
        return result

    def readiness(self, task):
        """Report every local prerequisite without checking a real account or sending."""
        blockers = []
        if task["state"] != "active":
            blockers.append("task_not_active")
        if task["phase"] != "ready":
            blockers.append("task_not_started")
        if self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0":
            blockers.append("global_stop")
        if task["mode"] == "live" and not self.live_enabled:
            blockers.append("live_writes_disabled")
        if self.clock() >= epoch(task["policy"]["ends_at"]):
            blockers.append("delivery_window_elapsed")
        items = []
        with self.store.connect() as db:
            raw_task = db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
            for item in task["items"]:
                if item["status"] != "not_attempted":
                    continue
                reason = telegram_blocker(db, raw_task, item["payload"], item["due_at"])
                if reason:
                    items.append(
                        {"item_id": item["id"], "reason": reason, "message": REASONS[reason]}
                    )
        return {
            "blockers": [{"reason": r, "message": REASONS[r]} for r in blockers],
            "items": items,
            "workflow": task_workflow(task, blockers, items, self.clock()),
            "sender_status": task["execution"]["status"],
            "live_writes_enabled": self.live_enabled,
        }

    def _integrity(self, db, task, stage):
        policy = json.loads(task["policy"])
        snapshot = json.loads(task["account_snapshot"])
        if digest({"accounts": snapshot, "policy": policy}) != task["policy_hash"]:
            raise Conflict("发现政策已发生变化，请创建新任务")
        for saved in snapshot:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (saved["id"],)).fetchone()
            if row is None or any(row[k] != v for k, v in saved.items()):
                raise Conflict("账号配置已变化，请创建新任务")
        if stage == "outbound":
            expected = task["manifest_hash"]
            if not task["manifest"] or digest(json.loads(task["manifest"])) != expected:
                raise Conflict("外发清单哈希不匹配")
            rows = db.execute(
                "SELECT payload,payload_hash FROM items WHERE task_id=? ORDER BY position",
                (task["id"],),
            ).fetchall()
            payloads = [json.loads(r["payload"]) for r in rows]
            manifest = json.loads(task["manifest"])
            if payloads != manifest["items"] or any(
                digest(p) != r["payload_hash"] for p, r in zip(payloads, rows)
            ):
                raise Conflict("执行项与已启动清单不一致")
        return policy

    def start_task(self, task_id, *, source="client", actor="operator", sending=True):
        """Persist an explicit start; no approval phrases or approval rows are required."""
        task = self.task(task_id)
        for item in task["items"]:
            payload = {
                k: v
                for k, v in item["payload"].items()
                if k not in {"position", "platform", "account_label"}
            }
            self.validate_item(task, ItemInput(**payload))
        with self.store.connect(write=True) as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if source == "scheduler" and (task["state"] != "active" or task["phase"] != "starting"):
                raise Conflict("任务已暂停或启动状态已变化")
            if task["state"] in {"cancelled", "completed"}:
                raise Conflict("任务已结束")
            self._integrity(db, task, "discovery_join")
            if task["campaign_id"]:
                campaign = db.execute(
                    "SELECT state FROM campaigns WHERE id=?", (task["campaign_id"],)
                ).fetchone()
                if not campaign or campaign[0] in {"paused", "cancelled", "completed"}:
                    raise Conflict("活动已暂停或结束，请先恢复活动")
            if task["state"] == "active" and (
                task["phase"] == "ready" or (task["phase"] == "starting" and not sending)
            ):
                if task["phase"] == "ready":
                    self._integrity(db, task, "outbound")
            else:
                if self.clock() >= epoch(json.loads(task["policy"])["ends_at"]):
                    raise Conflict("任务执行窗口已过期，请创建新任务")
                rows = db.execute(
                    "SELECT * FROM items WHERE task_id=? ORDER BY position", (task_id,)
                ).fetchall()
                phase = "discovered"
                if rows and sending:
                    if not self._execution_service_ready(db, task):
                        phase = "starting"
                    else:
                        self._resolve_membership_schedule(db, task)
                        rows = db.execute(
                            "SELECT * FROM items WHERE task_id=? ORDER BY position", (task_id,)
                        ).fetchall()
                        self._validate_outbound_readiness(db, task, check_membership=False)
                        waiting = any(
                            telegram_blocker(db, task, json.loads(r["payload"]), r["due_at"])
                            for r in rows
                        )
                        if waiting:
                            phase = "starting"
                        else:
                            self._freeze_manifest(db, task)
                            phase = "ready"
                db.execute(
                    "UPDATE tasks SET state='active',phase=?,updated_at=? WHERE id=?",
                    (phase, self.clock(), task_id),
                )
                if task["state"] != "active" or task["phase"] != phase:
                    current = db.execute(
                        "SELECT manifest_hash FROM tasks WHERE id=?", (task_id,)
                    ).fetchone()
                    Store.audit(
                        db,
                        "task.started",
                        {
                            "source": source,
                            "actor": actor,
                            "phase": phase,
                            "policy_hash": task["policy_hash"],
                            "manifest_hash": current[0],
                        },
                        task_id,
                        self.clock(),
                    )
        return self.task(task_id)

    def _execution_service_ready(self, db, task):
        policy = json.loads(task["policy"])
        if policy.get("outbound_after_service_recovery_seconds") is None:
            return True
        required = db.execute(
            "SELECT value FROM meta WHERE key='cloud_control_required'"
        ).fetchone()
        if not required or required[0] != "1":
            return True
        health = db.execute("SELECT value FROM meta WHERE key='cloud_control_health'").fetchone()
        return bool(health and json.loads(health[0]).get("state") == "connected")

    def _resolve_membership_schedule(self, db, task):
        """Resolve an explicitly requested relative time before freezing the manifest."""
        policy = json.loads(task["policy"])
        delay = policy.get("outbound_after_membership_seconds")
        if delay is None or task["manifest_hash"]:
            return
        previous_due = None
        last_by_account = {}
        for item in db.execute(
            "SELECT * FROM items WHERE task_id=? ORDER BY position", (task["id"],)
        ).fetchall():
            payload = json.loads(item["payload"])
            if payload["platform"] != "telegram" or item["attempt_id"]:
                continue
            candidate = db.execute(
                "SELECT * FROM candidates WHERE task_id=? AND account_id=? AND lower(target)=lower(?) "
                "ORDER BY CASE WHEN decision='qualified' THEN 1 ELSE 0 END,rowid DESC LIMIT 1",
                (task["id"], payload["account_id"], payload["target"]),
            ).fetchone()
            if not candidate and not policy["max_joins"]:
                candidate = db.execute(
                    "SELECT * FROM candidates WHERE account_id=? AND lower(target)=lower(?) "
                    "ORDER BY rowid DESC LIMIT 1",
                    (payload["account_id"], payload["target"]),
                ).fetchone()
            if not candidate or candidate["state"] not in {"joined", "already_joined"}:
                continue
            receipt = json.loads(candidate["result"])
            if (
                receipt.get("verified") is not True
                or not receipt.get("external_reference")
                or receipt.get("can_send") is not True
            ):
                continue
            try:
                # Backend receipts already include the mandatory first 3600 seconds.
                eligible = epoch(receipt["outbound_eligible_at_utc"]) + delay - 3600
            except (KeyError, TypeError, ValueError):
                continue
            due = max(item["due_at"], eligible, epoch(policy["starts_at"]), self.clock() + 30)
            account = db.execute(
                "SELECT min_interval,cooldown_until,next_at FROM accounts WHERE id=?",
                (item["account_id"],),
            ).fetchone()
            recovery_delay = policy.get("outbound_after_service_recovery_seconds")
            if recovery_delay is not None:
                base = max(
                    self.clock(),
                    epoch(policy["starts_at"]),
                    account["cooldown_until"],
                    account["next_at"],
                )
                due = max(due, base + recovery_delay)
            gap = max(policy["min_interval"], account["min_interval"])
            if previous_due is not None:
                due = max(
                    due, previous_due + (gap if policy.get("pacing_scope", "task") == "task" else 0)
                )
            if item["account_id"] in last_by_account:
                due = max(due, last_by_account[item["account_id"]] + gap)
            previous_due, last_by_account[item["account_id"]] = due, due
            if due == item["due_at"]:
                continue
            payload["scheduled_at"] = datetime.fromtimestamp(due, timezone.utc).isoformat()
            db.execute(
                "UPDATE items SET payload=?,payload_hash=?,due_at=? WHERE id=? AND status='not_attempted' AND attempt_id IS NULL",
                (canonical(payload), digest(payload), due, item["id"]),
            )
            Store.audit(
                db,
                "item.membership_schedule_resolved",
                {
                    "item_id": item["id"],
                    "candidate_id": candidate["id"],
                    "previous_due_at": item["due_at"],
                    "due_at": due,
                    "delay_seconds": delay,
                },
                task["id"],
                self.clock(),
            )

    def _resume_membership_dependents(self, account_id, target):
        """Continue separately started groups without waiting for the rest of a batch."""
        tasks = self.store.rows(
            "SELECT DISTINCT t.id FROM tasks t JOIN items i ON i.task_id=t.id "
            "WHERE t.state='active' AND t.phase='starting' AND i.account_id=? "
            "AND lower(json_extract(i.payload,'$.target'))=lower(?) "
            "AND json_extract(t.policy,'$.outbound_after_membership_seconds') IS NOT NULL",
            (account_id, target),
        )
        for task in tasks:
            try:
                self.start_task(task["id"], source="scheduler")
            except Conflict:
                # Write permissions, stop and timing gates remain in force.
                pass

    def start_and_run(self, task_id, *, source="client"):
        """Shared client/Codex entry point for an explicit user execution instruction."""
        from .campaigns import Campaigns
        from .workers import WorkerRun

        task = self.task(task_id)
        if task.get("campaign_id"):
            Campaigns(self).populate(task_id)
        self.start_task(task_id, source=source)
        runtime = self.store.one("SELECT value FROM meta WHERE key='execution_runtime'")
        if runtime and runtime["value"] == "pooled":
            from .queue_store import JobQueue

            JobQueue(self).sync(task_id)
            task = self.task(task_id)
            task["execution_result"] = {"joins": [], "processed": [], "queued": True}
            return task
        with WorkerRun(self.store, task_id, live_enabled=self.live_enabled) as sender:
            sender.phase("executing")
            joined = self.run_joins(task_id)
            current = self.task(task_id)
            if current["phase"] == "starting" and current["state"] == "active":
                self.start_task(task_id, source="scheduler")
            result = self.run_once(task_id)
        task = self.task(task_id)
        task["execution_result"] = {"joins": joined["processed"], **result}
        return task

    def validate_item(self, task, value: ItemInput, *, account=None, opportunity=None):
        if value.account_id not in task["account_ids"]:
            raise Conflict("账号不在该任务范围内")
        account = account or self.account(value.account_id)
        if (
            account["mode"] == "live"
            and (
                account["platform"] == "reddit"
                or (
                    account["platform"] == "x"
                    and value.action not in account.get("automatic_actions", [])
                )
            )
            and value.execution_mode != "manual"
        ):
            raise Conflict("此平台当前仅开放人工互动清单，请选择人工执行方式")
        if value.opportunity_id:
            from .planner import content_key, execution_mode

            source = opportunity or self.store.one(
                "SELECT * FROM opportunities WHERE id=?", (value.opportunity_id,)
            )
            if not source or source["platform"] != account["platform"]:
                raise Conflict("共享内容不存在或平台不匹配")
            original = (
                source["payload"]
                if isinstance(source["payload"], dict)
                else json.loads(source["payload"])
            )
            if (
                account["platform"] in {"telegram", "discord"}
                and value.target != original["target"]
            ):
                raise Conflict("执行目标与共享内容不一致")
            observed = {
                **original,
                "target": value.target,
                "reply_to": value.reply_to,
                "discord_reply_to": value.discord_reply_to,
                "tiktok_video_id": value.tiktok_video_id,
                "tiktok_comment_id": value.tiktok_comment_id,
            }
            if (
                value.content_key != source["content_key"]
                or content_key(observed) != value.content_key
            ):
                raise Conflict("执行目标与共享内容不一致")
            mode, reason = execution_mode(account, original)
            if mode == "disabled" or value.execution_mode != mode:
                raise Conflict(reason)
        if value.action not in CAPABILITIES[account["platform"]] or value.action in {
            "discover",
            "join",
        }:
            raise Conflict("该平台不支持此外发动作")
        policy = task["policy"]
        if not epoch(policy["starts_at"]) <= epoch(value.scheduled_at) < epoch(policy["ends_at"]):
            raise Conflict("计划时间超出任务执行窗口")
        if policy["target_allowlist"] and value.target not in policy["target_allowlist"]:
            raise Conflict("目标不在任务的目标边界内")
        if RISK.search(value.target):
            raise Conflict("目标包含被禁止的类别信号")
        no_text_actions = {"like", "repost"}
        if account["platform"] == "tiktok":
            from .backends.tiktok import NO_TEXT_ACTIONS

            no_text_actions |= NO_TEXT_ACTIONS
        if value.action not in no_text_actions and not (value.text.strip() or value.media):
            raise Conflict("此动作需要正文或媒体")
        if value.action in {"like", "repost"} and (value.text or value.media or value.title):
            raise Conflict("点赞和转发不能附带正文或媒体")
        platform = account["platform"]
        if platform != "tiktok" and (
            value.tiktok_video_id or value.tiktok_comment_id or value.tiktok_options
        ):
            raise Conflict("TikTok 字段仅用于 TikTok")
        if platform == "tiktok":
            from .backends.common import BackendError
            from .backends.tiktok import validate_item

            try:
                validate_item(value.model_dump())
            except (BackendError, ValueError) as exc:
                raise Conflict(getattr(exc, "safe_code", "TikTok 发布选项无效")) from None
            for entry in value.media:
                self.verify_media(entry.model_dump())
                ceiling = 20 * 1024 * 1024 if entry.media_type == "IMAGE" else 1024 * 1024 * 1024
                if Path(entry.path).stat().st_size > ceiling:
                    raise Conflict("TikTok 图片最多 20 MiB，视频最多 1 GiB；网页上传仍最多 50 MiB")
        if platform != "discord" and value.discord_reply_to:
            raise Conflict("Discord 原消息 ID 仅用于 Discord")
        if platform == "discord":
            from .backends.common import BackendError
            from .backends.discord import MAX_FILE_BYTES, MAX_TOTAL_BYTES, snowflake

            try:
                snowflake(value.target)
                if value.action == "reply":
                    snowflake(value.discord_reply_to)
            except BackendError:
                raise Conflict("Discord 需要有效的频道 ID；回复还需要原消息 ID") from None
            if value.reply_to or value.title or value.recipient_id:
                raise Conflict("Discord 请使用频道 ID 和 Discord 原消息 ID，不支持标题或私信")
            if len(value.text) > 2000:
                raise Conflict("Discord 正文最多 2000 字符")
            if value.action == "send_file" and not value.media:
                raise Conflict("Discord 发送文件需要媒体")
            if value.action == "send_message" and value.media:
                raise Conflict("请使用 Discord 发送文件或回复动作上传媒体")
            if value.action != "reply" and value.discord_reply_to:
                raise Conflict("Discord 原消息 ID 仅用于回复")
            total = 0
            for media in value.media:
                self.verify_media(media.model_dump())
                size = Path(media.path).stat().st_size
                total += size
                if size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise Conflict("Discord 单文件最多 10 MiB，一条消息附件合计最多 25 MiB")
                if any(c in media.filename for c in '\r\n"\\'):
                    raise Conflict("Discord 文件名不能包含换行、引号或反斜杠")
        if platform == "x":
            parsed = urlparse(value.target)
            if (
                parsed.scheme != "https"
                or parsed.hostname not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
                or not re.fullmatch(r"/[A-Za-z0-9_]+/status/\d+/?", parsed.path)
            ):
                raise Conflict("X 目标必须是明确的帖子链接")
            if (
                len(value.text) > 280
                or len(value.media) > 4
                or (value.media and value.action != "reply")
            ):
                raise Conflict("X 回复/引用最多 280 字符；仅回复支持最多 4 个媒体")
        if platform == "reddit":
            if value.media:
                raise Conflict("Reddit 当前执行器仅支持文字")
            if value.action == "post" and (
                not value.title.strip()
                or not re.fullmatch(r"(?:r/)?[A-Za-z0-9_]{2,21}", value.target)
            ):
                raise Conflict("Reddit 帖子需要标题和社区名称")
            if value.action == "reply":
                u = urlparse(value.target)
                if (
                    u.scheme != "https"
                    or u.hostname not in {"reddit.com", "www.reddit.com", "old.reddit.com"}
                    or "/comments/" not in u.path
                ):
                    raise Conflict("Reddit 回复需要帖子链接")
        if platform == "instagram":
            if value.action == "publish" and (not value.target_owned or not value.media):
                raise Conflict("Instagram 发布必须使用自有账号并提供媒体")
            if value.action == "manual_comment" and (value.media or not value.text.strip()):
                raise Conflict("人工评论需要精确正文，不支持媒体")
            if (
                value.action == "publish"
                and account["mode"] == "live"
                and any(
                    not m.source_url or not urlparse(m.source_url).hostname for m in value.media
                )
            ):
                raise Conflict("Instagram 媒体需要可访问的 HTTPS 来源地址")
        if platform == "telegram":
            if value.action in {"send_file", "schedule_file"} and len(value.media) != 1:
                raise Conflict("此 Telegram 动作需要一个媒体文件")
            if value.action in {"send_message", "schedule_message", "reply"} and value.media:
                raise Conflict("请选择发送文件动作")
            if value.action == "reply" and not value.reply_to:
                raise Conflict("Telegram 回复需要原消息 ID")
        for media in value.media:
            self.verify_media(media.model_dump())
        return account

    def _validate_outbound_readiness(self, db, task, *, check_membership=True):
        now = self.clock()
        if now >= epoch(json.loads(task["policy"])["ends_at"]):
            raise Conflict("任务执行窗口已过期，请创建新任务")
        for row in db.execute(
            "SELECT * FROM items WHERE task_id=? ORDER BY position", (task["id"],)
        ):
            payload = json.loads(row["payload"])
            native = payload["action"] in {"schedule_message", "schedule_file"}
            if now - row["due_at"] > DELIVERY_GRACE_SECONDS or (native and row["due_at"] <= now):
                raise Conflict("清单包含已过期的计划时间，请创建新任务")
            reason = (
                telegram_blocker(db, task, payload, row["due_at"]) if check_membership else None
            )
            if reason:
                raise Conflict(f"{payload['target']}：{REASONS[reason]}")

    def verify_media(self, media):
        if Path(media["filename"]).name != media["filename"]:
            raise Conflict("媒体文件名不能包含目录")
        if not media["path"]:
            raise Conflict("请先上传媒体并绑定文件哈希")
        path = Path(media["path"]).resolve()
        if not path.is_relative_to(self.media_root) or not path.is_file():
            raise Conflict("媒体不在项目允许目录中")
        if hashlib.sha256(path.read_bytes()).hexdigest() != media["sha256"]:
            raise Conflict("媒体内容已变化，请创建新任务")
        if media.get("source_url") and urlparse(media["source_url"]).scheme != "https":
            raise Conflict("媒体来源地址必须使用 HTTPS")

    def set_items(self, task_id, values):
        task = self.task(task_id)
        if not values or len(values) > 200:
            raise Conflict("每批需要 1–200 个执行项")
        previous_due = 0
        last_by_account = {}
        payloads = []
        for index, value in enumerate(values):
            account = self.validate_item(task, value)
            due = epoch(value.scheduled_at)
            if due < previous_due:
                raise Conflict("执行项必须按计划时间排序")
            if (
                index
                and task["policy"].get("pacing_scope", "task") == "task"
                and due - previous_due < task["policy"]["min_interval"]
            ):
                raise Conflict("执行项之间短于任务最小间隔")
            gap = max(task["policy"]["min_interval"], account["min_interval"])
            if (
                value.account_id in last_by_account
                and due - last_by_account[value.account_id] < gap
            ):
                raise Conflict("计划间隔短于账号或任务限额")
            previous_due = due
            last_by_account[value.account_id] = due
            payloads.append(
                {
                    **value.model_dump(),
                    "position": index,
                    "platform": account["platform"],
                    "account_label": account["label"],
                }
            )
        if len(
            {
                digest({k: v for k, v in p.items() if k not in {"position", "scheduled_at"}})
                for p in payloads
            }
        ) != len(payloads):
            raise Conflict("清单含重复动作")
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            self._integrity(db, row, "discovery_join")
            if (
                row["manifest_hash"]
                or row["phase"] == "starting"
                or row["state"] in {"cancelled", "completed"}
            ):
                raise Conflict("清单已经冻结；变更内容请创建新任务")
            if row["campaign_id"] and any(not p.get("opportunity_id") for p in payloads):
                raise Conflict("活动执行项必须关联已分配的共享内容")
            db.execute("UPDATE campaign_assignments SET item_id=NULL WHERE task_id=?", (task_id,))
            db.execute("DELETE FROM items WHERE task_id=?", (task_id,))
            for payload in payloads:
                db.execute(
                    "INSERT INTO items(id,task_id,account_id,position,payload,payload_hash,due_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        "item-" + uuid.uuid4().hex[:16],
                        task_id,
                        payload["account_id"],
                        payload["position"],
                        canonical(payload),
                        digest(payload),
                        epoch(payload["scheduled_at"]),
                    ),
                )
                if payload.get("opportunity_id"):
                    assignment = db.execute(
                        "SELECT id,account_id FROM campaign_assignments WHERE task_id=? AND opportunity_id=?",
                        (task_id, payload["opportunity_id"]),
                    ).fetchone()
                    if not assignment or assignment["account_id"] != payload["account_id"]:
                        raise Conflict("执行项与活动分配不一致")
                    db.execute(
                        "UPDATE campaign_assignments SET item_id=(SELECT id FROM items WHERE task_id=? AND position=?) WHERE id=?",
                        (task_id, payload["position"], assignment["id"]),
                    )
            Store.audit(db, "items.saved", {"count": len(payloads)}, task_id, self.clock())
        return self.task(task_id)

    def prepare(self, task_id):
        task = self.task(task_id)
        if not task["items"]:
            raise Conflict("请先保存执行项")
        for item in task["items"]:
            payload = {
                k: v
                for k, v in item["payload"].items()
                if k not in {"position", "platform", "account_label"}
            }
            self.validate_item(task, ItemInput(**payload))
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            self._integrity(db, row, "discovery_join")
            if row["state"] in {"cancelled", "completed"}:
                raise Conflict("任务已结束")
            self._freeze_manifest(db, row)
        return self.task(task_id)

    def _freeze_manifest(self, db, row):
        if row["manifest_hash"]:
            self._integrity(db, row, "outbound")
            return
        self._validate_outbound_readiness(db, row)
        quotas.reserve(db, row)
        items = [
            json.loads(r["payload"])
            for r in db.execute(
                "SELECT payload FROM items WHERE task_id=? ORDER BY position", (row["id"],)
            )
        ]
        manifest = {
            "task_id": row["id"],
            "accounts": json.loads(row["account_snapshot"]),
            "policy": json.loads(row["policy"]),
            "items": items,
        }
        if row["campaign_id"]:
            manifest["campaign_id"] = row["campaign_id"]
            manifest["profiles"] = {
                r["account_id"]: json.loads(r["profile_snapshot"])
                for r in db.execute(
                    "SELECT account_id,profile_snapshot FROM campaign_assignments WHERE task_id=?",
                    (row["id"],),
                )
            }
        h = digest(manifest)
        db.execute(
            "UPDATE tasks SET manifest=?,manifest_hash=?,phase='review',updated_at=? WHERE id=?",
            (canonical(manifest), h, self.clock(), row["id"]),
        )
        Store.audit(
            db, "manifest.frozen", {"hash": h, "count": len(items)}, row["id"], self.clock()
        )

    def change_state(self, task_id, state):
        self.task(task_id)
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row["state"] in {"cancelled", "completed"}:
                raise Conflict("已结束任务不可恢复")
            if state == "active":
                self._integrity(db, row, "discovery_join")
            db.execute(
                "UPDATE tasks SET state=?,updated_at=? WHERE id=?", (state, self.clock(), task_id)
            )
            if state == "cancelled":
                db.execute(
                    "UPDATE items SET status='cancelled' WHERE task_id=? AND status='not_attempted'",
                    (task_id,),
                )
                db.execute(
                    "DELETE FROM quota_reservations WHERE task_id=? AND state='reserved'",
                    (task_id,),
                )
                db.execute(
                    "DELETE FROM content_contacts WHERE assignment_id IN (SELECT ca.id FROM campaign_assignments ca LEFT JOIN items i ON i.id=ca.item_id WHERE ca.task_id=? AND (i.id IS NULL OR i.attempt_id IS NULL))",
                    (task_id,),
                )
            Store.audit(
                db,
                "task." + state,
                {"native_schedules": "already submitted schedules require platform reconciliation"},
                task_id,
                self.clock(),
            )
        return self.task(task_id)

    def set_stop(self, stopped):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE meta SET value=? WHERE key='global_stop'", ("1" if stopped else "0",)
            )
            Store.audit(db, "global_stop", {"stopped": stopped})
        from .cloud_control import queue_settings

        queue_settings(self, {"global_stop": stopped})
        return {
            "global_stop": stopped,
            "native_schedule_note": "已提交的平台排期不受本地停止开关撤回",
        }

    def recover(self, lease_seconds=300):
        with self.store.connect(write=True) as db:
            rows = db.execute(
                "SELECT id,task_id,attempt_id,result FROM items WHERE status='executing' AND claimed_at<?",
                (self.clock() - lease_seconds,),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE items SET status='unknown',result=? WHERE id=?",
                    (
                        canonical(
                            {
                                **json.loads(row["result"]),
                                "reason": "worker_lease_expired",
                                "status": "unknown",
                                "verified": False,
                            }
                        ),
                        row["id"],
                    ),
                )
                db.execute(
                    "UPDATE attempts SET outcome='unknown',finished_at=? WHERE id=?",
                    (self.clock(), row["attempt_id"]),
                )
                Store.audit(
                    db,
                    "attempt.recovered_unknown",
                    {"item_id": row["id"]},
                    row["task_id"],
                    self.clock(),
                )
            joins = db.execute(
                "SELECT id,candidate_id FROM join_attempts WHERE outcome='claimed' AND started_at<?",
                (self.clock() - lease_seconds,),
            ).fetchall()
            for row in joins:
                db.execute(
                    "UPDATE join_attempts SET outcome='unknown',finished_at=? WHERE id=?",
                    (self.clock(), row["id"]),
                )
                db.execute(
                    "UPDATE candidates SET state='unknown',result=? WHERE id=?",
                    (canonical({"reason": "worker_lease_expired"}), row["candidate_id"]),
                )
        return len(rows)

    def _skip(self, item_id, reason, db=None, *, candidate=False):
        if db is None:
            with self.store.connect(write=True) as connection:
                return self._skip(item_id, reason, connection, candidate=candidate)
        table, state = ("candidates", "state") if candidate else ("items", "status")
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (item_id,)).fetchone()
        if not row or row[state] != "not_attempted":
            return None
        result = json.loads(row["result"])
        if result.get("skip_reason") == reason:
            return None
        result.update(
            skip_reason=reason, skip_message=REASONS[reason], skip_recorded_at=self.clock()
        )
        db.execute(f"UPDATE {table} SET result=? WHERE id=?", (canonical(result), item_id))
        Store.audit(
            db,
            "candidate.skipped" if candidate else "item.skipped",
            {"candidate_id" if candidate else "item_id": item_id, "reason": reason},
            row["task_id"],
            self.clock(),
        )
        return None

    def _claim(self, item_id, backend_gate=None):
        now = self.clock()
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row or row["status"] != "not_attempted":
                return None
            task = db.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
            if task["state"] != "active":
                return self._skip(item_id, "task_not_active", db)
            if task["campaign_id"]:
                campaign = db.execute(
                    "SELECT state FROM campaigns WHERE id=?", (task["campaign_id"],)
                ).fetchone()
                if not campaign or campaign[0] in {"paused", "cancelled"}:
                    return self._skip(item_id, "task_not_active", db)
            if task["phase"] != "ready":
                return self._skip(item_id, "task_not_started", db)
            policy = self._integrity(db, task, "outbound")
            if db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()[0] != "0":
                return self._skip(item_id, "global_stop", db)
            payload = json.loads(row["payload"])
            account = db.execute(
                "SELECT * FROM accounts WHERE id=?", (row["account_id"],)
            ).fetchone()
            account = dict(account)
            account["automatic_actions"] = automatic_actions(account)
            if (
                account["mode"] == "live"
                and (
                    account["platform"] == "reddit"
                    or (
                        account["platform"] == "x"
                        and payload["action"] not in account["automatic_actions"]
                    )
                )
                and payload.get("execution_mode", "automatic") != "manual"
            ):
                return self._skip(item_id, "platform_policy_blocked", db)
            if payload.get("opportunity_id"):
                from .planner import execution_mode

                source = db.execute(
                    "SELECT payload FROM opportunities WHERE id=?", (payload["opportunity_id"],)
                ).fetchone()
                if not source or execution_mode(dict(account), json.loads(source[0]))[
                    0
                ] != payload.get("execution_mode", "automatic"):
                    return self._skip(item_id, "target_cannot_send", db)
                content = json.loads(source[0])
                if content.get("expires_at") and now >= epoch(content["expires_at"]):
                    return self._skip(item_id, "delivery_window_elapsed", db)
            if account["disabled"]:
                return self._skip(item_id, "account_disabled", db)
            if account["status"] != "healthy":
                return self._skip(item_id, "account_unhealthy", db)
            if account["cooldown_until"] > now:
                return self._skip(item_id, "account_cooldown", db)
            if account["next_at"] > now or (
                policy.get("pacing_scope", "task") == "task" and task["next_at"] > now
            ):
                return self._skip(item_id, "pacing_wait", db)
            if (
                account["mode"] == "live"
                and payload.get("execution_mode", "automatic") == "automatic"
            ):
                if not self.live_enabled:
                    return self._skip(item_id, "live_writes_disabled", db)
                if not self.adapters.commissioned(account):
                    return self._skip(item_id, "backend_not_commissioned", db)
                if not backend_gate or backend_gate.get("allowed") is not True:
                    return self._skip(item_id, "backend_gate_blocked", db)
            # Native schedules are submitted ahead of delivery; other actions are claimed when due.
            native = payload["action"] in {"schedule_message", "schedule_file"}
            if now < epoch(policy["starts_at"]) or (not native and row["due_at"] > now):
                return self._skip(item_id, "scheduled_time_pending", db)
            if (
                now >= epoch(policy["ends_at"])
                or now - row["due_at"] > DELIVERY_GRACE_SECONDS
                or (native and row["due_at"] <= now)
            ):
                db.execute(
                    "UPDATE items SET status='missed',result=? WHERE id=?",
                    (canonical({"reason": "delivery_window_elapsed"}), item_id),
                )
                return None
            if policy.get("ordering_mode", "strict") == "independent_accounts":
                earlier = db.execute(
                    "SELECT status FROM items WHERE task_id=? AND account_id=? AND position<?",
                    (task["id"], account["id"], row["position"]),
                ).fetchall()
            else:
                earlier = db.execute(
                    "SELECT status FROM items WHERE task_id=? AND position<?",
                    (task["id"], row["position"]),
                ).fetchall()
            if any(r["status"] not in TERMINAL for r in earlier):
                return self._skip(item_id, "earlier_item_pending", db)
            target_key = digest([account["platform"], payload["target"].casefold()])
            user_key = digest(
                [account["platform"], payload["recipient_id"] or payload["target"].casefold()]
            )
            day = now - now % 86400
            mode = account["mode"]
            if backend_gate and "daily_remaining" in backend_gate:
                used = db.execute(
                    "SELECT count(*) FROM activity WHERE account_id=? AND started_at>=?",
                    (account["id"], day),
                ).fetchone()[0]
                if used >= int(backend_gate["daily_remaining"]):
                    return self._skip(item_id, "activity_limit", db)
            quota_reason = quotas.check_claim(
                db, account, task, payload, row["due_at"], now, item_id
            )
            if quota_reason:
                return self._skip(item_id, quota_reason, db)
            for media in payload["media"]:
                self.verify_media(media)
            reason = telegram_blocker(db, task, payload, now)
            if reason:
                return self._skip(item_id, reason, db)
            attempt = "attempt-" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO attempts(id,item_id,account_id,task_id,target_key,user_key,mode,started_at) VALUES(?,?,?,?,?,?,?,?)",
                (attempt, item_id, account["id"], task["id"], target_key, user_key, mode, now),
            )
            db.execute(
                "UPDATE items SET status='executing',attempt_id=?,claimed_at=?,result='{}' WHERE id=?",
                (attempt, now, item_id),
            )
            db.execute("UPDATE quota_reservations SET state='consumed' WHERE item_id=?", (item_id,))
            db.execute(
                "UPDATE accounts SET next_at=? WHERE id=?",
                (now + max(account["min_interval"], policy["min_interval"]), account["id"]),
            )
            db.execute(
                "UPDATE tasks SET next_at=? WHERE id=?",
                (now + policy["min_interval"], task["id"]),
            )
            Store.audit(
                db, "attempt.claimed", {"item_id": item_id, "attempt_id": attempt}, task["id"], now
            )
            return {
                "item_id": item_id,
                "task_id": task["id"],
                "attempt_id": attempt,
                "payload": payload,
                "manifest_hash": task["manifest_hash"],
            }

    def _queue_pooled_task(self, task_id):
        runtime = self.store.one("SELECT value FROM meta WHERE key='execution_runtime'")
        if (
            not runtime
            or runtime["value"] != "pooled"
            or getattr(self.adapters.context, "job", None)
        ):
            return False
        from .queue_store import JobQueue

        JobQueue(self).sync(task_id)
        return True

    def run_once(self, task_id=None):
        if self._queue_pooled_task(task_id):
            return {"processed": [], "queued": True}
        self.recover()
        if task_id:
            self.task(task_id)
        stopped = self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0"
        sql = "SELECT i.id,i.account_id,i.task_id,i.payload,i.due_at,t.policy,t.mode,t.next_at,t.state,t.phase FROM items i JOIN tasks t ON t.id=i.task_id WHERE i.status='not_attempted' AND t.state NOT IN ('cancelled','completed')"
        params = ()
        if task_id:
            sql += " AND i.task_id=?"
            params = (task_id,)
        rows = self._dispatchable_rows(
            self.store.rows(sql + " ORDER BY due_at,i.position", params), stopped
        )
        groups = {}
        strict = {}
        for row in rows:
            if json.loads(row["policy"]).get("ordering_mode", "strict") == "strict":
                strict.setdefault(row["task_id"], []).append(row)
            else:
                groups.setdefault(row["account_id"], []).append(row)
        limits = self.execution_settings()
        processed = []
        with ThreadPoolExecutor(
            max_workers=limits["max_workers"], thread_name_prefix="account-sender"
        ) as executor:
            futures = [
                executor.submit(self._run_account_group, group, stopped, limits)
                for group in groups.values()
            ]
            futures.extend(
                executor.submit(self._run_strict_rows, group, stopped, limits)
                for group in strict.values()
            )
            for future in futures:
                processed.extend(future.result())
        with self.store.connect(write=True) as db:
            self._mark_completed(db)
        return {"processed": processed}

    def _run_strict_rows(self, rows, stopped, limits):
        processed = []
        for row in rows:
            processed.extend(self._run_account_group([row], stopped, limits))
        return processed

    def _dispatchable_rows(self, rows, stopped):
        """Keep local waits out of shared account lanes; claims recheck every gate."""
        ready = []
        for row in rows:
            policy = json.loads(row["policy"])
            payload = json.loads(row["payload"])
            now = self.clock()
            native = payload["action"] in {"schedule_message", "schedule_file"}
            reason = None
            if row["state"] != "active":
                reason = "task_not_active"
            elif (
                now >= epoch(policy["ends_at"])
                or now - row["due_at"] > DELIVERY_GRACE_SECONDS
                or (native and row["due_at"] <= now)
            ):
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE items SET status='missed',result=? WHERE id=? AND status='not_attempted'",
                        (canonical({"reason": "delivery_window_elapsed"}), row["id"]),
                    )
                continue
            elif row["phase"] != "ready":
                reason = "task_not_started"
            elif stopped:
                reason = "global_stop"
            elif (
                row["mode"] == "live"
                and payload.get("execution_mode", "automatic") != "manual"
                and not self.live_enabled
            ):
                reason = "live_writes_disabled"
            elif now < epoch(policy["starts_at"]) or (not native and row["due_at"] > now):
                reason = "scheduled_time_pending"
            elif policy.get("pacing_scope", "task") == "task" and row["next_at"] > now:
                reason = "pacing_wait"
            else:
                account = self.account(row["account_id"])
                if account["disabled"]:
                    reason = "account_disabled"
                elif account["cooldown_until"] > now:
                    reason = "account_cooldown"
                elif account["next_at"] > now:
                    reason = "pacing_wait"
                else:
                    with self.store.connect() as db:
                        task = db.execute(
                            "SELECT * FROM tasks WHERE id=?", (row["task_id"],)
                        ).fetchone()
                        reason = telegram_blocker(db, task, payload, now)
            if reason:
                self._skip(row["id"], reason)
            else:
                ready.append(row)
        return ready

    def _run_account_group(self, rows, stopped, limits):
        rows = self._dispatchable_rows(rows, stopped)
        if not rows:
            return []
        try:
            account = self.account(rows[0]["account_id"])
            route = route_for(account, json.loads(rows[0]["payload"]))
            with self.lanes.acquire(rows[0]["account_id"], limits, resource=route.resource):
                return self._run_account_rows(rows, stopped)["processed"]
        except AccountBusy:
            for row in rows:
                self._skip(row["id"], "account_busy")
            return []

    def _run_account_rows(self, rows, stopped):
        processed = []
        checked = set()
        for row in rows:
            if row["state"] != "active":
                self._skip(row["id"], "task_not_active")
                continue
            if row["phase"] != "ready":
                self._skip(
                    row["id"],
                    "task_not_started",
                )
                continue
            if stopped:
                self._skip(row["id"], "global_stop")
                continue
            manual = json.loads(row["payload"]).get("execution_mode", "automatic") == "manual"
            if row["mode"] == "live" and not manual and not self.live_enabled:
                self._skip(row["id"], "live_writes_disabled")
                continue
            if (
                json.loads(row["policy"]).get("pacing_scope", "task") == "task"
                and row["next_at"] > self.clock()
            ):
                self._skip(row["id"], "pacing_wait")
                continue
            policy = json.loads(row["policy"])
            native = json.loads(row["payload"])["action"] in {"schedule_message", "schedule_file"}
            if (
                self.clock() >= epoch(policy["ends_at"])
                or self.clock() - row["due_at"] > DELIVERY_GRACE_SECONDS
                or (native and row["due_at"] <= self.clock())
            ):
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE items SET status='missed',result=? WHERE id=? AND status='not_attempted'",
                        (canonical({"reason": "delivery_window_elapsed"}), row["id"]),
                    )
                continue
            if self.clock() < epoch(policy["starts_at"]) or (
                not native and row["due_at"] > self.clock()
            ):
                self._skip(row["id"], "scheduled_time_pending")
                continue
            try:
                with self.store.connect() as db:
                    task = db.execute(
                        "SELECT * FROM tasks WHERE id=?", (row["task_id"],)
                    ).fetchone()
                    self._integrity(db, task, "outbound")
                    reason = telegram_blocker(db, task, json.loads(row["payload"]), self.clock())
                if reason:
                    self._skip(row["id"], reason)
                    continue
            except Conflict as exc:
                self._skip(row["id"], "manifest_integrity_failed")
                processed.append({"item_id": row["id"], "status": "blocked", "reason": str(exc)})
                continue
            account = self.account(row["account_id"])
            if not manual and not self.adapters.commissioned(account):
                self._skip(row["id"], "backend_not_commissioned")
                continue
            if account["disabled"] or account["cooldown_until"] > self.clock():
                self._skip(
                    row["id"], "account_disabled" if account["disabled"] else "account_cooldown"
                )
                continue
            if not manual and account["id"] not in checked:
                # A fresh read-only health check before a live claim; simulation needs one too.
                self.check_account(account["id"])
                checked.add(account["id"])
            account = self.account(row["account_id"])
            if account["status"] != "healthy" or account["cooldown_until"] > self.clock():
                self._skip(
                    row["id"],
                    "account_cooldown"
                    if account["cooldown_until"] > self.clock()
                    else "account_unhealthy",
                )
                continue
            try:
                gate = (
                    self.call(
                        account,
                        "preflight",
                        {"payload": json.loads(row["payload"])},
                        row["task_id"],
                    )
                    if account["mode"] == "live" and not manual
                    else None
                )
                if gate:
                    self.apply_cooldown(account["id"], gate)
                if gate and gate.get("allowed") is not True:
                    self._skip(row["id"], "backend_gate_blocked")
                    processed.append(
                        {
                            "item_id": row["id"],
                            "status": "blocked",
                            "reason": "原平台停止、冷却或限额尚未允许执行",
                        }
                    )
                    continue
                claim = self._claim(row["id"], gate)
            except Conflict as exc:
                self._skip(row["id"], "manifest_integrity_failed")
                processed.append({"item_id": row["id"], "status": "blocked", "reason": str(exc)})
                continue
            if not claim:
                continue
            account = self.account(row["account_id"])
            if manual or claim["payload"]["action"] == "manual_comment":
                result = {"status": "manual_attention", "reason": "manual_publication_required"}
            else:
                # Stop/pause can race a claim. Recheck before entering the adapter.
                current = self.task(claim["task_id"])
                stopped = (
                    self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0"
                )
                if stopped or current["state"] != "active":
                    result = {"status": "failed_known", "reason": "stopped_before_submission"}
                else:
                    try:
                        result = self.call(account, "execute", claim, claim["task_id"])
                    except Conflict:
                        result = {"status": "unknown", "reason": "transport_outcome_unknown"}
            normalized = self.finish(claim, result)
            processed.append({"item_id": row["id"], **normalized})
        with self.store.connect(write=True) as db:
            self._mark_completed(db)
        return {"processed": processed}

    def _mark_completed(self, db, task_id=None):
        db.execute(
            "DELETE FROM quota_reservations WHERE state='reserved' AND item_id IN (SELECT id FROM items WHERE status IN ('cancelled','missed') AND attempt_id IS NULL)"
        )
        db.execute(
            "DELETE FROM content_contacts WHERE assignment_id IN (SELECT ca.id FROM campaign_assignments ca JOIN items i ON i.id=ca.item_id WHERE i.status IN ('cancelled','missed') AND i.attempt_id IS NULL)"
        )
        scope = " AND id=?" if task_id else " AND state='active'"
        params = (self.clock(), task_id) if task_id else (self.clock(),)
        db.execute(
            "UPDATE tasks SET state='completed',updated_at=? "
            "WHERE state NOT IN ('cancelled','completed') AND phase='ready' "
            "AND EXISTS(SELECT 1 FROM items WHERE task_id=tasks.id) "
            "AND NOT EXISTS(SELECT 1 FROM items WHERE task_id=tasks.id "
            "AND status NOT IN ('confirmed','failed_known','missed','cancelled'))" + scope,
            params,
        )
        db.execute(
            "UPDATE campaigns SET state='completed',updated_at=? WHERE state='materialized' AND EXISTS(SELECT 1 FROM tasks WHERE campaign_id=campaigns.id) AND NOT EXISTS(SELECT 1 FROM tasks WHERE campaign_id=campaigns.id AND state NOT IN ('completed','cancelled'))",
            (self.clock(),),
        )

    def finish(self, claim, result):
        status = result.get("status", "unknown")
        reference = result.get("external_reference")
        if status == "confirmed" and not (result.get("verified") and reference):
            status = "unknown"
        if status == "scheduled":
            ids = set(map(str, result.get("message_ids", [])))
            found = set(map(str, result.get("lookup_message_ids", [])))
            if not (
                reference
                and ids
                and ids <= found
                and all(result.get(k) is True for k in ("scheduled", "active", "verified"))
            ):
                status = "unknown"
        if status not in TERMINAL:
            status = "unknown"
        safe = {
            k: result[k]
            for k in (
                "verified",
                "scheduled",
                "active",
                "message_ids",
                "lookup_message_ids",
                "reason",
                "external_reference",
                "published_at",
                "scheduled_message_ids",
                "publish_id",
                "post_ids",
                "comment_id",
                "platform_status",
            )
            + FIELDS
            if k in result
        }
        safe["status"] = status
        now = self.clock()
        if "published_at" in safe:
            safe["published_at"] = publication_time(safe["published_at"], now)
        with self.store.connect(write=True) as db:
            row = db.execute(
                "SELECT * FROM items WHERE id=? AND attempt_id=?",
                (claim["item_id"], claim["attempt_id"]),
            ).fetchone()
            if not row:
                raise Conflict("执行所有者不匹配")
            if row["status"] not in {"executing", "unknown"}:
                return json.loads(row["result"])
            previous = json.loads(row["result"])
            for key in FIELDS:
                if key not in safe and key in previous:
                    safe[key] = previous[key]
            if not reference and row["external_reference"]:
                reference = row["external_reference"]
                safe["external_reference"] = reference
            db.execute(
                "UPDATE items SET status=?,external_reference=?,result=? WHERE id=?",
                (status, reference, canonical(safe), claim["item_id"]),
            )
            db.execute(
                "UPDATE attempts SET outcome=?,finished_at=? WHERE id=?",
                (status, now, claim["attempt_id"]),
            )
            if status == "confirmed":
                record_publication(db, claim["item_id"], safe, now)
            if result.get("rate_limited") or result.get("risk_warning"):
                cooldown = now + max(1, int(result.get("retry_after_seconds") or 3600))
                db.execute(
                    "UPDATE accounts SET cooldown_until=max(cooldown_until,?) WHERE id=?",
                    (cooldown, row["account_id"]),
                )
                if result.get("risk_warning"):
                    db.execute("UPDATE accounts SET disabled=1 WHERE id=?", (row["account_id"],))
                Store.audit(
                    db,
                    "account.cooldown",
                    {"account_id": row["account_id"], "until": cooldown},
                    claim["task_id"],
                    now,
                )
            Store.audit(
                db,
                "attempt." + status,
                {
                    "item_id": claim["item_id"],
                    "external_reference": reference,
                    **{
                        k: safe[k]
                        for k in (
                            "reason",
                            "execution_step",
                            "submission_started",
                            "exception_type",
                        )
                        if k in safe
                    },
                },
                claim["task_id"],
                now,
            )
            self._mark_completed(db, claim["task_id"])
        return safe

    def reconcile(self, item_id):
        row = self.store.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not row:
            raise NotFound("执行项不存在")
        if row["status"] not in {"unknown", "scheduled"}:
            raise Conflict("只有未知结果或平台排期需要核对")
        payload = {
            "item_id": item_id,
            "task_id": row["task_id"],
            "attempt_id": row["attempt_id"],
            "payload": json.loads(row["payload"]),
            "external_reference": row["external_reference"],
            "result": json.loads(row["result"]),
        }
        account = self.account(row["account_id"])
        if (
            account["platform"] in {"discord", "tiktok"}
            and account["cooldown_until"] > self.clock()
        ):
            raise Conflict("账号仍在冷却，请稍后核对")
        result = self.call(account, "verify", payload, row["task_id"])
        self.apply_cooldown(account["id"], result)
        if not result.get("verified"):
            if account["platform"] == "tiktok":
                progress = {
                    k: result[k]
                    for k in ("publish_id", "post_ids", "comment_id", "platform_status", "reason")
                    if k in result
                }
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE items SET result=? WHERE id=? AND status='unknown'",
                        (canonical({**json.loads(row["result"]), **progress}), item_id),
                    )
                return {"status": row["status"], **progress}
            return {"status": row["status"], "reason": "not_conclusively_verified"}
        # Reconciliation never turns an item back into a retryable state.
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='unknown' WHERE id=? AND status='scheduled'", (item_id,)
            )
        return self.finish(payload, result)

    def collect_feedback(self, item_id):
        row = self.store.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not row or row["status"] != "confirmed":
            raise Conflict("回复采集需要已核验的发布结果")
        account = self.account(row["account_id"])
        if account["mode"] != "live" or account["platform"] != "telegram":
            raise Conflict("当前回复采集仅接入真实 Telegram 互动")
        if account["cooldown_until"] > self.clock():
            raise Conflict("账号仍在冷却，请稍后采集回复")
        check = self.store.one("SELECT * FROM feedback_checks WHERE item_id=?", (item_id,))
        statistics = Statistics(self.store, clock=self.clock)
        try:
            result = self.call(
                account,
                "feedback",
                {
                    "item_id": item_id,
                    "payload": json.loads(row["payload"]),
                    "result": json.loads(row["result"]),
                    "feedback_cursor": check["last_message_id"] if check else 0,
                },
                row["task_id"],
            )
            self.apply_cooldown(account["id"], result)
            if result.get("verified") is not True:
                statistics.mark_feedback(
                    item_id,
                    self.clock(),
                    "failed",
                    error_code=result.get("reason", "feedback_unverified"),
                )
                return {
                    "item_id": item_id,
                    "status": "failed",
                    "reason": safe_code(result.get("reason"), "feedback_unverified"),
                }
            count = 0
            for event in result.get("feedback_events", []):
                count += statistics.record_feedback(
                    item_id, event["event_id"], event["occurred_at"]
                )
            statistics.mark_feedback(
                item_id,
                result["feedback_checked_through"],
                result["feedback_status"],
                cursor=result["feedback_cursor"],
                error_code=result.get("reason"),
            )
            return {
                "item_id": item_id,
                "status": result["feedback_status"],
                "new_replies": count,
                **({"reason": safe_code(result["reason"])} if result.get("reason") else {}),
            }
        except Exception as exc:
            statistics.mark_feedback(
                item_id,
                self.clock(),
                "failed",
                error_code=getattr(exc, "safe_code", type(exc).__name__),
            )
            raise

    def collect_statistics(self):
        """Read existing Telegram publications and feedback once per reporting boundary."""
        now = self.clock()
        local = datetime.fromtimestamp(now, ZONE)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        slot = midnight + max(h for h in (0, 3, 6, 9, 12, 15, 18, 20, 21) if h <= local.hour) * 3600
        with self.store.connect(write=True) as db:
            previous = db.execute(
                "SELECT value FROM meta WHERE key='statistics_collection_slot'"
            ).fetchone()
            if previous and float(previous[0]) >= slot:
                return {"processed": []}
            db.execute(
                "INSERT INTO meta VALUES('statistics_collection_slot',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(slot),),
            )
        processed = []
        rows = self.store.rows(
            "SELECT i.id,i.status,i.due_at FROM items i JOIN accounts a ON a.id=i.account_id "
            "JOIN tasks t ON t.id=i.task_id WHERE a.platform='telegram' AND a.mode='live' AND t.mode='live' "
            "AND i.status IN ('confirmed','scheduled','unknown') AND a.cooldown_until<=? ORDER BY i.due_at",
            (now,),
        )
        for row in rows:
            try:
                if row["status"] in {"scheduled", "unknown"} and row["due_at"] <= now:
                    self.reconcile(row["id"])
                if (
                    self.store.one("SELECT status FROM items WHERE id=?", (row["id"],))["status"]
                    == "confirmed"
                ):
                    processed.append(self.collect_feedback(row["id"]))
            except Exception:
                processed.append({"item_id": row["id"], "status": "unavailable"})
        return {"processed": processed}

    def manual_outcome(self, item_id, value):
        row = self.store.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not row or row["status"] != "manual_attention":
            raise Conflict("该项目不在人工处理状态")
        payload = json.loads(row["payload"])
        if payload["action"] != "manual_comment" and payload.get("execution_mode") != "manual":
            raise Conflict("该动作不支持人工登记")
        hosts = {
            "instagram": {"www.instagram.com", "instagram.com"},
            "x": {"x.com", "twitter.com", "www.x.com", "www.twitter.com"},
            "reddit": {"www.reddit.com", "reddit.com", "old.reddit.com"},
            "telegram": {"t.me"},
            "discord": {"discord.com"},
            "tiktok": {"www.tiktok.com", "tiktok.com"},
        }
        reference = urlparse(value.external_reference)
        exact = value.observed_text == payload["text"]
        status = (
            "confirmed"
            if exact
            and value.verified
            and reference.scheme == "https"
            and reference.hostname in hosts[payload["platform"]]
            else "unknown"
        )
        result = {
            "status": status,
            "verified": status == "confirmed",
            "external_reference": value.external_reference,
            "reason": "manual_result_recorded",
        }
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status=?,external_reference=?,result=? WHERE id=?",
                (status, value.external_reference, canonical(result), item_id),
            )
            db.execute(
                "UPDATE attempts SET outcome=?,finished_at=? WHERE id=?",
                (status, self.clock(), row["attempt_id"]),
            )
            Store.audit(
                db,
                "manual.outcome",
                {"item_id": item_id, "exact_text": exact, "status": status},
                row["task_id"],
                self.clock(),
            )
            self._mark_completed(db, row["task_id"])
        return result

    def candidate(self, task_id, value: CandidateInput):
        task = self.task(task_id)
        if value.account_id not in task["account_ids"]:
            raise Conflict("候选账号超出任务范围")
        p = task["policy"]
        # Only the literal annotation "non-adult" is negative evidence. Titles,
        # targets and any other adult/NSFW signals still pass through the risk gate.
        content = (
            value.title + " " + NON_ADULT.sub("", value.text) + " " + value.target
        ).casefold()
        reasons = []
        if RISK.search(content):
            reasons.append("blocked_category")
        if not value.is_public:
            reasons.append("not_public")
        if value.confidence < p["min_confidence"]:
            reasons.append("low_confidence")
        if value.language not in p["languages"]:
            reasons.append("language")
        if value.members < p["min_members"] or value.activity < p["min_activity"]:
            reasons.append("size_or_activity")
        if any(k.casefold() not in content for k in p["required_keywords"]):
            reasons.append("required_keywords")
        if any(k.casefold() in content for k in p["blocked_keywords"]):
            reasons.append("blocked_keywords")
        if p["queries"] and not any(
            any(w in content for w in q.casefold().split()) for q in p["queries"]
        ):
            reasons.append("unrelated_topic")
        if p["target_allowlist"] and value.target not in p["target_allowlist"]:
            reasons.append("target_boundary")
        decision = "rejected" if reasons else "qualified"
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            self._integrity(db, row, "discovery_join")
            if row["state"] in {"paused", "cancelled", "completed"}:
                raise Conflict("任务已暂停或结束")
            existing = db.execute(
                "SELECT * FROM candidates WHERE task_id=? AND account_id=? AND target=?",
                (task_id, value.account_id, value.target),
            ).fetchone()
            payload = canonical(value.model_dump())
            if existing:
                if existing["payload"] == payload:
                    return {
                        "decision": existing["decision"],
                        "reasons": json.loads(existing["reasons"]),
                    }
                if row["manifest_hash"] or existing["state"] != "not_attempted":
                    raise Conflict("已冻结清单或已尝试入群的候选不可更正")
                if db.execute(
                    "SELECT 1 FROM join_attempts WHERE candidate_id=?", (existing["id"],)
                ).fetchone():
                    raise Conflict("已尝试入群的候选不可更正")
                db.execute(
                    "UPDATE candidates SET payload=?,decision=?,reasons=? WHERE id=?",
                    (payload, decision, canonical(reasons), existing["id"]),
                )
                Store.audit(
                    db,
                    "candidate.corrected",
                    {
                        "candidate_id": existing["id"],
                        "before_hash": digest(json.loads(existing["payload"])),
                        "after_hash": digest(value.model_dump()),
                        "decision": decision,
                        "reasons": reasons,
                    },
                    task_id,
                    self.clock(),
                )
                return {"decision": decision, "reasons": reasons}
            if (
                db.execute(
                    "SELECT count(*) FROM candidates WHERE task_id=?", (task_id,)
                ).fetchone()[0]
                >= p["max_candidates"]
            ):
                raise Conflict("已达到候选上限")
            db.execute(
                "INSERT OR IGNORE INTO candidates(id,task_id,account_id,target,payload,decision,reasons) VALUES(?,?,?,?,?,?,?)",
                (
                    "candidate-" + uuid.uuid4().hex[:12],
                    task_id,
                    value.account_id,
                    value.target,
                    payload,
                    decision,
                    canonical(reasons),
                ),
            )
            Store.audit(
                db,
                "candidate." + decision,
                {"target": value.target, "reasons": reasons},
                task_id,
                self.clock(),
            )
        return {"decision": decision, "reasons": reasons}

    def discover(self, task_id, account_id):
        task = self.task(task_id)
        if account_id not in task["account_ids"]:
            raise Conflict("账号不在任务内")
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            self._integrity(db, row, "discovery_join")
            if row["state"] in {"paused", "cancelled", "completed"}:
                raise Conflict("任务已暂停或结束")
        account = self.account(account_id)
        if "discover" not in CAPABILITIES[account["platform"]]:
            raise Conflict("该平台请通过候选录入接入")
        result = self.call(account, "discover", {"policy": task["policy"]}, task_id)
        decisions = []
        for candidate in result.get("candidates", [])[: task["policy"]["max_candidates"]]:
            decisions.append(
                self.candidate(task_id, CandidateInput(**{**candidate, "account_id": account_id}))
            )
        return {"decisions": decisions, "source_note": result.get("source_note", "")}

    def run_joins(self, task_id):
        task = self.task(task_id)
        if self._queue_pooled_task(task_id):
            return {"processed": [], "queued": True}
        processed = []
        if not task["policy"]["max_joins"]:
            return {"processed": processed}
        for account_id in task["account_ids"]:
            task = self.task(task_id)
            # Future windows, cooldowns and completed candidates need no account lane.
            pending = [
                c
                for c in task["candidates"]
                if c["account_id"] == account_id and c["state"] == "not_attempted"
            ]
            account = self.account(account_id)
            if account["platform"] != "telegram":
                continue
            eligible = False
            for candidate in pending:
                reason = self._join_blocker(task, candidate, account)
                if reason:
                    self._skip(candidate["id"], reason, candidate=True)
                else:
                    eligible = True
            if not eligible:
                continue
            try:
                with self.lanes.acquire(account_id, self.execution_settings()):
                    processed.extend(self._run_joins(task_id, account_id)["processed"])
            except AccountBusy:
                continue
        return {"processed": processed}

    def _join_blocker(self, task, candidate, account):
        if candidate["decision"] != "qualified":
            return "candidate_rejected"
        if task["state"] != "active":
            return "task_not_active"
        if task["phase"] not in {"discovered", "starting", "ready"}:
            return "task_not_started"
        if self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0":
            return "global_stop"
        if account["mode"] == "live" and not self.live_enabled:
            return "live_writes_disabled"
        if not self.adapters.commissioned(account):
            return "backend_not_commissioned"
        now = self.clock()
        if now >= epoch(task["policy"]["ends_at"]):
            return "delivery_window_elapsed"
        if now < epoch(task["policy"]["starts_at"]):
            return "scheduled_time_pending"
        if account["disabled"]:
            return "account_disabled"
        if account["cooldown_until"] > now:
            return "account_cooldown"
        if account["next_at"] > now or task["next_at"] > now:
            return "pacing_wait"
        return None

    def _run_joins(self, task_id, account_id, candidate_id=None):
        self.recover()
        task = self.task(task_id)
        processed = []
        if not task["policy"]["max_joins"]:
            return {"processed": []}
        for candidate in task["candidates"]:
            if candidate_id and candidate["id"] != candidate_id:
                continue
            if candidate["account_id"] != account_id:
                continue
            if candidate["state"] != "not_attempted":
                continue
            if candidate["decision"] != "qualified":
                self._skip(candidate["id"], "candidate_rejected", candidate=True)
                continue
            account = self.account(candidate["account_id"])
            if account["platform"] != "telegram":
                continue
            if account["mode"] == "live" and not self.live_enabled:
                self._skip(candidate["id"], "live_writes_disabled", candidate=True)
                continue
            if not self.adapters.commissioned(account):
                self._skip(candidate["id"], "backend_not_commissioned", candidate=True)
                continue
            if task["state"] != "active":
                self._skip(candidate["id"], "task_not_active", candidate=True)
                continue
            if self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0":
                self._skip(candidate["id"], "global_stop", candidate=True)
                continue
            if (
                not epoch(task["policy"]["starts_at"])
                <= self.clock()
                < epoch(task["policy"]["ends_at"])
            ):
                self._skip(
                    candidate["id"],
                    "scheduled_time_pending"
                    if self.clock() < epoch(task["policy"]["starts_at"])
                    else "delivery_window_elapsed",
                    candidate=True,
                )
                continue
            with self.store.connect() as db:
                row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                self._integrity(db, row, "discovery_join")
            # Avoid an unnecessary live identity check while this task is waiting
            # for its persisted per-account interval or platform cooldown.
            if (
                account["next_at"] > self.clock()
                or task["next_at"] > self.clock()
                or account["cooldown_until"] > self.clock()
            ):
                self._skip(
                    candidate["id"],
                    "account_cooldown"
                    if account["cooldown_until"] > self.clock()
                    else "pacing_wait",
                    candidate=True,
                )
                continue
            self.check_account(account["id"])
            now = self.clock()
            with self.store.connect(write=True) as db:
                row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                policy = self._integrity(db, row, "discovery_join")
                a = db.execute("SELECT * FROM accounts WHERE id=?", (account["id"],)).fetchone()
                c = db.execute("SELECT * FROM candidates WHERE id=?", (candidate["id"],)).fetchone()
                if (
                    row["state"] != "active"
                    or c["state"] != "not_attempted"
                    or c["decision"] != "qualified"
                ):
                    self._skip(candidate["id"], "task_not_active", db, candidate=True)
                    continue
                if (
                    db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()[0]
                    != "0"
                ):
                    self._skip(candidate["id"], "global_stop", db, candidate=True)
                    break
                if not epoch(policy["starts_at"]) <= now < epoch(policy["ends_at"]):
                    self._skip(candidate["id"], "delivery_window_elapsed", db, candidate=True)
                    continue
                if (
                    a["disabled"]
                    or a["status"] != "healthy"
                    or a["cooldown_until"] > now
                    or a["next_at"] > now
                    or row["next_at"] > now
                ):
                    reason = (
                        "account_disabled"
                        if a["disabled"]
                        else "account_unhealthy"
                        if a["status"] != "healthy"
                        else "account_cooldown"
                        if a["cooldown_until"] > now
                        else "pacing_wait"
                    )
                    self._skip(candidate["id"], reason, db, candidate=True)
                    continue
                if (
                    db.execute(
                        "SELECT count(*) FROM join_attempts WHERE task_id=?", (task_id,)
                    ).fetchone()[0]
                    >= policy["max_joins"]
                ):
                    self._skip(candidate["id"], "join_limit", db, candidate=True)
                    break
                quota_reason = quotas.check_claim(
                    db, a, row, {"target": c["target"], "recipient_id": ""}, now, now, None
                )
                if quota_reason:
                    self._skip(candidate["id"], quota_reason, db, candidate=True)
                    continue
                attempt = "join-" + uuid.uuid4().hex
                key = digest(["telegram", c["target"].casefold()])
                # Prevent another task from attempting a target already attempted by this account.
                if db.execute(
                    "SELECT 1 FROM join_attempts WHERE account_id=? AND target_key=?",
                    (a["id"], key),
                ).fetchone():
                    self._skip(candidate["id"], "join_already_attempted", db, candidate=True)
                    continue
                db.execute(
                    "INSERT INTO join_attempts(id,candidate_id,account_id,task_id,target_key,user_key,mode,started_at) VALUES(?,?,?,?,?,?,?,?)",
                    (attempt, c["id"], a["id"], task_id, key, key, a["mode"], now),
                )
                db.execute("UPDATE candidates SET state='executing' WHERE id=?", (c["id"],))
                db.execute(
                    "UPDATE accounts SET next_at=? WHERE id=?",
                    (
                        now
                        + max(
                            policy["min_interval"],
                            a["min_interval"],
                            90 if a["mode"] == "live" else 0,
                        ),
                        a["id"],
                    ),
                )
                db.execute(
                    "UPDATE tasks SET next_at=? WHERE id=?",
                    (now + policy["min_interval"], task_id),
                )
                Store.audit(db, "join.claimed", {"candidate_id": c["id"]}, task_id, now)
            try:
                if (
                    self.task(task_id)["state"] != "active"
                    or self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"]
                    != "0"
                ):
                    result = {"status": "failed_known", "reason": "stopped_before_submission"}
                else:
                    result = self.call(
                        account,
                        "join",
                        {
                            "candidate_id": candidate["id"],
                            "target": candidate["target"],
                            "min_interval": policy["min_interval"],
                        },
                        task_id,
                    )
            except Conflict:
                result = {"status": "unknown", "reason": "transport_outcome_unknown"}
            status = result.get("status", "unknown")
            if status not in {
                "joined",
                "already_joined",
                "pending_admin_approval",
                "failed_known",
                "unknown",
            }:
                status = "unknown"
            if status in {"joined", "already_joined"} and not result.get("verified"):
                status = "unknown"
            if status in {"joined", "already_joined"} and account["mode"] == "simulation":
                result.setdefault(
                    "outbound_eligible_at_utc",
                    datetime.fromtimestamp(self.clock() + 3600, timezone.utc).isoformat(),
                )
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE candidates SET state=?,result=? WHERE id=?",
                    (status, canonical(result), candidate["id"]),
                )
                db.execute(
                    "UPDATE join_attempts SET outcome=?,finished_at=? WHERE id=?",
                    (status, self.clock(), attempt),
                )
                if result.get("rate_limited") or result.get("risk_warning"):
                    db.execute(
                        "UPDATE accounts SET cooldown_until=max(cooldown_until,?),disabled=max(disabled,?) WHERE id=?",
                        (
                            self.clock() + max(1, int(result.get("retry_after_seconds") or 3600)),
                            int(bool(result.get("risk_warning"))),
                            account["id"],
                        ),
                    )
                Store.audit(
                    db, "join." + status, {"candidate_id": candidate["id"]}, task_id, self.clock()
                )
            processed.append({"candidate_id": candidate["id"], "status": status})
        return {"processed": processed}

    def run_reference_job(self, job):
        """Execute one persisted reference through the existing publication safeguards."""
        if job["kind"] == "join":
            account = self.account(job["account_id"])
            with self.lanes.acquire(account["id"], self.execution_settings()):
                result = self._run_joins(job["task_id"], account["id"], job["reference_id"])
            task = self.task(job["task_id"])
            if task["state"] == "active" and task["phase"] == "starting":
                self.start_task(task["id"], source="scheduler")
            candidate = self.store.one(
                "SELECT target FROM candidates WHERE id=?", (job["reference_id"],)
            )
            if candidate:
                self._resume_membership_dependents(account["id"], candidate["target"])
            return result
        self.recover()
        row = self.store.one(
            "SELECT i.id,i.account_id,i.task_id,i.payload,i.due_at,t.policy,t.mode,t.next_at,t.state,t.phase "
            "FROM items i JOIN tasks t ON t.id=i.task_id WHERE i.id=? AND i.status='not_attempted' AND i.attempt_id IS NULL",
            (job["reference_id"],),
        )
        if not row:
            return {"processed": []}
        return {"processed": self._run_account_group([row], False, self.execution_settings())}

    def reconcile_join(self, candidate_id):
        row = self.store.one("SELECT * FROM candidates WHERE id=?", (candidate_id,))
        if not row:
            raise NotFound("候选不存在")
        if row["state"] not in {"unknown", "pending_admin_approval"}:
            raise Conflict("只有未知或待管理员批准的入群需要核对")
        result = self.call(
            self.account(row["account_id"]),
            "verify_join",
            {"candidate_id": candidate_id, "target": row["target"]},
            row["task_id"],
        )
        if result.get("verified") is not True or result.get("status") not in {
            "joined",
            "already_joined",
        }:
            return {"status": row["state"], "reason": "not_conclusively_verified"}
        # Membership lookup may confirm joining, but never grants another attempt.
        safe = {
            k: result[k]
            for k in (
                "status",
                "verified",
                "can_send",
                "outbound_eligible_at_utc",
                "external_reference",
            )
            if k in result
        }
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE candidates SET state=?,result=? WHERE id=? AND state IN ('unknown','pending_admin_approval')",
                (safe["status"], canonical(safe), candidate_id),
            )
            db.execute(
                "UPDATE join_attempts SET outcome=?,finished_at=? WHERE candidate_id=?",
                (safe["status"], self.clock(), candidate_id),
            )
            Store.audit(
                db,
                "join.reconciled",
                {"candidate_id": candidate_id, "status": safe["status"]},
                row["task_id"],
                self.clock(),
            )
        self._resume_membership_dependents(row["account_id"], row["target"])
        return safe

    def run_cycle(self):
        if self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0":
            return self.run_once()
        for row in self.store.rows("SELECT id,phase FROM tasks WHERE state='active'"):
            try:
                self.run_joins(row["id"])
                if row["phase"] == "starting":
                    self.start_task(row["id"], source="scheduler")
            except Conflict:
                pass
        return self.run_once()
