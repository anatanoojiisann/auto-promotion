from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .models import (
    CAPABILITIES,
    AccountInput,
    CandidateInput,
    ItemInput,
    TaskInput,
    canonical,
    digest,
    epoch,
)
from .store import Store, decode


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


class Engine:
    def __init__(
        self, store: Store, adapters, *, clock=time.time, live_enabled=False, media_root=None
    ):
        self.store = store
        self.adapters = adapters
        self.clock = clock
        self.live_enabled = live_enabled
        self.media_root = Path(media_root or Path(store.path).parent / "media").resolve()
        self.media_root.mkdir(parents=True, exist_ok=True)

    def account(self, account_id):
        row = self.store.one("SELECT * FROM accounts WHERE id=?", (account_id,))
        if not row:
            raise NotFound("账号不存在")
        value = decode(row, "health")
        value["capabilities"] = sorted(CAPABILITIES[value["platform"]])
        value["writable"] = bool(
            value["status"] == "healthy"
            and not value["disabled"]
            and value["cooldown_until"] <= self.clock()
            and (value["mode"] == "simulation" or self.live_enabled)
            and self.adapters.commissioned(value)
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

    def call(self, account, operation, payload=None, task_id=None):
        started = time.monotonic()
        try:
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
            raise Conflict("平台调用未完成；请查看 API 中心的错误类型并核验连接") from None
        self.store.log_call(account["platform"], operation, "ok", started, task_id)
        return result

    def check_account(self, account_id):
        account = self.account(account_id)
        try:
            result = self.call(account, "health")
        except Conflict:
            result = {"healthy": False, "reason": "connection_unavailable"}
        # Health results are an allowlist; raw provider responses are not persisted.
        health = {
            key: result[key]
            for key in ("healthy", "service_ready", "identity_verified", "permissions", "reason")
            if key in result
        }
        status = (
            "healthy"
            if health.get("healthy") and health.get("identity_verified")
            else "unavailable"
        )
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE accounts SET status=?,health=?,checked_at=? WHERE id=?",
                (status, canonical(health), self.clock(), account_id),
            )
            Store.audit(db, "account.checked", {"account_id": account_id, "status": status})
        return self.account(account_id)

    def disable_account(self, account_id, disabled):
        self.account(account_id)
        with self.store.connect(write=True) as db:
            db.execute("UPDATE accounts SET disabled=? WHERE id=?", (int(disabled), account_id))
            Store.audit(db, "account.disabled", {"account_id": account_id, "disabled": disabled})
        return self.account(account_id)

    def create_task(self, value: TaskInput):
        if len(set(value.account_ids)) != len(value.account_ids):
            raise Conflict("任务账号不能重复")
        accounts = [self.account(a) for a in value.account_ids]
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
        result["discovery_phrase"] = self.phrase(task_id, "discovery_join", result["policy_hash"])
        result["outbound_phrase"] = (
            self.phrase(task_id, "outbound", result["manifest_hash"])
            if result["manifest_hash"]
            else None
        )
        result["summary"] = {
            s: sum(i["status"] == s for i in result["items"])
            for s in {i["status"] for i in result["items"]}
        }
        return result

    @staticmethod
    def phrase(task_id, stage, hash_value):
        return f"确认 {task_id} {stage} {hash_value[:12]}"

    def _integrity(self, db, task, stage):
        policy = json.loads(task["policy"])
        snapshot = json.loads(task["account_snapshot"])
        if digest({"accounts": snapshot, "policy": policy}) != task["policy_hash"]:
            raise Conflict("发现政策已发生变化，请创建新任务")
        for saved in snapshot:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (saved["id"],)).fetchone()
            if row is None or any(row[k] != v for k, v in saved.items()):
                raise Conflict("账号配置已变化，请创建新任务")
        grant = db.execute(
            "SELECT hash FROM approvals WHERE task_id=? AND stage=?", (task["id"], stage)
        ).fetchone()
        expected = task["policy_hash"] if stage == "discovery_join" else task["manifest_hash"]
        if not grant or grant["hash"] != expected:
            raise Conflict("缺少有效的精确审批")
        if stage == "outbound":
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
                raise Conflict("执行项与已批准清单不一致")
        return policy

    def approve(self, task_id, stage, phrase, actor="operator"):
        if stage not in {"discovery_join", "outbound"}:
            raise Conflict("审批阶段无效")
        with self.store.connect(write=True) as db:
            task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise NotFound("任务不存在")
            if task["state"] in {"cancelled", "completed"}:
                raise Conflict("任务已结束")
            h = task["policy_hash"] if stage == "discovery_join" else task["manifest_hash"]
            if not h or phrase != self.phrase(task_id, stage, h):
                raise Conflict("请输入当前阶段显示的完整确认短语")
            previous = db.execute(
                "SELECT * FROM approvals WHERE task_id=? AND stage=?", (task_id, stage)
            ).fetchone()
            if previous:
                self._integrity(db, task, stage)
            else:
                if stage == "outbound":
                    self._integrity(db, task, "discovery_join")
                    if task["phase"] != "review":
                        raise Conflict("请先冻结并预览最终外发清单")
                db.execute(
                    "INSERT INTO approvals VALUES(?,?,?,?,?,?)",
                    (task_id, stage, h, phrase, actor, self.clock()),
                )
                self._integrity(db, task, stage)
                db.execute(
                    "UPDATE tasks SET state='active',phase=?,updated_at=? WHERE id=?",
                    ("ready" if stage == "outbound" else "discovered", self.clock(), task_id),
                )
                Store.audit(
                    db,
                    "approval.granted",
                    {"stage": stage, "hash": h, "actor": actor},
                    task_id,
                    self.clock(),
                )
        return self.task(task_id)

    def validate_item(self, task, value: ItemInput):
        if value.account_id not in task["account_ids"]:
            raise Conflict("账号不在该任务的批准范围内")
        account = self.account(value.account_id)
        if value.action not in CAPABILITIES[account["platform"]] or value.action in {
            "discover",
            "join",
        }:
            raise Conflict("该平台不支持此外发动作")
        policy = task["policy"]
        if not epoch(policy["starts_at"]) <= epoch(value.scheduled_at) < epoch(policy["ends_at"]):
            raise Conflict("计划时间超出任务执行窗口")
        if policy["target_allowlist"] and value.target not in policy["target_allowlist"]:
            raise Conflict("目标不在批准的目标边界内")
        if RISK.search(value.target):
            raise Conflict("目标包含被禁止的类别信号")
        if value.action not in {"like", "repost"} and not (value.text.strip() or value.media):
            raise Conflict("此动作需要正文或媒体")
        if value.action in {"like", "repost"} and (value.text or value.media or value.title):
            raise Conflict("点赞和转发不能附带正文或媒体")
        platform = account["platform"]
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
            if (
                value.action == "publish"
                and account["mode"] == "live"
                and account["backend_label"] != os.getenv("AP_INSTAGRAM_ACCOUNT")
            ):
                raise Conflict("Instagram 发布账号未在 AP_INSTAGRAM_ACCOUNT 中配置")
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
            if row["manifest_hash"] or row["state"] in {"cancelled", "completed"}:
                raise Conflict("清单已经冻结；变更内容请创建新任务")
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
            if not row["manifest_hash"]:
                # Read items inside this transaction, so a concurrent edit cannot race the freeze.
                items = [
                    json.loads(r["payload"])
                    for r in db.execute(
                        "SELECT payload FROM items WHERE task_id=? ORDER BY position", (task_id,)
                    )
                ]
                manifest = {
                    "task_id": task_id,
                    "accounts": json.loads(row["account_snapshot"]),
                    "policy": json.loads(row["policy"]),
                    "items": items,
                }
                h = digest(manifest)
                db.execute(
                    "UPDATE tasks SET manifest=?,manifest_hash=?,phase='review',updated_at=? WHERE id=?",
                    (canonical(manifest), h, self.clock(), task_id),
                )
                Store.audit(
                    db, "manifest.frozen", {"hash": h, "count": len(items)}, task_id, self.clock()
                )
        return self.task(task_id)

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
        return {
            "global_stop": stopped,
            "native_schedule_note": "已提交的平台排期不受本地停止开关撤回",
        }

    def recover(self, lease_seconds=300):
        with self.store.connect(write=True) as db:
            rows = db.execute(
                "SELECT id,task_id,attempt_id FROM items WHERE status='executing' AND claimed_at<?",
                (self.clock() - lease_seconds,),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE items SET status='unknown',result=? WHERE id=?",
                    (canonical({"reason": "worker_lease_expired"}), row["id"]),
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

    def _claim(self, item_id, backend_gate=None):
        now = self.clock()
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if not row or row["status"] != "not_attempted":
                return None
            task = db.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
            if task["state"] != "active" or task["phase"] != "ready":
                return None
            policy = self._integrity(db, task, "outbound")
            if db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()[0] != "0":
                return None
            payload = json.loads(row["payload"])
            account = db.execute(
                "SELECT * FROM accounts WHERE id=?", (row["account_id"],)
            ).fetchone()
            if (
                account["disabled"]
                or account["status"] != "healthy"
                or account["cooldown_until"] > now
                or account["next_at"] > now
            ):
                return None
            if account["mode"] == "live" and (
                not self.live_enabled
                or not self.adapters.commissioned(account)
                or not backend_gate
                or backend_gate.get("allowed") is not True
            ):
                return None
            # Native schedules are submitted ahead of delivery; other actions are claimed when due.
            native = payload["action"] in {"schedule_message", "schedule_file"}
            if now < epoch(policy["starts_at"]) or (not native and row["due_at"] > now):
                return None
            if (
                now >= epoch(policy["ends_at"])
                or now - row["due_at"] > 900
                or (native and row["due_at"] <= now)
            ):
                db.execute(
                    "UPDATE items SET status='missed',result=? WHERE id=?",
                    (canonical({"reason": "delivery_window_elapsed"}), item_id),
                )
                return None
            earlier = db.execute(
                "SELECT status FROM items WHERE task_id=? AND position<?",
                (task["id"], row["position"]),
            ).fetchall()
            if any(r["status"] not in TERMINAL for r in earlier):
                return None
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
                    return None
            limits = [
                ("mode=? AND started_at>=?", (mode, day), 100),
                ("mode=? AND started_at>=?", (mode, now - 3600), 20),
                ("account_id=? AND started_at>=?", (account["id"], day), account["daily_limit"]),
                (
                    "account_id=? AND started_at>=?",
                    (account["id"], now - 3600),
                    account["hourly_limit"],
                ),
                ("task_id=? AND started_at>=?", (task["id"], day), policy["daily_limit"]),
                (
                    "mode=? AND target_key=? AND started_at>=?",
                    (mode, target_key, day),
                    policy["target_daily_limit"],
                ),
                (
                    "mode=? AND user_key=? AND started_at>=?",
                    (mode, user_key, day),
                    policy["user_daily_limit"],
                ),
            ]
            for clause, params, limit in limits:
                if (
                    db.execute("SELECT count(*) FROM activity WHERE " + clause, params).fetchone()[
                        0
                    ]
                    >= limit
                ):
                    return None
            # Reserve delivery-day capacity too: multiple days of native schedule
            # submissions must not concentrate beyond one day's approved limits.
            delivery_day = row["due_at"] - row["due_at"] % 86400
            reserved = db.execute(
                "SELECT i.account_id,i.task_id,i.payload FROM items i JOIN accounts a ON a.id=i.account_id WHERE i.attempt_id IS NOT NULL AND a.mode=? AND i.due_at>=? AND i.due_at<?",
                (mode, delivery_day, delivery_day + 86400),
            ).fetchall()
            if (
                len(reserved) >= 100
                or sum(r["account_id"] == account["id"] for r in reserved) >= account["daily_limit"]
                or sum(r["task_id"] == task["id"] for r in reserved) >= policy["daily_limit"]
            ):
                return None
            reserved_payloads = (json.loads(r["payload"]) for r in reserved)
            same_platform = [p for p in reserved_payloads if p["platform"] == account["platform"]]
            if (
                sum(p["target"].casefold() == payload["target"].casefold() for p in same_platform)
                >= policy["target_daily_limit"]
            ):
                return None
            recipient = payload["recipient_id"] or payload["target"].casefold()
            if (
                sum(
                    (p["recipient_id"] or p["target"].casefold()) == recipient
                    for p in same_platform
                )
                >= policy["user_daily_limit"]
            ):
                return None
            for media in payload["media"]:
                self.verify_media(media)
            membership = db.execute(
                "SELECT state,result FROM candidates WHERE account_id=? AND target=? ORDER BY rowid DESC LIMIT 1",
                (account["id"], payload["target"]),
            ).fetchone()
            if membership:
                joined = json.loads(membership["result"])
                if membership["state"] in {"pending_admin_approval", "unknown", "executing"}:
                    return None
                if joined.get("can_send") is False:
                    return None
                if (
                    joined.get("outbound_eligible_at_utc")
                    and epoch(joined["outbound_eligible_at_utc"]) > now
                ):
                    return None
            attempt = "attempt-" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO attempts(id,item_id,account_id,task_id,target_key,user_key,mode,started_at) VALUES(?,?,?,?,?,?,?,?)",
                (attempt, item_id, account["id"], task["id"], target_key, user_key, mode, now),
            )
            db.execute(
                "UPDATE items SET status='executing',attempt_id=?,claimed_at=? WHERE id=?",
                (attempt, now, item_id),
            )
            db.execute(
                "UPDATE accounts SET next_at=? WHERE id=?",
                (now + max(account["min_interval"], policy["min_interval"]), account["id"]),
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

    def run_once(self, task_id=None):
        self.recover()
        if task_id:
            self.task(task_id)
        if self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0":
            return {"processed": []}
        sql = "SELECT i.id,i.account_id,i.task_id,i.payload,i.due_at,t.policy,t.mode FROM items i JOIN tasks t ON t.id=i.task_id WHERE i.status='not_attempted' AND t.state='active' AND t.phase='ready'"
        params = ()
        if task_id:
            sql += " AND i.task_id=?"
            params = (task_id,)
        rows = self.store.rows(sql + " ORDER BY due_at,i.position", params)
        processed = []
        checked = set()
        for row in rows:
            if row["mode"] == "live" and not self.live_enabled:
                continue
            policy = json.loads(row["policy"])
            native = json.loads(row["payload"])["action"] in {"schedule_message", "schedule_file"}
            if (
                self.clock() >= epoch(policy["ends_at"])
                or self.clock() - row["due_at"] > 900
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
                continue
            account = self.account(row["account_id"])
            if not self.adapters.commissioned(account):
                continue
            if account["id"] not in checked:
                # A fresh read-only health check before a live claim; simulation needs one too.
                self.check_account(account["id"])
                checked.add(account["id"])
            try:
                gate = (
                    self.call(
                        account,
                        "preflight",
                        {"payload": json.loads(row["payload"])},
                        row["task_id"],
                    )
                    if account["mode"] == "live"
                    else None
                )
                if gate and gate.get("allowed") is not True:
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
                processed.append({"item_id": row["id"], "status": "blocked", "reason": str(exc)})
                continue
            if not claim:
                continue
            account = self.account(row["account_id"])
            if claim["payload"]["action"] == "manual_comment":
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
            )
            if k in result
        }
        safe["status"] = status
        now = self.clock()
        with self.store.connect(write=True) as db:
            row = db.execute(
                "SELECT * FROM items WHERE id=? AND attempt_id=?",
                (claim["item_id"], claim["attempt_id"]),
            ).fetchone()
            if not row:
                raise Conflict("执行所有者不匹配")
            if row["status"] not in {"executing", "unknown"}:
                return json.loads(row["result"])
            db.execute(
                "UPDATE items SET status=?,external_reference=?,result=? WHERE id=?",
                (status, reference, canonical(safe), claim["item_id"]),
            )
            db.execute(
                "UPDATE attempts SET outcome=?,finished_at=? WHERE id=?",
                (status, now, claim["attempt_id"]),
            )
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
                {"item_id": claim["item_id"], "external_reference": reference},
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
        result = self.call(self.account(row["account_id"]), "verify", payload, row["task_id"])
        if not result.get("verified"):
            return {"status": row["status"], "reason": "not_conclusively_verified"}
        # Reconciliation never turns an item back into a retryable state.
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE items SET status='unknown' WHERE id=? AND status='scheduled'", (item_id,)
            )
        return self.finish(payload, result)

    def manual_outcome(self, item_id, value):
        row = self.store.one("SELECT * FROM items WHERE id=?", (item_id,))
        if not row or row["status"] != "manual_attention":
            raise Conflict("该项目不在人工处理状态")
        payload = json.loads(row["payload"])
        if payload["action"] != "manual_comment":
            raise Conflict("该动作不支持人工登记")
        exact = value.observed_text == payload["text"]
        status = (
            "confirmed"
            if exact
            and value.verified
            and value.external_reference.startswith("https://www.instagram.com/")
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
        content = (value.title + " " + value.text + " " + value.target).casefold()
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
            if row["state"] != "active":
                raise Conflict("任务未激活")
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
                    canonical(value.model_dump()),
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
            if row["state"] != "active":
                raise Conflict("任务未激活")
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
        self.recover()
        task = self.task(task_id)
        processed = []
        if not task["policy"]["max_joins"]:
            return {"processed": []}
        for candidate in task["candidates"]:
            if candidate["decision"] != "qualified" or candidate["state"] != "not_attempted":
                continue
            account = self.account(candidate["account_id"])
            if (
                account["platform"] != "telegram"
                or not self.adapters.commissioned(account)
                or (account["mode"] == "live" and not self.live_enabled)
            ):
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
                    continue
                if (
                    db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()[0]
                    != "0"
                ):
                    break
                if not epoch(policy["starts_at"]) <= now < epoch(policy["ends_at"]):
                    continue
                if (
                    a["disabled"]
                    or a["status"] != "healthy"
                    or a["cooldown_until"] > now
                    or a["next_at"] > now
                ):
                    continue
                if (
                    db.execute(
                        "SELECT count(*) FROM join_attempts WHERE task_id=?", (task_id,)
                    ).fetchone()[0]
                    >= policy["max_joins"]
                ):
                    break
                day = now - now % 86400
                if (
                    db.execute(
                        "SELECT count(*) FROM activity WHERE account_id=? AND started_at>=?",
                        (a["id"], day),
                    ).fetchone()[0]
                    >= a["daily_limit"]
                ):
                    continue
                if (
                    db.execute(
                        "SELECT count(*) FROM activity WHERE mode=? AND started_at>=?",
                        (a["mode"], day),
                    ).fetchone()[0]
                    >= 100
                ):
                    continue
                if (
                    db.execute(
                        "SELECT count(*) FROM activity WHERE mode=? AND started_at>=?",
                        (a["mode"], now - 3600),
                    ).fetchone()[0]
                    >= 20
                ):
                    continue
                if (
                    db.execute(
                        "SELECT count(*) FROM activity WHERE account_id=? AND started_at>=?",
                        (a["id"], now - 3600),
                    ).fetchone()[0]
                    >= a["hourly_limit"]
                ):
                    continue
                if (
                    db.execute(
                        "SELECT count(*) FROM activity WHERE task_id=? AND started_at>=?",
                        (task_id, day),
                    ).fetchone()[0]
                    >= policy["daily_limit"]
                ):
                    continue
                attempt = "join-" + uuid.uuid4().hex
                key = digest(["telegram", c["target"].casefold()])
                # Prevent another task from attempting a target already attempted by this account.
                if db.execute(
                    "SELECT 1 FROM join_attempts WHERE account_id=? AND target_key=?",
                    (a["id"], key),
                ).fetchone():
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
        return safe

    def run_cycle(self):
        if self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"] != "0":
            return {"processed": []}
        for row in self.store.rows(
            "SELECT t.id FROM tasks t JOIN approvals a ON a.task_id=t.id AND a.stage='discovery_join' WHERE t.state='active'"
        ):
            try:
                self.run_joins(row["id"])
            except Conflict:
                pass
        return self.run_once()
