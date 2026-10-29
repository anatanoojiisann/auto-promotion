"""Scoped, backed-up enrollment of the two explicitly authorized Facebook accounts."""

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from auto_promotion.adapters import AdapterRegistry
from auto_promotion.config import database_path, load_environment, workspace_path
from auto_promotion.core import Engine
from auto_promotion.meta_browser import MetaBrowser
from auto_promotion.meta_migration import register
from auto_promotion.store import Store

load_environment(".env")
database = database_path()
output = Path("outputs/facebook-formal-20261026").resolve()
output.mkdir(parents=True, exist_ok=True)
report_path = output / "migration-report.json"
if report_path.exists():
    raise SystemExit("MigrationReportAlreadyExists: inspect prior outcome before repeating")
backup = output / "before-migration.db"
if backup.exists():
    raise SystemExit("BackupAlreadyExists: inspect prior outcome before repeating")
with (
    sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db,
    sqlite3.connect(backup) as copy,
):
    db.backup(copy)
policy = json.loads(
    (database.parent / "completion-cycle-policy.json").read_text(encoding="utf-8-sig")
)
identities = {"facebook:100024447714168", "facebook:61595674742646"}
registrations = [
    r
    for r in policy["external_accounts"]
    if r["account_id"] in identities and r["platform"] == "facebook"
]
assert {r["account_id"] for r in registrations} == identities
sources = {
    str(Path(raw).resolve()) for r in registrations for raw in r["details"]["workflow_paths"]
}


def source_hashes():
    return {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in sources}


def snapshot(path):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return {
            "counts": {
                t: db.execute("SELECT count(*) FROM " + t).fetchone()[0]
                for t in ("accounts", "tasks", "items", "attempts", "execution_jobs")
            },
            "flags": dict(
                db.execute(
                    "SELECT key,value FROM meta WHERE key IN ('global_stop','cloud_control_required','execution_runtime')"
                )
            ),
            "tasks": {
                r["id"]: hashlib.sha256(json.dumps(dict(r), sort_keys=True).encode()).hexdigest()
                for r in db.execute("SELECT * FROM tasks")
            },
            "items": {
                r["id"]: hashlib.sha256(json.dumps(dict(r), sort_keys=True).encode()).hexdigest()
                for r in db.execute("SELECT * FROM items")
            },
        }


before = snapshot(backup)
hashes = source_hashes()
engine = Engine(Store(database), AdapterRegistry(workspace_path()), live_enabled=False)
browser = MetaBrowser(engine)
ids = register(browser, registrations)
first = snapshot(database)
assert first["counts"]["attempts"] == before["counts"]["attempts"], "Unexpected new attempt"
assert first["counts"]["execution_jobs"] == before["counts"]["execution_jobs"], (
    "Unexpected execution job"
)
assert first["flags"] == before["flags"], "Existing execution switches changed"
assert source_hashes() == hashes, "Original history file changed"
changed = {
    table: [key for key, value in before[table].items() if first[table].get(key) != value]
    for table in ("tasks", "items")
}
assert not any(changed.values()), "Preexisting task or item changed during migration"
register(browser, registrations)
after = snapshot(database)
assert after["counts"] == first["counts"], "Non-idempotent enrollment"
report = {
    "at": time.time(),
    "scope": sorted(identities),
    "database": str(database),
    "backup": str(backup),
    "before": before["counts"],
    "after": after["counts"],
    "account_ids": ids,
    "flags_preserved": before["flags"] == after["flags"],
    "original_task_and_item_rows_preserved": not any(changed.values()),
    "source_hashes": hashes,
    "source_files_unchanged": source_hashes() == hashes,
    "idempotency_verified": True,
    "new_attempts": after["counts"]["attempts"] - before["counts"]["attempts"],
    "new_execution_jobs": after["counts"]["execution_jobs"] - before["counts"]["execution_jobs"],
    "accounts": browser.status()["accounts"],
    "history": engine.store.rows(
        "SELECT a.label,i.status,count(*) count FROM meta_browser_history h JOIN items i ON i.id=h.item_id JOIN accounts a ON a.id=i.account_id WHERE a.platform='facebook' GROUP BY a.label,i.status"
    ),
    "imported_tasks": engine.store.rows(
        "SELECT t.id,t.state FROM tasks t JOIN meta_browser_sources s ON s.task_id=t.id"
    ),
    "execution_started": False,
}
report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(
    json.dumps(
        {
            k: report[k]
            for k in (
                "account_ids",
                "before",
                "after",
                "flags_preserved",
                "source_files_unchanged",
                "idempotency_verified",
                "new_attempts",
                "new_execution_jobs",
                "history",
                "execution_started",
            )
        },
        ensure_ascii=False,
    )
)
