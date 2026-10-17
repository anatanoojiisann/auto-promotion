"""UTC quotas and delivery reservations, shared by planning, freezing and claiming."""

import json
from bisect import bisect_left, bisect_right, insort
from collections import defaultdict

from .models import ExecutionSettings, QuotaLimit, canonical, digest

# A manual handoff has not contacted the platform. Keep its audit attempt, but do
# not charge a delivery until a manual outcome or an actual submission is recorded.
UNSUBMITTED_MANUAL = (
    "(i.status='manual_attention' AND json_extract(i.payload,'$.execution_mode')='manual' "
    "AND json_extract(i.result,'$.reason')='manual_publication_required' "
    "AND i.external_reference IS NULL "
    "AND COALESCE(json_extract(i.result,'$.submission_started'),0)=0)"
)


def settings(db):
    row = db.execute("SELECT value FROM meta WHERE key='execution_settings'").fetchone()
    return ExecutionSettings.model_validate_json(row[0]).model_dump()


def bounded(left, right):
    return {k: min(left[k], right[k]) for k in ("daily", "hourly")}


def scopes(db, account, task, payload):
    account, task = dict(account), dict(task)
    policy = task["policy"]
    if isinstance(policy, str):
        policy = json.loads(policy)
    current = settings(db)
    snapshot = policy.get("quota_snapshot", {})
    approved = snapshot.get("execution") or ExecutionSettings().model_dump()
    mode, platform = account["mode"], account["platform"]
    result = [
        ("global", mode, bounded(current["global_limit"], approved["global_limit"])),
        (
            "account",
            account["id"],
            {"daily": account["daily_limit"], "hourly": account["hourly_limit"]},
        ),
        ("task", task["id"], {"daily": policy["daily_limit"], "hourly": 100000}),
        (
            "target",
            digest([platform, payload["target"].casefold()]),
            {"daily": policy["target_daily_limit"], "hourly": 100000},
        ),
        (
            "user",
            digest([platform, payload.get("recipient_id") or payload["target"].casefold()]),
            {"daily": policy["user_daily_limit"], "hourly": 100000},
        ),
    ]
    fallback = QuotaLimit(daily=100000, hourly=10000).model_dump()
    result.append(
        (
            "platform",
            platform,
            bounded(
                current["platform_limits"].get(platform, fallback),
                approved.get("platform_limits", {}).get(platform, fallback),
            ),
        )
    )
    campaign_id = task.get("campaign_id")
    if campaign_id:
        campaign = db.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if campaign:
            config = json.loads(campaign["config"])
            pool = db.execute(
                "SELECT limits FROM account_pools WHERE id=?", (campaign["pool_id"],)
            ).fetchone()
            pool_limit = json.loads(pool[0])
            result.extend(
                [
                    ("campaign", campaign_id, config["limit"]),
                    (
                        "pool",
                        campaign["pool_id"],
                        bounded(pool_limit, snapshot.get("pool", pool_limit)),
                    ),
                ]
            )
    return result


def event(account, task, payload, at, *, pool_id=None):
    return {
        "at": at,
        "mode": account["mode"],
        "global": account["mode"],
        "platform": account["platform"],
        "account": account["id"],
        "task": task["id"],
        "campaign": dict(task).get("campaign_id"),
        "pool": pool_id,
        "target": digest([account["platform"], payload["target"].casefold()]),
        "user": digest(
            [account["platform"], payload.get("recipient_id") or payload["target"].casefold()]
        ),
    }


def events(db, mode, *, delivery=False, exclude_item=None):
    result = []
    if delivery:
        rows = db.execute(
            "SELECT i.id,i.account_id,i.task_id,i.due_at at,i.payload,a.platform,a.mode,t.campaign_id,c.pool_id "
            "FROM items i JOIN accounts a ON a.id=i.account_id JOIN tasks t ON t.id=i.task_id "
            "LEFT JOIN campaigns c ON c.id=t.campaign_id WHERE i.attempt_id IS NOT NULL AND a.mode=? "
            "AND NOT " + UNSUBMITTED_MANUAL,
            (mode,),
        )
        for row in rows:
            if row["id"] == exclude_item:
                continue
            result.append(
                event(
                    {"id": row["account_id"], "platform": row["platform"], "mode": mode},
                    {"id": row["task_id"], "campaign_id": row["campaign_id"]},
                    json.loads(row["payload"]),
                    row["at"],
                    pool_id=row["pool_id"],
                )
            )
    else:
        for row in db.execute(
            "SELECT v.*,a.platform,t.campaign_id,c.pool_id FROM activity v JOIN accounts a ON a.id=v.account_id "
            "JOIN tasks t ON t.id=v.task_id LEFT JOIN campaigns c ON c.id=t.campaign_id WHERE v.mode=? "
            "AND NOT EXISTS (SELECT 1 FROM attempts att JOIN items i ON i.id=att.item_id "
            "WHERE att.task_id=v.task_id AND att.account_id=v.account_id "
            "AND att.started_at=v.started_at AND " + UNSUBMITTED_MANUAL + ")",
            (mode,),
        ):
            result.append(
                {
                    "at": row["started_at"],
                    "mode": mode,
                    "global": mode,
                    "account": row["account_id"],
                    "task": row["task_id"],
                    "platform": row["platform"],
                    "campaign": row["campaign_id"],
                    "pool": row["pool_id"],
                    "target": row["target_key"],
                    "user": row["user_key"],
                }
            )
    for row in db.execute(
        "SELECT r.*,a.platform,a.mode,t.campaign_id,c.pool_id FROM quota_reservations r "
        "JOIN accounts a ON a.id=r.account_id JOIN tasks t ON t.id=r.task_id "
        "LEFT JOIN campaigns c ON c.id=t.campaign_id WHERE r.state='reserved' AND a.mode=?",
        (mode,),
    ):
        if row["item_id"] != exclude_item:
            result.append(
                event(
                    {"id": row["account_id"], "platform": row["platform"], "mode": mode},
                    {"id": row["task_id"], "campaign_id": row["campaign_id"]},
                    json.loads(row["payload"]),
                    row["due_at"],
                    pool_id=row["pool_id"],
                )
            )
    return result


class Calendar:
    def __init__(self, records=()):
        self.records = []
        self.times = defaultdict(list)
        for record in records:
            self.add(record)

    def add(self, record):
        self.records.append(record)
        for scope in (
            "global",
            "platform",
            "account",
            "task",
            "campaign",
            "pool",
            "target",
            "user",
        ):
            if record.get(scope) is not None:
                insort(self.times[(record["mode"], scope, record[scope])], record["at"])

    def blocker(self, limits, at, mode, *, future=True):
        next_at, reason = at, None
        day = at - at % 86400
        for scope, key, limit in limits:
            times = self.times.get((mode, scope, key), [])
            if bisect_left(times, day + 86400) - bisect_left(times, day) >= limit["daily"]:
                next_at, reason = max(next_at, day + 86400), scope + "_daily_limit"
            if len(times) < limit["hourly"]:
                continue
            ends = [at] + (
                times[bisect_right(times, at) : bisect_left(times, at + 3600)] if future else []
            )
            for end in ends:
                left, right = bisect_right(times, end - 3600), bisect_right(times, end)
                if right - left >= limit["hourly"]:
                    next_at, reason = max(next_at, times[left] + 3600), scope + "_hourly_limit"
        return reason, max(at + 1, next_at)

    def slot(self, limits, start, end, mode):
        at = start
        while at < end:
            reason, later = self.blocker(limits, at, mode)
            if not reason:
                return at
            at = later
        return None


def check_claim(db, account, task, payload, due, now, item_id):
    limits = scopes(db, account, task, payload)
    actual = Calendar(events(db, account["mode"], exclude_item=item_id))
    if actual.blocker(limits, now, account["mode"], future=False)[0]:
        return "activity_limit"
    delivery = Calendar(events(db, account["mode"], delivery=True, exclude_item=item_id))
    if delivery.blocker(limits, due, account["mode"])[0]:
        return "delivery_limit"
    return None


def reserve(db, task):
    task = dict(task)
    if not task.get("campaign_id"):
        return
    from .core import Conflict

    calendars = {}
    for item in db.execute("SELECT * FROM items WHERE task_id=? ORDER BY due_at", (task["id"],)):
        if db.execute("SELECT 1 FROM quota_reservations WHERE item_id=?", (item["id"],)).fetchone():
            continue
        account = dict(
            db.execute("SELECT * FROM accounts WHERE id=?", (item["account_id"],)).fetchone()
        )
        payload = json.loads(item["payload"])
        if account["mode"] not in calendars:
            calendars[account["mode"]] = Calendar(events(db, account["mode"], delivery=True))
        calendar = calendars[account["mode"]]
        reason = calendar.blocker(
            scopes(db, account, task, payload), item["due_at"], account["mode"]
        )[0]
        if reason:
            raise Conflict("计划额度已被其他批次占用，请重新规划未批准活动：" + reason)
        db.execute(
            "INSERT INTO quota_reservations VALUES(?,?,?,?,?, 'reserved')",
            (item["id"], account["id"], task["id"], item["due_at"], canonical(payload)),
        )
        pool_id = db.execute(
            "SELECT pool_id FROM campaigns WHERE id=?", (task["campaign_id"],)
        ).fetchone()[0]
        calendar.add(event(account, task, payload, item["due_at"], pool_id=pool_id))
