"""Read-only import of public task metadata, never credential tables."""

import json
import sqlite3
from pathlib import Path


def inventory(workspace):
    root = Path(workspace)
    sources = [
        ("telegram", root / "telegram_social_tasks.db", "social_tasks"),
        ("reddit", root / "reddit-engagement-workflow/data/redsignal.db", "workflow_tasks"),
        ("reddit", root / "redsignal.db", "workflow_tasks"),
        ("x", root / "x-engagement/var/x-engagement.db", "workflow_tasks"),
        ("instagram", root / "meta-social-engagement/var/meta-social.db", "tasks"),
    ]
    result = []
    for platform, path, table in sources:
        if not path.is_file():
            continue
        try:
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            db.row_factory = sqlite3.Row
            try:
                allowed = {r[1] for r in db.execute(f"PRAGMA table_info({table})")} & {
                    "task_id",
                    "id",
                    "name",
                    "status",
                    "state",
                    "created_at",
                    "account",
                    "account_id",
                }
                if not allowed:
                    continue
                columns = ",".join(sorted(allowed))
                rows = db.execute(f"SELECT {columns} FROM {table} LIMIT 200").fetchall()
                for row in rows:
                    r = dict(row)
                    result.append(
                        {
                            "platform": platform,
                            "task_id": r.get("task_id", r.get("id")),
                            "name": r.get("name", "历史任务"),
                            "status": r.get("status", r.get("state", "unknown")),
                            "source": str(path.relative_to(root)),
                            "execution_owner": "legacy",
                            "read_only": True,
                        }
                    )
            finally:
                db.close()
        except sqlite3.Error:
            continue
    return result


def import_accounts(engine, workspace):
    import re

    from .models import AccountInput

    found = {}
    for pattern, platform in [
        ("telegram_task_*.json", "telegram"),
        ("reddit_outbound_manifest_*.json", "reddit"),
        ("x_thread_task_*.json", "x"),
        ("instagram_outbound_execution_*.json", "instagram"),
    ]:
        for path in sorted(workspace.glob(pattern)):
            try:
                value = json.loads(path.read_text())
            except (ValueError, OSError):
                continue
            raw = value.get("account") or value.get("account_id")
            if not isinstance(raw, str):
                continue
            label = value.get("account_label") or raw
            if platform == "x":
                match = re.search(r"@([A-Za-z0-9_]{1,15})", raw)
                if not match:
                    continue
                raw = match.group(1)
            elif platform == "reddit":
                match = re.search(r"u/([A-Za-z0-9_-]{3,30})", raw)
                if not match:
                    continue
                raw = match.group(1)
                label = raw
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", raw):
                continue
            found[(platform, raw)] = str(label)[:80]
    count = 0
    existing = {
        (a["platform"], a["backend_label"]) for a in engine.accounts() if a["mode"] == "live"
    }
    for (platform, backend), label in found.items():
        if (platform, backend) not in existing:
            engine.add_account(AccountInput(platform=platform, label=label, backend_label=backend))
            count += 1
    return count
