"""Local business goals, evidence imports, reply triage and read-only content feeds."""

import hashlib
import json
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .campaigns import Campaigns
from .core import RISK, Conflict, NotFound
from .models import canonical, digest, epoch
from .pools import Pools
from .store import Store, decode


class Growth:
    def __init__(self, engine):
        self.engine, self.store, self.clock = engine, engine.store, engine.clock

    def goals(self):
        return [
            decode(r, "payload")
            for r in self.store.rows("SELECT * FROM growth_goals ORDER BY created_at DESC,id")
        ]

    def goal(self, identifier):
        row = self.store.one("SELECT * FROM growth_goals WHERE id=?", (identifier,))
        if not row:
            raise NotFound("业务目标不存在")
        return decode(row, "payload")

    def save_goal(self, value, identifier=None):
        if identifier:
            old = self.goal(identifier)["payload"]
            if old["mode"] != value.mode or old["app_store_url"] != value.app_store_url:
                raise Conflict("应用与真实／模拟范围不能更改，请创建新业务目标")
        for campaign_id in value.campaign_ids:
            campaign = Campaigns(self.engine).get(campaign_id, limit=1)
            accounts = Pools(self.engine).get(campaign["pool_id"])["accounts"]
            if any(a["mode"] != value.mode for a in accounts):
                raise Conflict("业务目标与活动的真实／模拟范围不一致")
        identifier = identifier or "goal-" + uuid.uuid4().hex[:12]
        now = self.clock()
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO growth_goals VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "payload=excluded.payload,updated_at=excluded.updated_at",
                (identifier, canonical(value.model_dump()), now, now),
            )
            Store.audit(db, "growth.goal_saved", {"goal_id": identifier}, now=now)
        return self.goal(identifier)

    def channels(self, goal_id):
        goal = self.goal(goal_id)["payload"]
        rows = [
            decode(r, "payload")
            for r in self.store.rows(
                "SELECT * FROM growth_channels WHERE goal_id=? ORDER BY created_at,id", (goal_id,)
            )
        ]
        for row in rows:
            p = row["payload"]
            row["url"] = (
                goal["app_store_url"]
                + "?"
                + urlencode({"pt": p["provider_token"], "ct": p["campaign_token"], "mt": "8"})
                if p["provider_token"]
                else None
            )
        return rows

    def add_channel(self, goal_id, value):
        goal = self.goal(goal_id)["payload"]
        if value.campaign_id and value.campaign_id not in goal["campaign_ids"]:
            raise Conflict("请先将活动关联到业务目标")
        if value.account_id:
            account = self.engine.account(value.account_id)
            if account["mode"] != goal["mode"]:
                raise Conflict("渠道账号与业务目标的真实／模拟范围不一致")
            if value.campaign_id:
                campaign = Campaigns(self.engine).get(value.campaign_id, limit=1)
                if (
                    value.account_id
                    not in Pools(self.engine).get(campaign["pool_id"])["account_ids"]
                ):
                    raise Conflict("渠道账号不在该活动号池内")
        key = digest(
            [goal["mode"], goal["app_store_url"], value.provider_token, value.campaign_token]
        )
        identifier = "channel-" + uuid.uuid4().hex[:12]
        with self.store.connect(write=True) as db:
            if db.execute("SELECT 1 FROM growth_channels WHERE link_key=?", (key,)).fetchone():
                raise Conflict("该渠道标记已经使用，避免跨目标混淆归因")
            db.execute(
                "INSERT INTO growth_channels VALUES(?,?,?,?,?)",
                (identifier, goal_id, canonical(value.model_dump()), key, self.clock()),
            )
            Store.audit(
                db, "growth.channel_created", {"goal_id": goal_id, "channel_id": identifier}
            )
        return next(c for c in self.channels(goal_id) if c["id"] == identifier)

    def import_events(self, goal_id, values):
        self.goal(goal_id)
        channels = {c["id"] for c in self.channels(goal_id)}
        imported = duplicates = 0
        now = self.clock()
        with self.store.connect(write=True) as db:
            for value in sorted(values, key=lambda v: (epoch(v.occurred_at), v.kind == "refund")):
                at = epoch(value.occurred_at)
                if at <= 0 or at > now:
                    raise Conflict("事件发生时间必须为过去的实际时间")
                if value.channel_id and value.channel_id not in channels:
                    raise Conflict("事件渠道不属于该业务目标")
                raw = value.model_dump()
                fingerprint = digest(raw)
                existing = db.execute(
                    "SELECT payload_hash FROM conversion_events WHERE goal_id=? AND source=? AND event_id=?",
                    (goal_id, value.source, value.event_id),
                ).fetchone()
                if existing:
                    if existing[0] != fingerprint:
                        raise Conflict("相同来源事件编号的数据不同，不能覆盖原始证据")
                    duplicates += 1
                    continue
                subject = hashlib.sha256((goal_id + ":" + value.subject_key).encode()).hexdigest()
                previous = [
                    decode(dict(r), "payload")
                    for r in db.execute(
                        "SELECT * FROM conversion_events WHERE goal_id=? AND subject_hash=?",
                        (goal_id, subject),
                    )
                ]
                if value.kind != "refund" and any(e["kind"] == value.kind for e in previous):
                    raise Conflict("同一匿名用户的首次事件／试用已经登记，不能重复计数")
                if value.kind in {"first_charge", "refund"}:
                    transactions = [
                        decode(dict(r), "payload")
                        for r in db.execute(
                            "SELECT * FROM conversion_events WHERE goal_id=? AND kind IN ('first_charge','refund') "
                            "AND json_extract(payload,'$.transaction_id')=?",
                            (goal_id, value.transaction_id),
                        )
                    ]
                    charges = [e for e in transactions if e["kind"] == "first_charge"]
                    if value.kind == "first_charge" and charges:
                        raise Conflict("交易已登记，不能跨来源重复计算扣费")
                    if value.kind == "refund":
                        if not charges or charges[0]["subject_hash"] != subject:
                            raise Conflict("退款必须关联同一用户的已登记扣费交易")
                        charge = charges[0]
                        refunded = sum(
                            e["payload"]["amount_minor"]
                            for e in transactions
                            if e["kind"] == "refund"
                        )
                        if (
                            value.currency != charge["payload"]["currency"]
                            or at < charge["occurred_at"]
                            or refunded + value.amount_minor > charge["payload"]["amount_minor"]
                        ):
                            raise Conflict("退款币种、时间或累计金额与原交易不一致")
                raw["subject_key"] = subject
                db.execute(
                    "INSERT INTO conversion_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        goal_id,
                        value.source,
                        value.event_id,
                        subject,
                        value.kind,
                        at,
                        value.channel_id or None,
                        canonical(raw),
                        fingerprint,
                        now,
                    ),
                )
                imported += 1
            Store.audit(
                db,
                "growth.events_imported",
                {"goal_id": goal_id, "imported": imported, "duplicates": duplicates},
                now=now,
            )
        return {"imported": imported, "duplicates": duplicates}

    def import_metrics(self, goal_id, values):
        goal = self.goal(goal_id)["payload"]
        channels = {c["id"] for c in self.channels(goal_id)}
        today = datetime.fromtimestamp(self.clock(), ZoneInfo(goal["timezone"])).date()
        with self.store.connect(write=True) as db:
            for value in values:
                if value.channel_id not in channels or date.fromisoformat(value.day) > today:
                    raise Conflict("指标渠道不属于该目标，或日期在未来")
                db.execute(
                    "INSERT INTO growth_store_metrics VALUES(?,?,?,?,?) "
                    "ON CONFLICT(channel_id,day,country) DO UPDATE SET "
                    "payload=excluded.payload,updated_at=excluded.updated_at",
                    (
                        value.channel_id,
                        value.day,
                        value.country,
                        canonical(value.model_dump()),
                        self.clock(),
                    ),
                )
            Store.audit(db, "growth.metrics_imported", {"goal_id": goal_id, "count": len(values)})
        return {"saved": len(values)}

    def report(self, goal_id):
        goal = self.goal(goal_id)["payload"]
        start, end, now = epoch(goal["starts_at"]), epoch(goal["ends_at"]), self.clock()
        countries = set(goal["countries"])
        rows = [
            decode(r, "payload")
            for r in self.store.rows(
                "SELECT * FROM conversion_events WHERE goal_id=? AND occurred_at<=?", (goal_id, now)
            )
        ]
        accepted = [r for r in rows if not countries or r["payload"]["country"] in countries]
        cohort = {
            r["subject_hash"]: r
            for r in accepted
            if r["kind"] == "first_download" and start <= r["occurred_at"] < end
        }
        events = [
            r
            for r in accepted
            if r["subject_hash"] in cohort
            and r["occurred_at"] >= cohort[r["subject_hash"]]["occurred_at"]
        ]
        funnel = {
            kind: len({r["subject_hash"] for r in events if r["kind"] == kind})
            for kind in ("first_download", "first_generation", "trial_start", "first_charge")
        }
        trials = [r for r in events if r["kind"] == "trial_start"]
        mature = [r for r in trials if epoch(r["payload"]["trial_ends_at"]) <= now]
        paid = {r["subject_hash"]: r for r in events if r["kind"] == "first_charge"}
        mature_paid = sum(
            1
            for r in mature
            if r["subject_hash"] in paid
            and paid[r["subject_hash"]]["occurred_at"] >= epoch(r["payload"]["trial_ends_at"])
        )
        revenue = defaultdict(lambda: {"charges_minor": 0, "refunds_minor": 0, "net_minor": 0})
        for row in events:
            if row["kind"] in {"first_charge", "refund"}:
                p = row["payload"]
                field = "charges_minor" if row["kind"] == "first_charge" else "refunds_minor"
                revenue[p["currency"]][field] += p["amount_minor"]
        for amounts in revenue.values():
            amounts["net_minor"] = amounts["charges_minor"] - amounts["refunds_minor"]
        metrics = [
            decode(r, "payload")["payload"]
            for r in self.store.rows(
                "SELECT m.* FROM growth_store_metrics m JOIN growth_channels c ON c.id=m.channel_id "
                "WHERE c.goal_id=?",
                (goal_id,),
            )
        ]
        zone = ZoneInfo(goal["timezone"])
        metrics = [
            m
            for m in metrics
            if start
            <= datetime.combine(date.fromisoformat(m["day"]), datetime.min.time(), zone).timestamp()
            < min(end, now)
            and (not countries or m["country"] in countries)
        ]
        if not countries:
            grouped = defaultdict(list)
            for metric in metrics:
                grouped[(metric["channel_id"], metric["day"])].append(metric)
            # Global and territorial extracts describe overlapping users, never add both.
            metrics = [
                m
                for group in grouped.values()
                for m in (next(([r] for r in group if not r["country"]), group))
            ]

        def total(field):
            known = [m[field] for m in metrics if m[field] is not None]
            return sum(known) if known else None

        spend = defaultdict(int)
        for m in metrics:
            if m["spend_minor"] is not None:
                spend[m["currency"]] += m["spend_minor"]
        # Aggregate App Store reports and anonymous event counts are separate populations.
        downloads = total("first_downloads")
        linked = sum(1 for r in events if r["kind"] == "first_download" and r["channel_id"])
        interactions = []
        if goal["campaign_ids"]:
            placeholders = ",".join("?" for _ in goal["campaign_ids"])
            interactions = self.store.rows(
                "SELECT p.published_at,i.result,i.external_reference FROM publication_evidence p "
                "JOIN items i ON i.id=p.item_id JOIN tasks t ON t.id=i.task_id "
                f"WHERE t.campaign_id IN ({placeholders}) AND t.mode=? AND i.status='confirmed' "
                "AND p.published_at>=? AND p.published_at<?",
                (*goal["campaign_ids"], goal["mode"], start, min(end, now)),
            )
            interactions = [
                i
                for i in interactions
                if i["external_reference"] and json.loads(i["result"]).get("verified")
            ]
        today = datetime.fromtimestamp(now, zone).date()
        today_count = sum(
            datetime.fromtimestamp(i["published_at"], zone).date() == today for i in interactions
        )
        cac = (
            spend[goal["currency"]] / funnel["first_charge"]
            if goal["currency"] in spend and funnel["first_charge"]
            else None
        )
        return {
            "goal": self.goal(goal_id),
            "observed_at": now,
            "cohort_basis": "first_download_in_goal_window",
            "data_status": "partial_import" if rows or metrics else "not_connected",
            "event_funnel": funnel,
            "attributed_event_downloads": linked,
            "unattributed_event_downloads": funnel["first_download"] - linked,
            "excluded_country_events": len(rows) - len(accepted),
            "events_without_cohort_download": sum(
                r["subject_hash"] not in cohort for r in accepted
            ),
            "store_metrics": {
                "product_page_views": total("product_page_views"),
                "first_downloads": downloads,
                "reported_days": len({m["day"] for m in metrics}),
            },
            "mature_trials": len(mature),
            "immature_trials": len(trials) - len(mature),
            "mature_trial_paid": mature_paid,
            "observed_trial_to_paid_rate": mature_paid / len(mature) if mature else None,
            "revenue_by_currency": dict(revenue),
            "spend_by_currency": dict(spend),
            "observed_cac_minor": cac,
            "cac_within_target": cac <= goal["max_cac_minor"]
            if cac is not None and goal["max_cac_minor"] is not None
            else None,
            "interactions": {
                "confirmed_in_window": len(interactions),
                "confirmed_today": today_count,
                "daily_target_progress": today_count / goal["daily_interactions"]
                if goal["daily_interactions"]
                else None,
            },
            "progress": {
                "store_downloads": downloads / goal["downloads_target"]
                if downloads is not None and goal["downloads_target"]
                else None,
                "observed_paid": funnel["first_charge"] / goal["paid_target"]
                if goal["paid_target"]
                else None,
            },
            "channels": self.channels(goal_id),
            "channel_funnel": {
                c["id"]: {
                    kind: len(
                        {
                            r["subject_hash"]
                            for r in events
                            if r["kind"] == kind
                            and cohort[r["subject_hash"]]["channel_id"] == c["id"]
                        }
                    )
                    for kind in funnel
                }
                for c in self.channels(goal_id)
            },
        }

    def sync_inbox(self):
        now = self.clock()
        with self.store.connect(write=True) as db:
            for row in db.execute(
                "SELECT f.* FROM feedback_events f JOIN items i ON i.id=f.item_id "
                "JOIN tasks t ON t.id=i.task_id WHERE t.mode='live'"
            ):
                identifier = (
                    "reply-" + digest([row["platform"], row["account_id"], row["event_id"]])[:24]
                )
                db.execute(
                    "INSERT OR IGNORE INTO reply_inbox(id,platform,account_id,event_id,item_id,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        identifier,
                        row["platform"],
                        row["account_id"],
                        row["event_id"],
                        row["item_id"],
                        now,
                    ),
                )

    def inbox(self, offset=0, limit=100):
        Campaigns._page(offset, limit)
        self.sync_inbox()
        rows = self.store.rows(
            "SELECT r.*,i.payload,i.task_id,f.occurred_at,a.label account_label "
            "FROM reply_inbox r JOIN items i ON i.id=r.item_id JOIN accounts a ON a.id=r.account_id "
            "JOIN feedback_events f ON f.platform=r.platform AND f.account_id=r.account_id "
            "AND f.event_id=r.event_id ORDER BY f.occurred_at DESC,r.id LIMIT ? OFFSET ?",
            (limit, offset),
        )
        return {
            "total": self.store.one("SELECT count(*) n FROM reply_inbox")["n"],
            "offset": offset,
            "counts": {
                r["state"]: r["n"]
                for r in self.store.rows("SELECT state,count(*) n FROM reply_inbox GROUP BY state")
            },
            "items": [decode(r, "payload") for r in rows],
        }

    def update_reply(self, identifier, value):
        self.sync_inbox()
        row = self.store.one("SELECT * FROM reply_inbox WHERE id=?", (identifier,))
        if not row:
            raise NotFound("回复待办不存在")
        if value.followup_task_id:
            task = self.engine.task(value.followup_task_id)
            if (
                task["mode"] != "live"
                or row["account_id"] not in task["account_ids"]
                or task["id"]
                == self.store.one("SELECT task_id FROM items WHERE id=?", (row["item_id"],))[
                    "task_id"
                ]
            ):
                raise Conflict("后续回复应关联同账号的新真实任务")
        if value.state == "responded":
            if not value.followup_task_id:
                raise Conflict("完成回复需要关联已核验的后续任务")
            origin = json.loads(
                self.store.one("SELECT payload FROM items WHERE id=?", (row["item_id"],))["payload"]
            )
            matching = [
                i
                for i in task["items"]
                if i["account_id"] == row["account_id"]
                and i["payload"]["target"] == origin["target"]
                and str(i["payload"].get("reply_to") or i["payload"].get("discord_reply_to") or "")
                == row["event_id"].rsplit(":", 1)[-1]
                and i["payload"]["action"] == "reply"
                and i["status"] == "confirmed"
                and i["external_reference"]
            ]
            if not matching:
                raise Conflict("后续任务尚无针对该回复的已核验投递")
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE reply_inbox SET state=?,note=?,followup_task_id=?,updated_at=? WHERE id=?",
                (value.state, value.note, value.followup_task_id or None, self.clock(), identifier),
            )
            Store.audit(db, "growth.reply_updated", {"reply_id": identifier, "state": value.state})
        return {"id": identifier, **value.model_dump()}

    def create_followup(self, identifier, value):
        from .dispatcher import AccountLanes

        self.sync_inbox()
        reply = self.store.one("SELECT * FROM reply_inbox WHERE id=?", (identifier,))
        if not reply:
            raise NotFound("回复待办不存在")
        origin = json.loads(
            self.store.one("SELECT payload FROM items WHERE id=?", (reply["item_id"],))["payload"]
        )
        if (
            value.account_ids != [reply["account_id"]]
            or value.policy.target_allowlist != [origin["target"]]
            or value.policy.max_joins
        ):
            raise Conflict("后续任务必须使用原账号、原目标白名单且不入新群")
        if epoch(value.policy.ends_at) <= self.clock():
            raise Conflict("请设置新的有效执行窗口")
        with AccountLanes(self.store).acquire(reply["account_id"]):
            reply = self.store.one("SELECT * FROM reply_inbox WHERE id=?", (identifier,))
            if reply["followup_task_id"]:
                task = self.engine.task(reply["followup_task_id"])
                if task["state"] not in {"cancelled", "completed"}:
                    return task
            task = self.engine.create_task(value)
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE reply_inbox SET followup_task_id=?,state='reviewing',updated_at=? WHERE id=?",
                    (task["id"], self.clock(), identifier),
                )
                Store.audit(db, "growth.followup_created", {"reply_id": identifier}, task["id"])
            return self.engine.task(task["id"])

    def review_content(self, identifier, value, expected_revision):
        with self.store.connect(write=True) as db:
            row = db.execute("SELECT * FROM opportunities WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise NotFound("内容不存在")
            from .planner import content_key

            if row["revision"] != expected_revision:
                raise Conflict("内容已更新，请刷新后重新核对")
            if row["platform"] != value.platform or row["content_key"] != content_key(
                value.model_dump()
            ):
                raise Conflict("原始平台、目标和消息标识不能更换")
            if value.permission == "allowed" and not value.permission_evidence.strip():
                raise Conflict("允许互动需要填写群规或管理员许可的真实证据")
            db.execute(
                "UPDATE opportunities SET payload=?,revision=revision+1,updated_at=? WHERE id=?",
                (canonical(value.model_dump()), self.clock(), identifier),
            )
            Store.audit(
                db,
                "growth.content_reviewed",
                {"opportunity_id": identifier, "revision": expected_revision + 1},
            )
        return {"id": identifier, "revision": expected_revision + 1}

    def feeds(self):
        return [
            decode(r, "payload", "result")
            for r in self.store.rows("SELECT * FROM content_feeds ORDER BY id")
        ]

    def save_feed(self, value, identifier=None):
        account = self.engine.account(value.account_id)
        if account["platform"] != "telegram" or RISK.search(value.target):
            raise Conflict("请选择 Telegram 账号和公开相关目标")
        identifier = identifier or "feed-" + uuid.uuid4().hex[:12]
        with self.store.connect(write=True) as db:
            if (
                identifier
                and db.execute(
                    "SELECT lease_until FROM content_feeds WHERE id=? AND lease_until>?",
                    (identifier, self.clock()),
                ).fetchone()
            ):
                raise Conflict("来源正在采集，请稍后更新")
            db.execute(
                "INSERT INTO content_feeds(id,payload,next_at) VALUES(?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,next_at=excluded.next_at",
                (identifier, canonical(value.model_dump()), self.clock()),
            )
        return next(f for f in self.feeds() if f["id"] == identifier)

    def poll_feeds(self):
        completed = 0
        for feed in self.feeds():
            p, now = feed["payload"], self.clock()
            if not p["enabled"] or feed["next_at"] > now:
                continue
            with self.store.connect(write=True) as db:
                if not db.execute(
                    "UPDATE content_feeds SET lease_until=? WHERE id=? AND lease_until<=? AND next_at<=?",
                    (now + 300, feed["id"], now, now),
                ).rowcount:
                    continue
            try:
                result = Campaigns(self.engine).collect_telegram_content(
                    p["account_id"], p["target"], p["limit"]
                )
                status = "complete"
                completed += 1
            except Exception:
                result, status = {"reason": "content_read_incomplete"}, "needs_check"
            with self.store.connect(write=True) as db:
                db.execute(
                    "UPDATE content_feeds SET next_at=?,last_at=?,status=?,result=?,lease_until=0 WHERE id=?",
                    (
                        self.clock() + p["interval_seconds"],
                        self.clock(),
                        status,
                        canonical(result),
                        feed["id"],
                    ),
                )
        return {"completed": completed}

    def readiness(self, *, worker=False, report_worker=False):
        accounts = self.engine.accounts()
        live = [a for a in accounts if a["mode"] == "live"]
        usable = [
            a
            for a in live
            if a["status"] == "healthy"
            and not a["disabled"]
            and a["cooldown_until"] <= self.clock()
        ]
        profiles = {a["id"]: Pools(self.engine).profile(a["id"])["payload"] for a in usable}
        content = [
            json.loads(r["payload"]) for r in self.store.rows("SELECT payload FROM opportunities")
        ]
        ready_content = [
            p
            for p in content
            if p["permission"] == "allowed"
            and p["permission_evidence"].strip()
            and p["is_public"]
            and p["confidence"] >= 0.8
            and p["language"] != "unknown"
            and (not p["expires_at"] or epoch(p["expires_at"]) > self.clock())
        ]
        blockers = Counter()
        for row in self.store.rows(
            "SELECT id FROM tasks WHERE state IN ('draft','active','paused')"
        ):
            for blocker in self.engine.task(row["id"])["readiness"]["blockers"]:
                blockers[blocker["message"]] += 1
        return {
            "live_accounts": len(live),
            "usable_accounts": len(usable),
            "telegram_usable": sum(a["platform"] == "telegram" for a in usable),
            "profiles_missing": [
                a["label"]
                for a in usable
                if not all(profiles[a["id"]][k] for k in ("brand", "role", "facts"))
            ],
            "pools": len(Pools(self.engine).list()),
            "content_total": len(content),
            "content_with_reviewed_evidence": len(ready_content),
            "global_stop": self.store.one("SELECT value FROM meta WHERE key='global_stop'")["value"]
            != "0",
            "live_writes_enabled": self.engine.live_enabled,
            "sender_configured": worker,
            "read_scheduler_configured": report_worker,
            "task_blockers": dict(blockers),
            "feed_count": len(self.feeds()),
            "goal_count": len(self.goals()),
            "platform_execution": {
                "telegram": "automatic_after_start",
                "discord": "automatic_after_start",
                "instagram": "owned_media_or_manual_comment",
                "x": "manual",
                "reddit": "manual",
            },
        }
