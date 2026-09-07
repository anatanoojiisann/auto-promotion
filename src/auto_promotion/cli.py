"""Command-line entry point shared by source checkouts and installed wheels."""

import argparse
import json
import os
from pathlib import Path

from .adapters import AdapterRegistry
from .config import database_path, load_environment, workspace_path
from .core import Engine
from .legacy import import_accounts
from .models import PLATFORMS, AccountInput
from .store import Store


def main():
    load_environment(Path.cwd() / ".env")
    parser = argparse.ArgumentParser(description="Auto Promotion control plane")
    parser.add_argument("--db", default=str(database_path()))
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8780)
    serve.add_argument("--no-worker", action="store_true")
    sub.add_parser("init")
    sub.add_parser("worker-once")
    checks = sub.add_parser("check-accounts")
    checks.add_argument("--live-only", action="store_true")
    sub.add_parser("seed-simulation")
    sub.add_parser("import-workspace-accounts")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(
            create_app(db_path=args.db, worker=not args.no_worker),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
        )
        return
    store = Store(args.db)
    engine = Engine(
        store,
        AdapterRegistry(workspace_path()),
        live_enabled=os.getenv("AUTO_PROMOTION_LIVE_WRITES") == "1",
    )
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
        result = engine.run_cycle()
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
