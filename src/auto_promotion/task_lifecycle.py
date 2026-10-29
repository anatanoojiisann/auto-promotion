"""End finite execution batches without inventing or retrying delivery outcomes."""

import json

from .models import epoch
from .store import Store

EXECUTION_TERMINAL = frozenset(
    {
        "confirmed",
        "scheduled",
        "unknown",
        "failed_known",
        "manual_attention",
        "missed",
        "cancelled",
    }
)


def complete_batches(db, now, task_id=None):
    query = "SELECT * FROM tasks WHERE state='active'"
    args = ()
    if task_id:
        query += " AND id=?"
        args = (task_id,)
    for task in db.execute(query, args).fetchall():
        policy = json.loads(task["policy"])
        expired = now >= epoch(policy["ends_at"])
        rows = db.execute("SELECT status FROM items WHERE task_id=?", (task["id"],)).fetchall()
        if db.execute(
            "SELECT 1 FROM execution_jobs WHERE task_id=? AND state='leased' AND lease_until>?",
            (task["id"], now),
        ).fetchone():
            continue
        if any(r["status"] == "executing" for r in rows):
            continue
        if db.execute(
            "SELECT 1 FROM candidates WHERE task_id=? AND state='executing'", (task["id"],)
        ).fetchone():
            continue
        if expired:
            db.execute(
                "UPDATE items SET status='missed',result=? WHERE task_id=? "
                "AND status='not_attempted' AND attempt_id IS NULL",
                (json.dumps({"reason": "delivery_window_elapsed"}), task["id"]),
            )
        elif (
            task["phase"] != "ready"
            or not rows
            or any(r["status"] not in EXECUTION_TERMINAL for r in rows)
        ):
            continue
        pending = db.execute(
            "SELECT status,count(*) n FROM items WHERE task_id=? "
            "AND status IN ('unknown','scheduled','manual_attention') GROUP BY status",
            (task["id"],),
        ).fetchall()
        details = {
            "reason": "execution_window_expired" if expired else "execution_finished",
            "receipt_pending": {r["status"]: r["n"] for r in pending},
            "attempts_changed": False,
        }
        db.execute(
            "UPDATE tasks SET state='completed',updated_at=? WHERE id=? AND state='active'",
            (now, task["id"]),
        )
        Store.audit(db, "task.execution_finished", details, task["id"], now)


def receipt_summary(items):
    return {
        "execution_finished": bool(items) and all(i["status"] in EXECUTION_TERMINAL for i in items),
        "pending_verification": sum(i["status"] in {"unknown", "scheduled"} for i in items),
        "manual_attention": sum(i["status"] == "manual_attention" for i in items),
    }
