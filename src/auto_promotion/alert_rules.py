"""Persisted change detection and notification deduplication."""

from .alert_templates import REASONS, account_notice, report_notice, task_notice, when
from .models import digest


class AlertRules:
    def __init__(self, store, *, progress_seconds=1800):
        self.store, self.progress_seconds = store, progress_seconds

    def observe(self, sample):
        now, s = sample["at"], self.store
        with s.connect(write=True) as db:
            last = s.meta(db, "last_observed_at")
            if last is not None and now <= last:
                return
            baseline = last is None
            for account in sample["accounts"]:
                self.account(db, account, now, baseline)
            for task in sample["tasks"]:
                self.task(db, task, now, baseline)
            for report in sample["reports"]:
                self.report(db, report, now, baseline)
            stopped = sample["global_stop"]
            if baseline:
                problems = [a for a in sample["accounts"] if a["severity"] != "info"]
                lines = [
                    "【播报服务已启动】",
                    "时间：" + when(now),
                    f"监控真实账号 {len(sample['accounts'])} 个；历史任务已建立基线。",
                    f"当前有 {len(problems)} 个账号需关注：",
                ]
                lines.extend(f"{a['label']}：{REASONS[a['issue']]}" for a in problems[:12])
                if not problems:
                    lines[-1] = "当前没有明确的账号异常。"
                lines.append("数据来自已保存检查；后续播报新异常、恢复、任务变化和定时报表。")
                s.enqueue(db, "monitor_started:" + str(now), "startup", "\n".join(lines))
            elif stopped != s.meta(db, "global_stop"):
                s.enqueue(
                    db,
                    "global_stop:" + str(now),
                    "control",
                    "【执行状态变化】\n"
                    + (
                        "全局执行已停止。"
                        if stopped
                        else "全局停止已解除；任务仍按原有授权、冷却和额度执行。"
                    )
                    + "\n时间："
                    + when(now),
                )
            s.set_meta(db, "global_stop", stopped)
            s.set_meta(db, "last_observed_at", now)
            s.set_meta(db, "last_collection_error", None)

    def account(self, db, a, now, baseline):
        s, key = self.store, a["id"]
        old = s.snapshot(db, "account", key) or {}
        failure_since = old.get("failure_since", now) if old.get("issue") == a["issue"] else now
        s.save(db, "account", key, {**a, "failure_since": failure_since})
        row = db.execute("SELECT * FROM alert_incidents WHERE object_id=?", (key,)).fetchone()
        incident = dict(row) if row else None
        if a["issue"] == "healthy":
            if (
                incident
                and incident["active"]
                and a["verified"]
                and a["checked_at"] > incident["evidence_at"]
                and now - a["checked_at"] <= 300
            ):
                db.execute(
                    "UPDATE alert_outbox SET state='expired',reason='incident_recovered' WHERE event_key LIKE ? AND state='pending'",
                    (f"account:{key}:%",),
                )
                s.enqueue(
                    db,
                    f"recovery:{key}:{incident['opened_at']}",
                    "recovery",
                    account_notice(a, now, recovered=True, duration=now - incident["opened_at"]),
                )
                db.execute("UPDATE alert_incidents SET active=0 WHERE object_id=?", (key,))
            return
        if a["severity"] == "info":
            return
        if a["issue"] in {"temporary_unavailable", "navigation_blocked"} and (
            a["failures"] < 3 or now - failure_since < 120
        ):
            return
        if not incident or not incident["active"] or incident["issue"] != a["issue"]:
            db.execute(
                "INSERT OR REPLACE INTO alert_incidents VALUES(?,?,?,?,?,1)",
                (key, a["issue"], now, now, a["checked_at"]),
            )
            if not baseline:
                s.enqueue(db, f"account:{key}:{now}:initial", "account", account_notice(a, now))
            return
        if now - incident["last_notice"] >= 3600:
            unresolved = db.execute(
                "SELECT 1 FROM alert_outbox WHERE event_key LIKE ? AND state IN ('pending','sending','unknown') LIMIT 1",
                (f"account:{key}:{incident['opened_at']}:%",),
            ).fetchone()
            if not unresolved:
                s.enqueue(
                    db,
                    f"account:{key}:{incident['opened_at']}:{int(now // 3600)}",
                    "account",
                    account_notice(a, now),
                )
                db.execute("UPDATE alert_incidents SET last_notice=? WHERE object_id=?", (now, key))

    def task(self, db, t, now, baseline):
        s, key = self.store, t["key"]
        old = s.snapshot(db, "task", key)
        sig = digest([t[k] for k in ("counts", "state", "phase", "waiting")])
        total = t["total"]
        confirmed = t["counts"].get("confirmed", 0)
        ended = (
            total > 0
            and sum(
                t["counts"].get(k, 0) for k in ("confirmed", "failed_known", "missed", "cancelled")
            )
            == total
        )
        milestone = min(3, int(4 * confirmed / total)) if total >= 10 else 0
        previous = old or {
            "started": False,
            "state": "draft",
            "ended": False,
            "milestone": 0,
            "last_notice": now,
            "signature": sig,
        }
        title = None
        if not baseline and t["started"] and t["watch"]:
            if t["state"] != previous["state"] and t["state"] in {
                "paused",
                "cancelled",
                "completed",
            }:
                title = {
                    "paused": "任务已暂停",
                    "cancelled": "任务已取消",
                    "completed": "本批处理结束",
                }[t["state"]]
            elif ended and not previous["ended"]:
                title = "全部核验成功" if confirmed == total else "本批处理结束"
            elif not previous["started"]:
                title = "任务已启动"
            elif milestone > previous["milestone"]:
                title = "任务进度里程碑"
            elif (
                sig != previous["signature"]
                and now - previous["last_notice"] >= self.progress_seconds
            ):
                title = "任务进度"
        if title:
            db.execute(
                "UPDATE alert_outbox SET state='expired',reason='progress_superseded' WHERE event_key LIKE ? AND state='pending'",
                (f"task:{key}:%",),
            )
            s.enqueue(db, f"task:{key}:{now}:{sig}", "task", task_notice(t, now, title), ttl=7200)
        s.save(
            db,
            "task",
            key,
            {
                "started": t["started"],
                "state": t["state"],
                "ended": ended,
                "milestone": max(milestone, previous["milestone"]),
                "last_notice": now if title or baseline else previous["last_notice"],
                # Preserve the last broadcast fingerprint until a delayed change is sent.
                "signature": sig if title or baseline else previous["signature"],
            },
        )

    def report(self, db, report, now, baseline):
        s = self.store
        key = f"{report['kind']}:{report['cutoff']}"
        sig = digest([report["daily"], report["period"]])
        old = s.snapshot(db, "report", key)
        previous = old or {"sent_hash": sig, "last_notice": now}
        send = not baseline and (
            old is None or (sig != old["sent_hash"] and now - old["last_notice"] >= 1800)
        )
        midnight_interval = report["kind"] == "interval" and report["cutoff"] % 86400 == 57600
        if send and not midnight_interval:
            kind = report["kind"]
            interval_hash = digest(report["period"])
            if kind != "interval" or interval_hash != s.meta(db, "last_interval_hash"):
                text = report_notice(report)
                if old:
                    text = "【数据更正】晚到回执更新了以下报表。\n" + text
                due = max(now, report["cutoff"] + 300) if kind == "daily_final" else now
                db.execute(
                    "UPDATE alert_outbox SET state='expired',reason='report_superseded' WHERE event_key LIKE ? AND state='pending'",
                    (f"report:{key}:%",),
                )
                s.enqueue(db, f"report:{key}:{sig}", "report", text, due=due)
                s.set_meta(db, "last_interval_hash", interval_hash) if kind == "interval" else None
            previous = {"sent_hash": sig, "last_notice": now}
        s.save(db, "report", key, previous)
