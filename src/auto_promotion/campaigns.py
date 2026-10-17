"""Shared opportunities and campaign plans. Planning never starts execution."""

import json
import sqlite3
import uuid
from collections import defaultdict

from .core import Conflict, NotFound
from .models import CandidateInput, ItemInput, OpportunityInput, TaskInput, canonical, digest
from .planner import build_plan, content_key
from .pools import Pools
from .quotas import UNSUBMITTED_MANUAL, settings
from .store import Store, decode


class Campaigns:
    def __init__(self, engine):
        self.engine, self.store, self.clock = engine, engine.store, engine.clock
        self.pools = Pools(engine)

    def import_content(self, values, *, preserve_review=False):
        imported, updated, duplicates = [], 0, 0
        with self.store.connect(write=True) as db:
            for value in values:
                payload = value.model_dump()
                key = content_key(payload)
                row = db.execute(
                    "SELECT * FROM opportunities WHERE platform=? AND content_key=?",
                    (value.platform, key),
                ).fetchone()
                if row:
                    identifier = row["id"]
                    previous = json.loads(row["payload"])
                    if preserve_review:
                        for field in (
                            "permission",
                            "permission_evidence",
                            "permission_account_ids",
                            "expires_at",
                            "draft_text",
                            "draft_title",
                            "topics",
                            "audience",
                        ):
                            payload[field] = previous[field]
                        if previous["context"] != payload["context"]:
                            payload["draft_text"] = ""
                            payload["draft_title"] = ""
                    if previous == payload:
                        duplicates += 1
                    else:
                        db.execute(
                            "UPDATE opportunities SET payload=?,revision=revision+1,updated_at=? WHERE id=?",
                            (canonical(payload), self.clock(), identifier),
                        )
                        updated += 1
                else:
                    identifier = "opp-" + uuid.uuid4().hex[:16]
                    db.execute(
                        "INSERT INTO opportunities VALUES(?,?,?,?,1,?,?)",
                        (
                            identifier,
                            value.platform,
                            key,
                            canonical(payload),
                            self.clock(),
                            self.clock(),
                        ),
                    )
                imported.append(identifier)
            Store.audit(
                db,
                "content.imported",
                {"count": len(values), "updated": updated, "duplicates": duplicates},
                now=self.clock(),
            )
        return {
            "ids": imported,
            "imported": len(values) - updated - duplicates,
            "updated": updated,
            "duplicates": duplicates,
        }

    def collect_telegram_content(self, account_id, target, limit=200):
        from .core import RISK

        account = self.engine.account(account_id)
        if (
            account["platform"] != "telegram"
            or account["disabled"]
            or account["cooldown_until"] > self.clock()
        ):
            raise Conflict("请选择可用且不在冷却中的 Telegram 账号")
        if not 1 <= limit <= 1000 or RISK.search(target):
            raise Conflict("读取范围无效")
        result = self.engine.call(account, "content", {"target": target, "limit": limit})
        self.engine.apply_cooldown(account_id, result)
        if result.get("rate_limited") or result.get("healthy") is False:
            raise Conflict("读取未完成；请核对账号或等待冷却")
        values = [OpportunityInput.model_validate(v) for v in result.get("content_items", [])]
        imported = (
            self.import_content(values, preserve_review=True)
            if values
            else {"ids": [], "imported": 0, "updated": 0, "duplicates": 0}
        )
        return {**imported, "truncated": bool(result.get("truncated"))}

    def content(self, *, offset=0, limit=100, platform=None):
        self._page(offset, limit)
        where, args = (" WHERE platform=?", (platform,)) if platform else ("", ())
        return {
            "total": self.store.one("SELECT count(*) n FROM opportunities" + where, args)["n"],
            "offset": offset,
            "items": [
                decode(r, "payload")
                for r in self.store.rows(
                    "SELECT * FROM opportunities"
                    + where
                    + " ORDER BY created_at DESC,id LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                )
            ],
        }

    @staticmethod
    def _page(offset, limit):
        if offset < 0 or not 1 <= limit <= 500:
            raise Conflict("分页范围无效")

    def create(self, value):
        pool = self.pools.get(value.pool_id)
        if not pool["account_ids"]:
            raise Conflict("号池没有账号")
        if len(set(value.opportunity_ids)) != len(value.opportunity_ids):
            raise Conflict("活动内容不能重复")
        if len({a["mode"] for a in pool["accounts"]}) != 1:
            raise Conflict("真实和模拟账号不能用于同一活动")
        if pool["accounts"][0]["mode"] == "live" and value.policy.min_interval < 120:
            raise Conflict("真实活动最小账号间隔为 120 秒")
        identifier = "campaign-" + uuid.uuid4().hex[:12]
        with self.store.connect(write=True) as db:
            for opportunity_id in value.opportunity_ids:
                if not db.execute(
                    "SELECT 1 FROM opportunities WHERE id=?", (opportunity_id,)
                ).fetchone():
                    raise Conflict("活动包含不存在的内容")
            db.execute(
                "INSERT INTO campaigns(id,name,pool_id,config,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (
                    identifier,
                    value.name,
                    value.pool_id,
                    canonical(value.model_dump()),
                    self.clock(),
                    self.clock(),
                ),
            )
            Store.audit(
                db,
                "campaign.created",
                {"campaign_id": identifier, "contents": len(value.opportunity_ids)},
                now=self.clock(),
            )
        return self.get(identifier)

    def raw(self, campaign_id):
        row = self.store.one("SELECT * FROM campaigns WHERE id=?", (campaign_id,))
        if not row:
            raise NotFound("活动不存在")
        return decode(row, "config", "plan_snapshot")

    def list(self):
        return [
            {
                k: v
                for k, v in self.get(r["id"], limit=1).items()
                if k not in {"assignments", "config", "plan_snapshot"}
            }
            for r in self.store.rows("SELECT id FROM campaigns ORDER BY created_at DESC LIMIT 200")
        ]

    def get(self, campaign_id, *, offset=0, limit=100):
        self._page(offset, limit)
        result = self.raw(campaign_id)
        result["assignments"] = [
            decode(r, "reasons", "payload", "profile_snapshot")
            for r in self.store.rows(
                "SELECT ca.*,o.platform,o.payload source_payload,i.status item_status,i.external_reference FROM campaign_assignments ca JOIN opportunities o ON o.id=ca.opportunity_id LEFT JOIN items i ON i.id=ca.item_id WHERE ca.campaign_id=? ORDER BY ca.rowid LIMIT ? OFFSET ?",
                (campaign_id, limit, offset),
            )
        ]
        for assignment in result["assignments"]:
            assignment["source_payload"] = json.loads(assignment["source_payload"])
        result["assignment_total"] = self.store.one(
            "SELECT count(*) n FROM campaign_assignments WHERE campaign_id=?", (campaign_id,)
        )["n"]
        result["summary"] = {
            r["status"]: r["n"]
            for r in self.store.rows(
                "SELECT status,count(*) n FROM campaign_assignments WHERE campaign_id=? GROUP BY status",
                (campaign_id,),
            )
        }
        result["execution_summary"] = {
            r["status"]: r["n"]
            for r in self.store.rows(
                "SELECT i.status,count(*) n FROM items i JOIN tasks t ON t.id=i.task_id WHERE t.campaign_id=? GROUP BY i.status",
                (campaign_id,),
            )
        }
        result["accounts"] = self.store.rows(
            "SELECT ca.account_id,a.label,a.platform,count(*) planned,sum(CASE WHEN i.status='confirmed' THEN 1 ELSE 0 END) confirmed FROM campaign_assignments ca JOIN accounts a ON a.id=ca.account_id LEFT JOIN items i ON i.id=ca.item_id WHERE ca.campaign_id=? GROUP BY ca.account_id",
            (campaign_id,),
        )
        for account in result["accounts"]:
            assignments = self.store.rows(
                "SELECT ca.profile_snapshot,ca.payload,i.status,i.id,c.status coverage,c.checked_through FROM campaign_assignments ca LEFT JOIN items i ON i.id=ca.item_id LEFT JOIN feedback_checks c ON c.item_id=i.id WHERE ca.campaign_id=? AND ca.account_id=?",
                (campaign_id, account["account_id"]),
            )
            profile = json.loads(assignments[0]["profile_snapshot"])
            account.update(role=profile["payload"]["role"], profile_version=profile["version"])
            account["observed_replies"] = self.store.one(
                "SELECT count(*) n FROM feedback_events f JOIN campaign_assignments ca ON ca.item_id=f.item_id WHERE ca.campaign_id=? AND ca.account_id=?",
                (campaign_id, account["account_id"]),
            )["n"]
            confirmed = [
                a
                for a in assignments
                if a["status"] == "confirmed"
                and json.loads(a["payload"])["action"] not in {"like", "repost"}
            ]
            covered = bool(confirmed) and all(
                a["coverage"] == "complete" and a["checked_through"] >= self.clock()
                for a in confirmed
            )
            responded = self.store.one(
                "SELECT count(DISTINCT f.item_id) n FROM feedback_events f JOIN campaign_assignments ca ON ca.item_id=f.item_id WHERE ca.campaign_id=? AND ca.account_id=? AND json_extract(ca.payload,'$.action') NOT IN ('like','repost')",
                (campaign_id, account["account_id"]),
            )["n"]
            account["reply_rate"] = responded / len(confirmed) if covered else None
        result["tasks"] = self.store.rows(
            "SELECT id,name,state,phase,policy_hash,manifest_hash FROM tasks WHERE campaign_id=? ORDER BY created_at,id",
            (campaign_id,),
        )
        result["offset"] = offset
        return result

    def _snapshot(self, db, campaign):
        pool = db.execute(
            "SELECT * FROM account_pools WHERE id=?", (campaign["pool_id"],)
        ).fetchone()
        members = [
            r[0]
            for r in db.execute(
                "SELECT account_id FROM pool_members WHERE pool_id=? ORDER BY account_id",
                (campaign["pool_id"],),
            )
        ]
        accounts = []
        for identifier in members:
            row = db.execute("SELECT * FROM accounts WHERE id=?", (identifier,)).fetchone()
            profile = db.execute(
                "SELECT version FROM account_profiles WHERE account_id=?", (identifier,)
            ).fetchone()
            accounts.append(
                {
                    k: row[k]
                    for k in (
                        "id",
                        "platform",
                        "mode",
                        "label",
                        "backend_label",
                        "identity_hash",
                        "daily_limit",
                        "hourly_limit",
                        "min_interval",
                        "disabled",
                        "status",
                        "cooldown_until",
                    )
                }
                | {"profile_version": profile[0] if profile else 0}
            )
        opportunities = []
        for identifier in campaign["config"]["opportunity_ids"]:
            row = db.execute(
                "SELECT revision FROM opportunities WHERE id=?", (identifier,)
            ).fetchone()
            opportunities.append([identifier, row[0]])
        return {
            "pool_revision": pool["revision"],
            "accounts": accounts,
            "opportunities": opportunities,
            "settings": settings(db),
        }

    def plan(self, campaign_id, expected_revision=None):
        campaign = self.raw(campaign_id)
        if campaign["state"] not in {"draft", "planned"}:
            raise Conflict("活动已生成执行任务，不能重新分配；变更请创建新活动")
        if expected_revision is not None and expected_revision != campaign["revision"]:
            raise Conflict("活动已变化，请刷新")
        pool = self.pools.get(campaign["pool_id"])
        profiles = {a: self.pools.profile(a) for a in pool["account_ids"]}
        opportunities = [
            decode(
                self.store.one("SELECT * FROM opportunities WHERE id=?", (identifier,)), "payload"
            )
            for identifier in campaign["config"]["opportunity_ids"]
        ]
        with self.store.connect() as db:
            fingerprint = self._snapshot(db, campaign)
        assignments, quotas = build_plan(self.engine, campaign, pool, opportunities, profiles)
        plan_snapshot = {"fingerprint": fingerprint, "quotas": quotas}
        plan_hash = digest({"snapshot": plan_snapshot, "assignments": assignments})
        with self.store.connect(write=True) as db:
            current = db.execute(
                "SELECT state,revision FROM campaigns WHERE id=?", (campaign_id,)
            ).fetchone()
            if (
                current["state"] not in {"draft", "planned"}
                or current["revision"] != campaign["revision"]
                or self._snapshot(db, campaign) != fingerprint
            ):
                raise Conflict("规划期间账号、内容或活动发生变化，请重新规划")
            db.execute("DELETE FROM campaign_assignments WHERE campaign_id=?", (campaign_id,))
            for item in assignments:
                db.execute(
                    "INSERT INTO campaign_assignments(id,campaign_id,opportunity_id,account_id,score,reasons,payload,profile_snapshot,status) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "assignment-" + uuid.uuid4().hex[:16],
                        campaign_id,
                        item["opportunity_id"],
                        item["account_id"],
                        item["score"],
                        canonical(item["reasons"]),
                        canonical(item["payload"]) if item["payload"] else None,
                        canonical(item["profile_snapshot"]) if item["profile_snapshot"] else None,
                        item["status"],
                    ),
                )
            db.execute(
                "UPDATE campaigns SET state='planned',revision=revision+1,plan_hash=?,plan_snapshot=?,updated_at=? WHERE id=?",
                (plan_hash, canonical(plan_snapshot), self.clock(), campaign_id),
            )
            Store.audit(
                db,
                "campaign.planned",
                {"campaign_id": campaign_id, "hash": plan_hash},
                now=self.clock(),
            )
        return self.get(campaign_id)

    def edit_assignment(self, assignment_id, value):
        row = self.store.one("SELECT * FROM campaign_assignments WHERE id=?", (assignment_id,))
        if not row or row["status"] != "planned":
            raise Conflict("此项目没有可编辑草稿")
        campaign = self.raw(row["campaign_id"])
        payload = json.loads(row["payload"])
        payload.update(text=value.text, title=value.title)
        self.engine.validate_item(
            {
                "id": "planning",
                "policy": campaign["config"]["policy"],
                "account_ids": [row["account_id"]],
            },
            ItemInput(**payload),
        )
        with self.store.connect(write=True) as db:
            if (
                db.execute("SELECT state FROM campaigns WHERE id=?", (campaign["id"],)).fetchone()[
                    0
                ]
                != "planned"
            ):
                raise Conflict("已生成执行批次，草稿不可修改")
            db.execute(
                "UPDATE campaign_assignments SET payload=? WHERE id=?",
                (canonical(payload), assignment_id),
            )
            db.execute(
                "UPDATE campaigns SET revision=revision+1,updated_at=? WHERE id=?",
                (self.clock(), campaign["id"]),
            )
            self._refresh_hash(db, campaign["id"])
        return self.get(campaign["id"])

    def generate_drafts(self, campaign_id, assignment_ids, provider=None):
        from .drafts import DraftService

        campaign = self.raw(campaign_id)
        if campaign["state"] != "planned":
            raise Conflict("AI 草稿只能在生成执行批次前更新")
        if len(set(assignment_ids)) != len(assignment_ids):
            raise Conflict("草稿项目不能重复")
        provider = provider or DraftService()
        changes = []
        for identifier in assignment_ids:
            row = self.store.one(
                "SELECT ca.*,o.payload source FROM campaign_assignments ca JOIN opportunities o ON o.id=ca.opportunity_id WHERE ca.id=? AND ca.campaign_id=? AND ca.status='planned'",
                (identifier, campaign_id),
            )
            if not row:
                raise Conflict("草稿项目不在此活动的已分配范围")
            payload = json.loads(row["payload"])
            if payload["action"] in {"like", "repost"}:
                raise Conflict("此动作没有可生成的正文")
            generated = provider.generate(
                json.loads(row["profile_snapshot"])["payload"], json.loads(row["source"])
            )
            payload.update(text=generated.text, title=generated.title or payload["title"])
            self.engine.validate_item(
                {
                    "id": "planning",
                    "policy": campaign["config"]["policy"],
                    "account_ids": [row["account_id"]],
                },
                ItemInput(**payload),
            )
            changes.append((canonical(payload), identifier))
        with self.store.connect(write=True) as db:
            current = db.execute(
                "SELECT state,revision FROM campaigns WHERE id=?", (campaign_id,)
            ).fetchone()
            if (
                current["state"] != "planned"
                or current["revision"] != campaign["revision"]
                or self._snapshot(db, campaign) != campaign["plan_snapshot"]["fingerprint"]
            ):
                raise Conflict("生成期间活动或账号资料发生变化，草稿未保存，请重新规划")
            db.executemany("UPDATE campaign_assignments SET payload=? WHERE id=?", changes)
            db.execute(
                "UPDATE campaigns SET revision=revision+1,updated_at=? WHERE id=?",
                (self.clock(), campaign_id),
            )
            self._refresh_hash(db, campaign_id)
            Store.audit(
                db,
                "campaign.drafts_generated",
                {"campaign_id": campaign_id, "count": len(changes)},
                now=self.clock(),
            )
        return self.get(campaign_id)

    @staticmethod
    def _plan_items(db, campaign_id):
        return [
            {
                k: (json.loads(r[k]) if r[k] else None)
                if k in {"reasons", "payload", "profile_snapshot"}
                else r[k]
                for k in (
                    "opportunity_id",
                    "account_id",
                    "score",
                    "reasons",
                    "payload",
                    "profile_snapshot",
                    "status",
                )
            }
            for r in db.execute(
                "SELECT * FROM campaign_assignments WHERE campaign_id=? ORDER BY rowid",
                (campaign_id,),
            )
        ]

    def _refresh_hash(self, db, campaign_id):
        snapshot = json.loads(
            db.execute("SELECT plan_snapshot FROM campaigns WHERE id=?", (campaign_id,)).fetchone()[
                0
            ]
        )
        db.execute(
            "UPDATE campaigns SET plan_hash=? WHERE id=?",
            (
                digest({"snapshot": snapshot, "assignments": self._plan_items(db, campaign_id)}),
                campaign_id,
            ),
        )

    def materialize(self, campaign_id):
        campaign = self.raw(campaign_id)
        if campaign["state"] in {"materialized", "paused", "completed"}:
            return self.get(campaign_id)
        if campaign["state"] != "planned":
            raise Conflict("请先生成分配预览")
        with self.store.connect(write=True) as db:
            current = db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if current["state"] in {"materialized", "paused", "completed"}:
                pass
            elif current["state"] != "planned" or current["revision"] != campaign["revision"]:
                raise Conflict("活动已变化，请刷新")
            else:
                if self._snapshot(db, campaign) != campaign["plan_snapshot"]["fingerprint"]:
                    raise Conflict("账号、人设、内容或额度已变化，请重新规划")
                if (
                    digest(
                        {
                            "snapshot": campaign["plan_snapshot"],
                            "assignments": self._plan_items(db, campaign_id),
                        }
                    )
                    != current["plan_hash"]
                ):
                    raise Conflict("分配计划完整性校验失败")
                groups = defaultdict(list)
                for row in db.execute(
                    "SELECT * FROM campaign_assignments WHERE campaign_id=? AND status='planned' ORDER BY rowid",
                    (campaign_id,),
                ):
                    groups[row["account_id"]].append(dict(row))
                if not groups:
                    raise Conflict("没有可生成任务的分配项目")
                for account_id, assignments in groups.items():
                    account = dict(
                        db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
                    )
                    for index in range(0, len(assignments), campaign["config"]["batch_size"]):
                        batch = assignments[index : index + campaign["config"]["batch_size"]]
                        policy = {
                            **campaign["config"]["policy"],
                            "pacing_scope": "account",
                            "ordering_mode": "independent_accounts",
                            "quota_snapshot": campaign["plan_snapshot"]["quotas"],
                            "target_allowlist": sorted(
                                {json.loads(a["payload"])["target"] for a in batch}
                            ),
                            "max_candidates": len(batch),
                        }
                        value = TaskInput(
                            name=campaign["name"]
                            + " · "
                            + account["label"]
                            + f" · {index // campaign['config']['batch_size'] + 1}",
                            account_ids=[account_id],
                            policy=policy,
                        )
                        snapshot = [
                            {
                                k: account[k]
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
                        ]
                        task_id = "AP-" + uuid.uuid4().hex[:14].upper()
                        db.execute(
                            "INSERT INTO tasks(id,name,mode,account_ids,account_snapshot,policy,policy_hash,created_at,updated_at,campaign_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (
                                task_id,
                                value.name,
                                account["mode"],
                                canonical([account_id]),
                                canonical(snapshot),
                                canonical(value.policy.model_dump()),
                                digest({"accounts": snapshot, "policy": value.policy.model_dump()}),
                                self.clock(),
                                self.clock(),
                                campaign_id,
                            ),
                        )
                        for assignment in batch:
                            payload = json.loads(assignment["payload"])
                            try:
                                db.execute(
                                    "INSERT INTO content_contacts VALUES(?,?,?,?)",
                                    (
                                        account["mode"],
                                        account["platform"],
                                        payload["content_key"],
                                        assignment["id"],
                                    ),
                                )
                            except sqlite3.IntegrityError:
                                raise Conflict("内容已被其他活动保留，请重新规划") from None
                            db.execute(
                                "UPDATE campaign_assignments SET task_id=?,status='batched' WHERE id=?",
                                (task_id, assignment["id"]),
                            )
                        Store.audit(
                            db,
                            "task.created",
                            {"campaign_id": campaign_id, "planned_items": len(batch)},
                            task_id,
                            self.clock(),
                        )
                db.execute(
                    "UPDATE campaigns SET state='materialized',revision=revision+1,updated_at=? WHERE id=?",
                    (self.clock(), campaign_id),
                )
                Store.audit(
                    db, "campaign.materialized", {"campaign_id": campaign_id}, now=self.clock()
                )
        for task in self.store.rows("SELECT id FROM tasks WHERE campaign_id=?", (campaign_id,)):
            self.populate(task["id"])
        return self.get(campaign_id)

    def populate(self, task_id):
        task = self.engine.task(task_id)
        if not task.get("campaign_id"):
            raise Conflict("此任务不是活动批次")
        if task["items"]:
            return task
        assignments = self.store.rows(
            "SELECT ca.*,o.payload source FROM campaign_assignments ca JOIN opportunities o ON o.id=ca.opportunity_id WHERE ca.task_id=? ORDER BY ca.rowid",
            (task_id,),
        )
        for assignment in assignments:
            source = json.loads(assignment["source"])
            self.engine.candidate(
                task_id,
                CandidateInput(
                    account_id=assignment["account_id"],
                    target=source["target"],
                    title=source["title"],
                    text=source["context"],
                    language=source["language"],
                    confidence=source["confidence"],
                    members=source["members"],
                    activity=source["activity"],
                    is_public=source["is_public"],
                ),
            )
        values = sorted(
            [ItemInput(**json.loads(a["payload"])) for a in assignments],
            key=lambda v: v.scheduled_at,
        )
        return self.engine.set_items(task_id, values)

    def change_state(self, campaign_id, state):
        campaign = self.raw(campaign_id)
        tasks = self.store.rows("SELECT id,state FROM tasks WHERE campaign_id=?", (campaign_id,))
        if state == "active" and not tasks:
            raise Conflict("请先生成执行批次")
        if campaign["state"] in {"cancelled", "completed"}:
            raise Conflict("已结束活动不能恢复")
        with self.store.connect(write=True) as db:
            current = db.execute(
                "SELECT state FROM campaigns WHERE id=?", (campaign_id,)
            ).fetchone()
            if current[0] in {"cancelled", "completed"}:
                raise Conflict("已结束活动不能恢复")
            children = db.execute(
                "SELECT * FROM tasks WHERE campaign_id=?", (campaign_id,)
            ).fetchall()
            for child in children:
                if child["state"] in {"cancelled", "completed"}:
                    continue
                started = child["phase"] in {"discovered", "starting", "ready"}
                if state == "active" and started:
                    self.engine._integrity(db, child, "discovery_join")
            for child in children:
                if child["state"] in {"cancelled", "completed"}:
                    continue
                started = child["phase"] in {"discovered", "starting", "ready"}
                target_state = "draft" if state == "active" and not started else state
                db.execute(
                    "UPDATE tasks SET state=?,updated_at=? WHERE id=?",
                    (target_state, self.clock(), child["id"]),
                )
                if state == "cancelled":
                    db.execute(
                        "UPDATE items SET status='cancelled' WHERE task_id=? AND status='not_attempted'",
                        (child["id"],),
                    )
                    db.execute(
                        "DELETE FROM quota_reservations WHERE task_id=? AND state='reserved'",
                        (child["id"],),
                    )
            db.execute(
                "UPDATE campaigns SET state=?,updated_at=? WHERE id=?",
                ("materialized" if state == "active" else state, self.clock(), campaign_id),
            )
            if state == "cancelled":
                db.execute(
                    "DELETE FROM content_contacts WHERE assignment_id IN (SELECT ca.id FROM campaign_assignments ca LEFT JOIN items i ON i.id=ca.item_id WHERE ca.campaign_id=? AND (i.id IS NULL OR i.attempt_id IS NULL OR "
                    + UNSUBMITTED_MANUAL
                    + "))",
                    (campaign_id,),
                )
            Store.audit(
                db, "campaign.state", {"campaign_id": campaign_id, "state": state}, now=self.clock()
            )
        return self.get(campaign_id)
