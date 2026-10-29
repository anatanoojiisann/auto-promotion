"""Back up, optionally stage, and enroll the already registered Meta workflows."""

import argparse
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.config import database_path, load_environment, workspace_path
from auto_promotion.core import Engine
from auto_promotion.meta_browser import MetaBrowser
from auto_promotion.meta_migration import import_existing
from auto_promotion.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", action="store_true")
    parser.add_argument("--cloud-catalog-pending", action="store_true")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    load_environment(".env")
    source = database_path()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / (
        "staging.db" if args.stage else "backup-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".db"
    )
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(target) as backup:
            original.backup(backup)
    before_counts = {}
    with sqlite3.connect(target) as before:
        for table in ("accounts", "tasks", "items", "attempts", "execution_jobs"):
            before_counts[table] = before.execute("SELECT count(*) FROM " + table).fetchone()[0]
        before_flags = dict(
            before.execute(
                "SELECT key,value FROM meta WHERE key IN ('global_stop','cloud_control_required')"
            )
        )
    database = target if args.stage else source
    if args.stage:
        (output / "completion-cycle-policy.json").write_bytes(
            (source.parent / "completion-cycle-policy.json").read_bytes()
        )
        os.environ["AP_ACCOUNTS_DIR"] = str(output / "accounts")
    engine = Engine(Store(database), AdapterRegistry(workspace_path()), live_enabled=False)
    if args.cloud_catalog_pending:
        with engine.store.connect(write=True) as db:
            db.execute(
                "INSERT INTO meta VALUES('meta_browser_cloud_catalog','pending_user_approval') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
    browser = MetaBrowser(engine)
    imported = import_existing(browser)
    after_counts = {
        table: engine.store.one("SELECT count(*) n FROM " + table)["n"] for table in before_counts
    }
    flags = {
        r["key"]: r["value"]
        for r in engine.store.rows(
            "SELECT key,value FROM meta WHERE key IN ('global_stop','cloud_control_required')"
        )
    }
    if (
        flags != before_flags
        or after_counts["attempts"] != before_counts["attempts"]
        or (after_counts["execution_jobs"] != before_counts["execution_jobs"])
    ):
        raise RuntimeError("MigrationUnexpectedExecutionMutation")
    report = {
        "stage": args.stage,
        "backup": str(target),
        "database": str(database),
        "before": before_counts,
        "after": after_counts,
        "flags_preserved": True,
        "new_attempts": 0,
        "new_execution_jobs": 0,
        "import": imported,
        "accounts": browser.status()["accounts"],
        "history": engine.store.rows(
            "SELECT a.platform,a.label,i.status,count(*) count FROM meta_browser_history h "
            "JOIN items i ON i.id=h.item_id JOIN accounts a ON a.id=i.account_id "
            "GROUP BY a.platform,a.label,i.status"
        ),
    }
    (output / ("staging-report.json" if args.stage else "migration-report.json")).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "stage",
                    "backup",
                    "before",
                    "after",
                    "flags_preserved",
                    "new_attempts",
                    "history",
                )
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
