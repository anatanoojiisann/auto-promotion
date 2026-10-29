"""Consistent, read-only projections of saved account/task evidence."""

import json
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .alert_plan_queues import merge_plan_queues
from .alert_summary import collect_summary, summary_cutoff
from .alert_templates import PLATFORMS, REASONS, STATES, ZONE, clean
from .health_checks import AUTH, NAVIGATION, RESTRICTED, TRANSIENT, VERIFICATION
from .models import digest, epoch
from .statistics import Statistics, day_bounds

IDENTITY = {
    "BrowserIdentityMismatch",
    "TabbitIdentityMismatch",
    "AccountIdentityChanged",
    "InstagramIdentityMismatch",
    "TikTokIdentityMismatch",
    "DuplicatePlatformIdentity",
}
AUTH_EXTRA = {"DiscordInvalidToken", "TikTokAuthorizationRequired"}
PERMISSION = {
    "DiscordMissingPermissions",
    "DiscordAttachmentPermissionRequired",
    "TikTokPermissionDenied",
    "XAPIAccessDenied",
}
TEMPORARY = TRANSIENT | {
    "BrowserNetworkUnavailable",
    "BrowserProxyUnavailable",
    "BrowserContextClosed",
    "TikTokTransportUnavailable",
}


@contextmanager
def read_database(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db
    finally:
        db.close()


def number(value):
    return (
        max(0, int(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    )


def project_statistics(data):
    """Allowlist numeric statistics; never forward arbitrary saved JSON."""
    keys = (
        "live_accounts",
        "planned_items",
        "published_success",
        "pending_verification",
        "confirmed_without_publication_time",
        "observed_replies",
    )
    platforms = []
    for p in data.get("platforms", []):
        if p.get("platform") not in PLATFORMS:
            continue
        platforms.append(
            {
                "platform": p["platform"],
                **{k: number(p.get(k)) for k in keys},
                "planned_statuses": {
                    k: number(p.get("planned_statuses", {}).get(k)) for k in STATES
                },
            }
        )
    t = data.get("totals", {})
    return {
        "platforms": platforms,
        "totals": {
            "received_replies": number(t["received_replies"])
            if t.get("received_replies") is not None
            else None,
            "observed_replies": number(t.get("observed_replies")),
            "published_success": number(t.get("published_success")),
            "confirmed_without_publication_time": number(
                t.get("confirmed_without_publication_time")
            ),
        },
    }


def account_issue(row, health, now, active):
    reason = health.get("reason")
    if health.get("risk_warning") or health.get("restricted") or reason in RESTRICTED:
        return "restricted", "critical"
    if reason in IDENTITY:
        return "identity_mismatch", "critical"
    if reason in VERIFICATION or any(
        health.get(k) for k in ("verification_required", "captcha_required", "challenge_required")
    ):
        return "verification_required", "warning"
    if reason in AUTH | AUTH_EXTRA:
        return "authorization_required", "warning"
    if reason in PERMISSION:
        return "permission_denied", "warning"
    if row["disabled"]:
        return "disabled", "info"
    if row["cooldown_until"] > now:
        return "cooldown", "warning" if active else "info"
    if reason in NAVIGATION:
        return "navigation_blocked", "warning"
    if reason in TEMPORARY:
        return "temporary_unavailable", "warning"
    if health.get("service_ready") is False:
        return "temporary_unavailable", "warning"
    if reason in {"InstagramRequestRejected", "TikTokRequestRejected", "DiscordRequestRejected"}:
        return "request_rejected", "warning"
    verified = health.get("healthy") is True and health.get("identity_verified") is True
    if verified and row["status"] == "healthy":
        if active and (not row["checked_at"] or now - row["checked_at"] > 300):
            return "stale", "warning"
        return "healthy", "info"
    return "unverified", "info"


class AlertSources:
    def __init__(self, path, *, clock=time.time, summary_only=False, plan_sources=None):
        self.path, self.clock = Path(path).resolve(), clock
        self.summary_only = summary_only
        self.plan_sources = plan_sources

    def collect(self):
        now = self.clock()
        with read_database(self.path) as db:
            if self.summary_only:
                sources = self.plan_sources() if self.plan_sources is not None else []
                summary = collect_summary(db, summary_cutoff(now), queue_at=now)
                return {
                    "at": now,
                    "summary": merge_plan_queues(db, summary, sources, self.path.parent, now),
                }
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            raw_accounts = [dict(r) for r in db.execute("SELECT * FROM accounts WHERE mode='live'")]
            raw_tasks = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE mode='live'")]
            all_items = defaultdict(list)
            pending_due = defaultdict(list)
            for r in db.execute(
                "SELECT i.task_id,i.account_id,i.status,i.result,i.external_reference,i.due_at,json_extract(i.payload,'$.action') action FROM items i JOIN accounts a ON a.id=i.account_id JOIN tasks t ON t.id=i.task_id WHERE a.mode='live' AND t.mode='live'"
            ):
                status = r["status"]
                if status == "confirmed" and not (
                    json.loads(r["result"]).get("verified") is True
                    and r["external_reference"]
                    and not r["external_reference"].startswith("simulation://")
                ):
                    status = "unverified"
                if status not in STATES:
                    status = "unverified"
                all_items[r["task_id"]].append((r["account_id"], status))
                if status == "not_attempted":
                    due = (
                        now if r["action"] in {"schedule_message", "schedule_file"} else r["due_at"]
                    )
                    pending_due[r["task_id"]].append((r["account_id"], due))
            policies = {t["id"]: json.loads(t["policy"]) for t in raw_tasks}
            deadlines = {
                t["id"]: epoch(policies[t["id"]]["ends_at"])
                if policies[t["id"]].get("ends_at")
                else 0
                for t in raw_tasks
            }
            live_task_ids = {
                r["id"]
                for r in raw_tasks
                if r["state"] == "active"
                and r["phase"] in {"ready", "starting"}
                and deadlines[r["id"]] > now
            }
            active = defaultdict(set)
            pending = Counter()
            for task in raw_tasks:
                if task["id"] in live_task_ids:
                    for aid, status in all_items[task["id"]]:
                        pending[aid] += status == "not_attempted"
                        if status in {"not_attempted", "executing"}:
                            active[aid].add(task["id"])
            accounts = []
            raw_by_id = {a["id"]: a for a in raw_accounts}
            for a in raw_accounts:
                h = json.loads(a["health"])
                issue, severity = account_issue(a, h, now, active[a["id"]])
                accounts.append(
                    {
                        "id": digest(a["id"]),
                        "label": clean(a["label"]),
                        "platform": a["platform"],
                        "issue": issue,
                        "severity": severity,
                        "checked_at": a["checked_at"] or 0,
                        "cooldown_until": a["cooldown_until"],
                        "tasks": len(active[a["id"]]),
                        "pending": pending[a["id"]],
                        "failures": number(h.get("consecutive_failures")),
                        "verified": h.get("healthy") is True and h.get("identity_verified") is True,
                    }
                )
            projected_accounts = {a["id"]: a for a in accounts}
            stopped = controls_stopped(db)
            tasks = []
            for task in raw_tasks:
                items = all_items[task["id"]]
                counts = dict(Counter(s for _, s in items))
                policy = policies[task["id"]]
                aids = [a for a in json.loads(task["account_ids"]) if a in raw_by_id]
                account_lines = []
                for aid in aids:
                    c = Counter(s for a, s in items if a == aid)
                    account_lines.append(
                        f"{clean(raw_by_id[aid]['label'])}："
                        + "，".join(f"{STATES[s]} {n}" for s, n in sorted(c.items()))
                    )
                eligibility = [
                    max(
                        due,
                        raw_by_id[aid]["next_at"],
                        raw_by_id[aid]["cooldown_until"],
                        task["next_at"] if policy.get("pacing_scope", "task") == "task" else 0,
                    )
                    for aid, due in pending_due[task["id"]]
                    if aid in raw_by_id
                ]
                next_at = min(eligibility) if eligibility else 0
                if policy.get("starts_at"):
                    next_at = max(next_at, epoch(policy["starts_at"])) if eligibility else 0
                waiting = "按固定清单处理并核验回执"
                if task["state"] == "paused":
                    waiting = "用户已暂停任务"
                elif task["state"] == "cancelled":
                    waiting = "任务已取消"
                elif counts.get("unknown") or counts.get("scheduled"):
                    waiting = "仍有排程或未知结果待核验"
                elif next_at > now:
                    waiting = "等待计划间隔或冷却"
                elif counts.get("manual_attention"):
                    waiting = "等待人工处理"
                elif not task["manifest_hash"]:
                    waiting = "准备固定清单" if task["phase"] == "starting" else "等待明确启动"
                problems = [
                    projected_accounts[digest(a)]["issue"]
                    for a in aids
                    if projected_accounts[digest(a)]["issue"] not in {"healthy", "unverified"}
                ]
                if problems and task["state"] == "active":
                    waiting = "；".join(dict.fromkeys(REASONS[p] for p in problems))
                if task["state"] == "active" and stopped:
                    waiting = "全局执行已停止"
                elif deadlines[task["id"]] and deadlines[task["id"]] <= now:
                    waiting = "执行窗口已结束；未决结果只核对回执"
                tasks.append(
                    {
                        "key": digest(task["id"]),
                        "id": clean(task["id"], 60),
                        "name": clean(task["name"], 100),
                        "state": task["state"],
                        "phase": task["phase"],
                        "counts": counts,
                        "total": len(items),
                        "account_lines": account_lines,
                        "waiting": waiting,
                        "next_at": next_at,
                        "updated_at": task["updated_at"],
                        "started": task["state"] != "draft"
                        and (bool(task["manifest_hash"]) or task["phase"] == "starting"),
                        "deadline": deadlines[task["id"]],
                        "watch": deadlines[task["id"]] > now
                        or (
                            task["state"] in {"paused", "cancelled", "completed"}
                            and task["updated_at"] >= now - 14400
                        ),
                    }
                )
            reports = []
            if "statistical_reports" in tables:
                for r in db.execute(
                    "SELECT * FROM statistical_reports WHERE cutoff>=? ORDER BY cutoff,kind",
                    (now - 172800,),
                ):
                    if r["kind"] not in {"interval", "daily_20", "daily_final"}:
                        continue
                    content = json.loads(r["content"])
                    reports.append(
                        {
                            "kind": r["kind"],
                            "cutoff": r["cutoff"],
                            "revision": r["revision"],
                            "day": datetime.fromtimestamp(r["cutoff"] - 1, ZONE).date().isoformat(),
                            "daily": project_statistics(content["daily"]),
                            "period": project_statistics(content["period"]),
                        }
                    )
            since, _ = day_bounds(datetime.fromtimestamp(now, ZONE).date())
            today = project_statistics(Statistics(None, clock=self.clock)._snapshot(db, since, now))
            controls = {
                r[0]: r[1]
                for r in db.execute(
                    "SELECT key,value FROM meta WHERE key IN ('global_stop','execution_runtime')"
                )
            }
            return {
                "at": now,
                "accounts": accounts,
                "tasks": tasks,
                "reports": reports,
                "today": today,
                "global_stop": controls.get("global_stop") != "0",
            }


def controls_stopped(db):
    row = db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()
    return not row or row[0] != "0"
