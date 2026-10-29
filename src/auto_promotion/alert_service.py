"""Independent account/task broadcasts. No platform adapters or campaign writes."""

import argparse
import json
import os
import time
from pathlib import Path

from .alert_plan_queues import describe_plan, load_plan
from .alert_relay import AlertRelay, AlertSettings
from .alert_sources import AlertSources, read_database
from .alert_store import AlertStore
from .alert_summary import SUMMARY_FORMAT, ThreeHourSummaryRules, summary_notice
from .alert_templates import when
from .config import database_path, load_environment


class AlertService:
    def __init__(self, db_path, state_path=None, *, settings=None, relay=None, clock=time.time):
        self.clock = clock
        self.settings = settings or AlertSettings.environment()
        path = Path(state_path) if state_path else Path(db_path).parent / "alerts.db"
        if path.resolve() == Path(db_path).resolve():
            raise ValueError("AlertDatabaseMustBeSeparate")
        self.store = AlertStore(path, clock=clock)
        self.sources = AlertSources(
            db_path, clock=clock, summary_only=True, plan_sources=self.store.planned_queue_sources
        )
        self.rules = ThreeHourSummaryRules(self.store)
        self.relay = relay or AlertRelay(self.settings)

    def register_plan_queue(self, path):
        root = self.sources.path.parent
        source = describe_plan(path, root)
        with read_database(self.sources.path) as db:
            load_plan(db, source, root)
        with self.store.connect(write=True) as db:
            sources = self.store.meta(db, "planned_queue_sources", [])
            sources = [
                s for s in sources if s["id"] != source["id"] and s["path"] != source["path"]
            ]
            sources.append(source)
            if len(sources) > 50:
                raise ValueError("PlannedQueueSourceLimit")
            self.store.set_meta(db, "planned_queue_sources", sources)
        return {"registered": True, "source": source["id"], "platform": "tiktok"}

    def collect(self):
        try:
            sample = self.sources.collect()
            self.rules.observe(sample)
            return True
        except Exception:
            with self.store.connect(write=True) as db:
                self.store.set_meta(db, "last_collection_error", "source_collection_failed")
            return False

    def heartbeat(self):
        with self.store.connect(write=True) as db:
            self.store.set_meta(
                db,
                "runtime",
                {
                    "heartbeat_at": self.clock(),
                    "pid": os.getpid(),
                    "poll_seconds": self.settings.poll_seconds,
                    "enabled": self.settings.enabled,
                    "key_configured": bool(self.settings.key),
                    "summary_format": SUMMARY_FORMAT,
                },
            )

    def drain(self, limit=5, notice_id=None):
        if not self.settings.enabled or not self.settings.key:
            return 0
        count = 0
        for _ in range(limit):
            row = self.store.claim(notice_id)
            if not row:
                break
            try:
                result = self.relay.send(row["text"])
            except Exception:
                result = {"state": "unknown", "reason": "relay_outcome_unknown"}
            self.store.settle(row, result)
            count += 1
        return count

    def status(self):
        status = self.store.status()
        runtime = status.get("runtime", {}) if status["service_state"] == "running" else {}
        return {
            "enabled": runtime.get("enabled", self.settings.enabled),
            "key_configured": runtime.get("key_configured", bool(self.settings.key)),
            **status,
        }


def main():
    load_environment(Path.cwd() / ".env")
    parser = argparse.ArgumentParser(
        description="Read-only campaign monitoring and Hala broadcasts"
    )
    parser.add_argument("--db", default=str(database_path()))
    parser.add_argument("--state-db")
    parser.add_argument("--env-file", help="Private file; only ALERT_RELAY_KEY is read")
    parser.add_argument("--enable", action="store_true", help="Enable Hala notifications only")
    parser.add_argument(
        "--plan-file", type=Path, help="Register a private reviewed reply-plan.json for counts only"
    )
    parser.add_argument(
        "command",
        choices=("preview", "once", "serve", "status", "test", "resume", "register-queue"),
    )
    args = parser.parse_args()
    try:
        settings = AlertSettings.environment(args.env_file, enable=args.enable)
        service = AlertService(args.db, args.state_db, settings=settings)
    except (ValueError, OSError):
        print(json.dumps({"error": "alert_configuration_unavailable"}))
        return 2
    if args.command == "preview":
        sample = service.sources.collect()
        print(summary_notice(sample["summary"]))
        return 0
    if args.command == "register-queue":
        if args.plan_file is None:
            parser.error("register-queue requires --plan-file")
        try:
            result = service.register_plan_queue(args.plan_file)
        except Exception:
            print(json.dumps({"error": "planned_queue_registration_failed"}))
            return 2
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == "resume":
        with service.store.connect(write=True) as db:
            service.store.set_meta(db, "authorization_blocked", False)
    elif args.command == "test":
        if not settings.enabled or not settings.key:
            print(json.dumps({"error": "alerts_not_enabled"}))
            return 2
        with service.store.connect(write=True) as db:
            notice_id = service.store.enqueue(
                db,
                "explicit_test:" + str(service.clock()),
                "test",
                "【测试播报】Auto Promotion 通知接口验证。\n时间："
                + when(service.clock())
                + "\n本消息仅验证接口接收，不代表社交任务已经执行。",
            )
        service.drain(limit=1, notice_id=notice_id)
        with service.store.connect() as db:
            result = dict(
                db.execute(
                    "SELECT id,state,reason,http_status FROM alert_outbox WHERE id=?", (notice_id,)
                ).fetchone()
            )
        print(json.dumps({"test": result}, ensure_ascii=False))
        return 0 if result["state"] == "accepted" else 1
    elif args.command in {"once", "serve"}:
        if args.command == "serve" and (not settings.enabled or not settings.key):
            print(json.dumps({"error": "alerts_not_enabled"}))
            return 2
        while True:
            if args.command == "serve":
                service.heartbeat()
            service.collect()
            service.drain()
            if args.command == "once":
                break
            time.sleep(settings.poll_seconds)
    print(json.dumps(service.status(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
