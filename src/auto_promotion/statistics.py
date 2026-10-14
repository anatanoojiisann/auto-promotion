"""Shared UTC+8 statistics. Never calls a platform or executes outbound work."""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .models import PLATFORMS, canonical, digest

ZONE = ZoneInfo("Asia/Shanghai")
REPLYABLE = {
    "send_message",
    "send_file",
    "schedule_message",
    "schedule_file",
    "reply",
    "post",
    "publish",
    "manual_comment",
    "quote",
}
STATUSES = (
    "confirmed",
    "scheduled",
    "unknown",
    "failed_known",
    "missed",
    "cancelled",
    "not_attempted",
    "executing",
    "manual_attention",
)


def day_bounds(value):
    day = date.fromisoformat(value) if isinstance(value, str) else value
    start = datetime.combine(day, datetime.min.time(), ZONE)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def publication_time(value, now):
    """Only accept an explicit, finite platform timestamp; never infer from the plan."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 < value <= now else None


def record_publication(db, item_id, result, now):
    published = publication_time(result.get("published_at"), now)
    db.execute(
        "INSERT INTO publication_evidence VALUES(?,?,?,?) "
        "ON CONFLICT(item_id) DO UPDATE SET "
        "published_at=COALESCE(excluded.published_at,publication_evidence.published_at), "
        "verified_at=excluded.verified_at,timestamp_source=CASE "
        "WHEN excluded.published_at IS NOT NULL THEN 'platform' "
        "ELSE publication_evidence.timestamp_source END",
        (item_id, published, now, "platform" if published is not None else "unavailable"),
    )


class Statistics:
    def __init__(self, store, *, clock=time.time):
        self.store = store
        self.clock = clock

    def snapshot(self, *, day=None, since=None, until=None):
        now = self.clock()
        if day is not None:
            since, end = day_bounds(day)
            until = min(end, now)
        else:
            since = 0 if since is None else float(since)
            until = now if until is None else min(float(until), now)
        if not all(math.isfinite(v) for v in (since, until)) or since < 0 or since > until:
            raise ValueError("统计时间范围无效")
        with self.store.connect() as db:
            db.execute("BEGIN")
            return self._snapshot(db, since, until)

    def _snapshot(self, db, since, until):
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT i.*,a.platform,p.published_at,p.verified_at,c.checked_through,c.status feedback_status "
                "FROM items i JOIN tasks t ON t.id=i.task_id JOIN accounts a ON a.id=i.account_id "
                "LEFT JOIN publication_evidence p ON p.item_id=i.id "
                "LEFT JOIN feedback_checks c ON c.item_id=i.id "
                "WHERE t.mode='live' AND a.mode='live'"
            )
        ]
        accounts = Counter(
            r[0] for r in db.execute("SELECT platform FROM accounts WHERE mode='live'")
        )
        events = [
            dict(r)
            for r in db.execute(
                "SELECT e.* FROM feedback_events e JOIN items i ON i.id=e.item_id "
                "JOIN tasks t ON t.id=i.task_id JOIN accounts a ON a.id=i.account_id "
                "WHERE t.mode='live' AND a.mode='live' AND e.occurred_at<?",
                (until,),
            )
        ]
        platforms = []
        for platform in PLATFORMS:
            items = [r for r in rows if r["platform"] == platform]
            planned = [r for r in items if since <= r["due_at"] < until]
            statuses = Counter(r["status"] for r in planned)
            confirmed = []
            for row in items:
                result = json.loads(row["result"])
                row["action"] = json.loads(row["payload"]).get("action", "")
                if (
                    row["status"] == "confirmed"
                    and result.get("verified") is True
                    and row["external_reference"]
                    and not row["external_reference"].startswith("simulation://")
                ):
                    confirmed.append(row)
            published = [
                r
                for r in confirmed
                if r["published_at"] is not None and since <= r["published_at"] < until
            ]
            replyable = [
                r
                for r in confirmed
                if r["action"] in REPLYABLE
                and (r["published_at"] is None or r["published_at"] < until)
            ]
            cohort = [r for r in published if r["action"] in REPLYABLE]
            ids = {r["id"] for r in replyable}
            replies = [e for e in events if e["item_id"] in ids]
            responded = {e["item_id"] for e in replies} & {r["id"] for r in cohort}
            complete = bool(replyable) and all(
                r["feedback_status"] == "complete" and r["checked_through"] >= until
                for r in replyable
            )
            feedback_status = (
                "complete"
                if complete
                else (
                    "failed"
                    if any(r["feedback_status"] == "failed" for r in replyable)
                    else "partial"
                    if replies or any(r["feedback_status"] for r in replyable)
                    else "unavailable"
                )
            )
            observed = sum(since <= e["occurred_at"] < until for e in replies)
            platforms.append(
                {
                    "platform": platform,
                    "live_accounts": accounts[platform],
                    "planned_items": len(planned),
                    "planned_statuses": {s: statuses[s] for s in STATUSES},
                    "published_success": len(published),
                    "published_by_action": dict(Counter(r["action"] for r in published)),
                    "confirmed_without_publication_time": sum(
                        r["published_at"] is None for r in confirmed
                    ),
                    "pending_verification": sum(
                        r["status"] in {"scheduled", "unknown"} for r in items
                    ),
                    "replyable_success": len(cohort),
                    "feedback_status": feedback_status,
                    "received_replies": observed if complete else None,
                    "responded_interactions": len(responded) if complete else None,
                    "reply_rate": len(responded) / len(cohort) if complete and cohort else None,
                    "observed_replies": observed,
                    "observed_responded_interactions": len(responded),
                }
            )
        connected = [p for p in platforms if p["live_accounts"] or p["planned_items"]]
        complete = bool(connected) and all(p["feedback_status"] == "complete" for p in connected)
        sum_keys = (
            "planned_items",
            "published_success",
            "confirmed_without_publication_time",
            "pending_verification",
            "replyable_success",
            "observed_replies",
            "observed_responded_interactions",
        )
        totals = {key: sum(p[key] for p in platforms) for key in sum_keys}
        totals["received_replies"] = totals["observed_replies"] if complete else None
        totals["responded_interactions"] = (
            totals["observed_responded_interactions"] if complete else None
        )
        totals["feedback_complete"] = complete
        totals["reply_rate"] = (
            totals["responded_interactions"] / totals["replyable_success"]
            if complete and totals["replyable_success"]
            else None
        )
        return {
            "timezone": "Asia/Shanghai",
            "scope": "all_live_tasks",
            "since": since,
            "until": until,
            "platforms": platforms,
            "totals": totals,
            "publication_time_basis": "verified_platform_timestamp",
            "reply_basis": "other_users_direct_replies;received_by_reply_time;responded_by_publication_time",
        }

    def record_feedback(self, item_id, event_id, occurred_at, *, is_self=False):
        """Collector entry point: IDs only, no reply text or credentials are retained."""
        now = self.clock()
        when = publication_time(occurred_at, now)
        if not event_id or when is None:
            raise ValueError("回复记录缺少有效标识或时间")
        if is_self:
            return False
        with self.store.connect(write=True) as db:
            row = db.execute(
                "SELECT a.platform,a.id account_id,i.payload,p.published_at FROM items i "
                "JOIN accounts a ON a.id=i.account_id JOIN tasks t ON t.id=i.task_id "
                "LEFT JOIN publication_evidence p ON p.item_id=i.id "
                "WHERE i.id=? AND a.mode='live' AND t.mode='live'",
                (item_id,),
            ).fetchone()
            if not row or json.loads(row["payload"]).get("action") not in REPLYABLE:
                raise ValueError("回复无法关联到真实外发互动")
            if row["published_at"] is not None and when < row["published_at"]:
                raise ValueError("回复时间早于原互动")
            existing = db.execute(
                "SELECT item_id FROM feedback_events WHERE platform=? AND account_id=? AND event_id=?",
                (row["platform"], row["account_id"], str(event_id)),
            ).fetchone()
            if existing and existing[0] != item_id:
                raise ValueError("回复已关联到其他互动")
            return (
                db.execute(
                    "INSERT OR IGNORE INTO feedback_events VALUES(?,?,?,?,?,?)",
                    (row["platform"], row["account_id"], str(event_id), item_id, when, now),
                ).rowcount
                == 1
            )

    def mark_feedback(self, item_id, checked_through, status, *, cursor=0):
        now = self.clock()
        if (
            status not in {"complete", "partial", "failed"}
            or publication_time(checked_through, now) is None
        ):
            raise ValueError("回复采集状态无效")
        with self.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO feedback_checks(item_id,checked_through,status,last_message_id) VALUES(?,?,?,?) "
                "ON CONFLICT(item_id) DO UPDATE SET checked_through=excluded.checked_through,"
                "status=excluded.status,last_message_id=max(feedback_checks.last_message_id,excluded.last_message_id)",
                (item_id, checked_through, status, cursor),
            )

    def reports(self, limit=30):
        if not 1 <= limit <= 100:
            raise ValueError("报告数量必须介于 1 和 100")
        return [
            {**r, "content": json.loads(r["content"])}
            for r in self.store.rows(
                "SELECT kind,cutoff,created_at,updated_at,revision,content FROM statistical_reports "
                "ORDER BY cutoff DESC,kind LIMIT ?",
                (limit,),
            )
        ]

    def _save_report(self, db, kind, start, cutoff, now):
        day = datetime.fromtimestamp(cutoff - 1, ZONE).date()
        cumulative_start, _ = day_bounds(day)
        content = {
            "kind": kind,
            "cutoff": cutoff,
            "day": day.isoformat(),
            "period": self._snapshot(db, start, cutoff),
            "daily": self._snapshot(db, cumulative_start, cutoff),
        }
        content_hash = digest(content)
        previous = db.execute(
            "SELECT content_hash FROM statistical_reports WHERE kind=? AND cutoff=?", (kind, cutoff)
        ).fetchone()
        if previous and previous[0] == content_hash:
            return False
        db.execute(
            "INSERT INTO statistical_reports VALUES(?,?,?,?,?,1,?,?) "
            "ON CONFLICT(kind,cutoff) DO UPDATE SET updated_at=excluded.updated_at,"
            "revision=statistical_reports.revision+1,content_hash=excluded.content_hash,content=excluded.content",
            (kind, cutoff, start, now, now, content_hash, canonical(content)),
        )
        return True

    def run_due(self):
        """Persist elapsed 3-hour slots, 20:00 snapshots, midnight finals and late evidence."""
        now = self.clock()
        changed = 0
        with self.store.connect(write=True) as db:
            state = db.execute("SELECT * FROM reporting_state WHERE id=1").fetchone()
            revision = int(
                db.execute("SELECT value FROM meta WHERE key='statistics_revision'").fetchone()[0]
            )
            today = datetime.fromtimestamp(now, ZONE).date()
            since = state["last_tick"] if state else day_bounds(today)[0]
            if now < since:
                return {"updated_reports": 0, "clock_moved_backwards": True}
            end = min(now, since + 31 * 86400)
            if not state or revision != state["source_revision"]:
                for row in db.execute(
                    "SELECT kind,starts_at,cutoff FROM statistical_reports"
                ).fetchall():
                    changed += self._save_report(
                        db, row["kind"], row["starts_at"], row["cutoff"], now
                    )
            day = datetime.fromtimestamp(since, ZONE).date()
            if not state:
                start, cutoff = day_bounds(today - timedelta(days=1))
                changed += self._save_report(db, "daily_final", start, cutoff, now)
            while day <= datetime.fromtimestamp(end, ZONE).date():
                start, midnight = day_bounds(day)
                windows = [
                    ("interval", start + h * 3600 - 10800, start + h * 3600)
                    for h in range(3, 25, 3)
                ]
                windows += [
                    ("daily_20", start, start + 20 * 3600),
                    ("daily_final", start, midnight),
                ]
                for kind, window_start, cutoff in sorted(windows, key=lambda w: (w[2], w[0])):
                    if since < cutoff <= end:
                        changed += self._save_report(db, kind, window_start, cutoff, now)
                day += timedelta(days=1)
            db.execute(
                "INSERT INTO reporting_state VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET "
                "last_tick=excluded.last_tick,source_revision=excluded.source_revision",
                (end, revision),
            )
        return {"updated_reports": changed, "checked_through": end, "catch_up_pending": end < now}
