"""Command-line entry point shared by source checkouts and installed wheels."""

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from .adapters import AdapterRegistry
from .config import database_path, load_environment, workspace_path
from .core import Engine
from .legacy import import_accounts
from .models import PLATFORMS, AccountInput
from .statistics import ZONE, Statistics
from .store import Store
from .task_tool_models import TemplateSave, TemplateTask
from .task_tools import TaskTools
from .workers import WorkerRun


def main():
    load_environment(Path.cwd() / ".env")
    parser = argparse.ArgumentParser(description="Auto Promotion control plane")
    parser.add_argument("--db", default=str(database_path()))
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8780)
    serve.add_argument("--no-worker", action="store_true")
    serve.add_argument(
        "--execution-mode",
        choices=("legacy", "pooled"),
        default=os.getenv("AP_EXECUTION_MODE", "pooled"),
    )
    callback = sub.add_parser(
        "tiktok-callback",
        help="Serve only TikTok OAuth callbacks and signed webhooks behind an HTTPS proxy",
    )
    callback.add_argument("--port", type=int, default=8781)
    serve.add_argument(
        "--reports-only", action="store_true", help="Run local reporting without outbound execution"
    )
    sub.add_parser("init")
    sub.add_parser("worker-once")
    pool = sub.add_parser("pool-worker", help="Run one persistent platform/connection executor")
    pool.add_argument("--pool", required=True)
    pool.add_argument("--node-id", required=True)
    pool.add_argument("--idle-exit", type=int, default=30)
    sub.add_parser("pools-status", help="Read persistent queue and per-pool worker health")
    channels = sub.add_parser(
        "execution-channels", help="Configure cloud-first, quota-only local fallback"
    )
    channels.add_argument("--enable", action="store_true")
    channels.add_argument(
        "--local-today",
        action="store_true",
        help="Use the independent local queue until midnight in Asia/Shanghai",
    )
    channels.add_argument("--account", action="append", default=[])
    channels.add_argument(
        "--local-through-task",
        action="append",
        default=[],
        help="Continue the existing local channel through these already-started task windows",
    )
    channels.add_argument(
        "--binding",
        action="store_true",
        help="Create/read this database's non-secret deployment binding",
    )
    sub.add_parser("cloud-status", help="Read Cloudflare queue and executor health")
    sub.add_parser(
        "cloud-sync", help="Persist current task snapshots and quota ledger to Cloudflare"
    )
    provision = sub.add_parser(
        "cloud-bootstrap",
        help="Register this scoped executor and persist its private configuration",
    )
    provision.add_argument("--url", required=True)
    provision.add_argument("--node-id", required=True)
    provision.add_argument("--operator-token-file", required=True)
    provision.add_argument("--account", action="append")
    provision.add_argument(
        "--adopt-local-policy",
        action="store_true",
        help="Copy current stop/live flags and limits without increasing them",
    )
    sub.add_parser("template-list", help="List reusable task settings")
    template = sub.add_parser("template-save", help="Save a task's reusable settings")
    template.add_argument("--task", required=True)
    template.add_argument("--name", required=True)
    template.add_argument("--description", default="")
    reuse = sub.add_parser("task-from-template", help="Create a fresh, unapproved draft")
    reuse.add_argument("--template", required=True)
    reuse.add_argument("--name", required=True)
    reuse.add_argument("--account", action="append", required=True)
    reuse.add_argument("--starts", required=True)
    reuse.add_argument("--ends", required=True)
    reuse.add_argument("--target", action="append", default=[])
    reuse.add_argument("--channel", choices=["inherit", "cloud", "local"])
    export = sub.add_parser("task-export", help="Export task evidence without platform calls")
    export.add_argument("--task", required=True)
    export.add_argument("--format", choices=("zip", "json", "html"), default="zip")
    export.add_argument("--output", type=Path, required=True)
    sub.add_parser("collect-statistics", help="Read Telegram delivery and replies; never publishes")
    statistics = sub.add_parser("statistics", help="Read all live-task interaction statistics")
    statistics.add_argument("--day", help="UTC+8 date, YYYY-MM-DD (defaults to today)")
    statistics.add_argument("--all", action="store_true", help="Include all recorded dates")
    sub.add_parser(
        "report-once", help="Save due reports and reconcile late local evidence; no platform calls"
    )
    checks = sub.add_parser("check-accounts")
    checks.add_argument("--live-only", action="store_true")
    sub.add_parser("seed-simulation")
    sub.add_parser("import-workspace-accounts")
    login = sub.add_parser("login", help="Configure a private local platform account")
    login.add_argument("platform", choices=PLATFORMS)
    login.add_argument("--account", required=True, help="Stable local account label")
    login.add_argument(
        "--browser-channel", choices=["chromium", "msedge", "chrome"], default="chromium"
    )
    sub.add_parser("doctor", help="Check this interpreter and the bundled backends")
    args = parser.parse_args()
    load_environment(Path(args.db).resolve().parent / "cloud-node.env")
    os.environ["AP_DB"] = str(Path(args.db).resolve())
    if args.command == "doctor":
        from .diagnostics import diagnose

        print(json.dumps(diagnose(), ensure_ascii=False, indent=2))
        return
    if args.command == "tiktok-callback":
        import uvicorn

        from .tiktok_gateway import create_gateway

        callback_engine = Engine(
            Store(args.db), AdapterRegistry(workspace_path()), live_enabled=False
        )
        uvicorn.run(
            create_gateway(callback_engine), host="127.0.0.1", port=args.port, access_log=False
        )
        return
    if args.command == "login":
        if args.platform == "tiktok":
            print(
                json.dumps(
                    {
                        "authenticated": False,
                        "error": "TikTokOAuthUseWorkbench",
                        "next_step": "Open the local workbench account center and choose Connect TikTok.",
                    }
                )
            )
            raise SystemExit(2)
        from .backends.common import BackendError, account_lock

        try:
            with account_lock(args.platform, args.account):
                if args.platform == "telegram":
                    from .backends.telegram import login as login_telegram

                    result = asyncio.run(login_telegram(args.account))
                elif args.platform == "instagram":
                    from .backends.instagram import configure

                    result = configure(args.account)
                elif args.platform == "discord":
                    from .backends.discord import configure

                    result = configure(args.account)
                else:
                    from .backends.browser import login as login_browser

                    result = login_browser(
                        args.platform, args.account, browser_channel=args.browser_channel
                    )
        except (Exception, KeyboardInterrupt) as exc:
            # Login errors may contain phone numbers, codes, cookies or tokens.
            code = exc.safe_code if isinstance(exc, BackendError) else type(exc).__name__
            print(json.dumps({"authenticated": False, "error": code}))
            raise SystemExit(1) from None
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(
            create_app(
                db_path=args.db,
                worker=not (args.no_worker or args.reports_only),
                report_worker=args.reports_only or not args.no_worker,
                pooled_worker=args.execution_mode == "pooled",
                statistics_only=args.reports_only,
            ),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
        )
        return
    store = Store(args.db)
    if args.command in {"statistics", "report-once"}:
        reporting = Statistics(store)
        try:
            result = (
                reporting.run_due()
                if args.command == "report-once"
                else reporting.snapshot(
                    day=None if args.all else args.day or datetime.now(ZONE).date()
                )
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    engine = Engine(
        store,
        AdapterRegistry(workspace_path()),
        live_enabled=os.getenv("AUTO_PROMOTION_LIVE_WRITES") == "1",
    )
    if args.command == "execution-channels":
        from .cloud_control import CloudControl, CloudUnavailable
        from .execution_channels import (
            binding,
            check_standby,
            configure_independent,
            enabled,
            read_state,
        )

        cloud = None if args.local_today or args.local_through_task else CloudControl.configured()
        try:
            if args.local_today and args.local_through_task:
                raise ValueError("ChooseOneLocalChannelWindow")
            if args.local_through_task:
                from .models import epoch

                tasks = [engine.task(task_id) for task_id in args.local_through_task]
                configure_independent(
                    engine,
                    args.account,
                    max(epoch(task["policy"]["ends_at"]) for task in tasks),
                    through_task_ids=args.local_through_task,
                )
            if args.local_today:
                from datetime import timedelta
                from zoneinfo import ZoneInfo

                now = datetime.fromtimestamp(engine.clock(), ZoneInfo("Asia/Shanghai"))
                until = datetime.combine(
                    now.date() + timedelta(days=1), datetime.min.time(), tzinfo=now.tzinfo
                ).timestamp()
                configure_independent(engine, args.account, until)
            if args.enable:
                try:
                    check_standby(engine, cloud)
                except CloudUnavailable as exc:
                    if str(exc) not in {
                        "CloudWriteQuotaExceeded",
                        "LocalChannelCheckpointRequired",
                    }:
                        raise
                    # Arm the policy now; the first trusted checkpoint is still
                    # mandatory before any local write can execute.
                    health = cloud.request("/health", None, method="GET")
                    if health.get("fallback") != {
                        "node_id": cloud.node_id,
                        "store_id": binding(engine),
                    }:
                        raise CloudUnavailable("LocalChannelBindingMismatch") from None
                with store.connect(write=True) as db:
                    db.execute(
                        "INSERT INTO meta VALUES('execution_channel_policy','cloud_then_local') "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                    )
                    Store.audit(db, "execution.channels_enabled", {"policy": "cloud_then_local"})
            result = {
                "policy": "cloud_then_local" if enabled(store) else "cloud_only",
                "channel": read_state(store),
                "checkpoint_saved": bool(
                    store.one("SELECT value FROM meta WHERE key='cloud_standby_permit'")
                ),
            }
            if args.binding:
                result["deployment_binding"] = {
                    "FALLBACK_NODE_ID": cloud.node_id if cloud else None,
                    "FALLBACK_STORE_ID": binding(engine),
                }
        except (CloudUnavailable, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}))
            raise SystemExit(2) from None
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.command in {"cloud-bootstrap", "cloud-sync", "cloud-status"}:
        from .cloud_admin import bootstrap, configured
        from .cloud_control import CloudUnavailable

        try:
            if args.command == "cloud-bootstrap":
                result = bootstrap(
                    engine,
                    url=args.url,
                    node_id=args.node_id,
                    operator_file=args.operator_token_file,
                    account_ids=args.account,
                    adopt_local=args.adopt_local_policy,
                )
            elif args.command == "cloud-sync":
                result = configured().sync(engine)
            else:
                result = configured().request("/v1/status", operator=True, method="GET")
        except (CloudUnavailable, OSError, ValueError) as exc:
            code = str(exc) if isinstance(exc, CloudUnavailable) else type(exc).__name__
            print(json.dumps({"ok": False, "error": code}))
            raise SystemExit(2) from None
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.command == "pool-worker":
        from .pool_runtime import run_pool

        result = run_pool(engine, args.pool, args.node_id, idle_exit=args.idle_exit)
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.command == "pools-status":
        from .queue_store import JobQueue

        print(json.dumps(JobQueue(engine).pools(), ensure_ascii=False))
        return
    if args.command in {"template-list", "template-save", "task-from-template", "task-export"}:
        from pydantic import ValidationError

        from .core import Conflict

        task_tools = TaskTools(engine)
        try:
            if args.command == "template-list":
                result = {"templates": task_tools.templates()}
            elif args.command == "template-save":
                result = task_tools.save_template(
                    TemplateSave(
                        source_task_id=args.task, name=args.name, description=args.description
                    )
                )
            elif args.command == "task-from-template":
                result = task_tools.create_from_template(
                    args.template,
                    TemplateTask(
                        name=args.name,
                        account_ids=args.account,
                        starts_at=args.starts,
                        ends_at=args.ends,
                        target_allowlist=args.target,
                        execution_channel=args.channel,
                    ),
                )
            else:
                data = task_tools.export(args.task, args.format)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("xb") as target:
                    target.write(data)
                result = {
                    "task_id": args.task,
                    "format": args.format,
                    "path": str(args.output.resolve()),
                    "bytes": len(data),
                }
        except (Conflict, ValidationError, ValueError, OSError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "init":
        result = {
            "initialized": True,
            "global_stop": store.one("SELECT value FROM meta WHERE key='global_stop'")["value"]
            == "1",
        }
    elif args.command == "seed-simulation":
        present = {a["platform"] for a in engine.accounts() if a["mode"] == "simulation"}
        for platform in PLATFORMS:
            if platform not in present:
                a = engine.add_account(
                    AccountInput(
                        platform=platform,
                        label=platform.title() + " · 模拟账号",
                        backend_label="simulation-" + platform,
                        mode="simulation",
                        min_interval=0,
                    )
                )
                engine.check_account(a["id"])
        result = {
            "simulation_accounts": sum(a["mode"] == "simulation" for a in engine.accounts()),
            "external_actions": 0,
        }
    elif args.command == "import-workspace-accounts":
        result = {
            "imported": import_accounts(engine, workspace_path()),
            "source": "historical_task_account_labels_only",
            "external_actions": 0,
        }
    elif args.command == "worker-once":
        with WorkerRun(engine.store, live_enabled=engine.live_enabled) as sender:
            sender.phase("executing")
            result = engine.run_cycle()
    elif args.command == "collect-statistics":
        result = engine.collect_statistics()
    else:
        accounts = [a for a in engine.accounts() if not args.live_only or a["mode"] == "live"]
        result = []
        for account in accounts:
            checked = engine.check_account(account["id"])
            result.append(
                {key: checked[key] for key in ("id", "platform", "label", "status", "health")}
            )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
