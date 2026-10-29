"""Resolve publication times exclusively from verified, actual source publications."""

import json
import math

from .models import epoch


def publication_schedule(db, task, now):
    policy = json.loads(task["policy"])
    sources = policy.get("outbound_after_task_ids", [])
    if not sources:
        return None, None
    account_ids = json.loads(task["account_ids"])
    if len(account_ids) != 1:
        return None, "interaction_dependencies_invalid"
    if policy.get("outbound_dependency_deadline") and now >= epoch(
        policy["outbound_dependency_deadline"]
    ):
        return None, "interaction_dependency_deadline_elapsed"
    latest = None
    for source_id in sources:
        source = db.execute("SELECT * FROM tasks WHERE id=?", (source_id,)).fetchone()
        if (
            not source
            or source_id == task["id"]
            or source["mode"] != task["mode"]
            or account_ids[0] not in json.loads(source["account_ids"])
        ):
            return None, "interaction_dependencies_invalid"
        rows = db.execute(
            "SELECT status,result,external_reference FROM items WHERE task_id=? AND account_id=?",
            (source_id, account_ids[0]),
        ).fetchall()
        if source["state"] == "cancelled":
            return None, "interaction_dependencies_failed"
        if policy.get("outbound_dependency_mode") == "finished":
            if source["state"] != "completed":
                return None, "interaction_dependencies_pending"
            latest = max(latest or 0, source["updated_at"])
            continue
        if any(row["status"] in {"failed_known", "missed", "cancelled"} for row in rows):
            return None, "interaction_dependencies_failed"
        if not rows:
            return None, "interaction_dependencies_failed" if source[
                "state"
            ] == "completed" else "interaction_dependencies_pending"
        for row in rows:
            result = json.loads(row["result"])
            stamp = result.get("published_at")
            if (
                row["status"] != "confirmed"
                or result.get("verified") is not True
                or not (row["external_reference"] or result.get("external_reference"))
            ):
                return None, "interaction_dependencies_pending"
            try:
                stamp = epoch(stamp) if isinstance(stamp, str) else stamp
                if type(stamp) not in {int, float} or not math.isfinite(stamp) or stamp <= 0:
                    raise ValueError
            except (TypeError, ValueError, OverflowError):
                return None, "interaction_dependencies_pending"
            if stamp > now:
                return None, "interaction_dependencies_pending"
            latest = stamp if latest is None else max(latest, stamp)
    due = latest + policy["outbound_after_tasks_seconds"]
    if not epoch(policy["starts_at"]) <= due < epoch(policy["ends_at"]):
        return due, "interaction_schedule_outside_window"
    if now > due and (not task["manifest_hash"] or task["state"] != "active"):
        return due, "interaction_schedule_elapsed"
    if not task["manifest_hash"]:
        account = db.execute(
            "SELECT next_at,cooldown_until FROM accounts WHERE id=?", (account_ids[0],)
        ).fetchone()
        if account and max(account["next_at"], account["cooldown_until"]) > due:
            return due, "interaction_schedule_pacing_blocked"
    return due, None
