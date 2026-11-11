"""Cheap local Meta blockers precede account handoff and any phone observation."""

import json


def blocker(db, job):
    if job["kind"] != "send" or job["pool"] not in {"facebook.phone", "instagram.phone"}:
        return None
    from .meta_browser import claim_blocker

    item = db.execute("SELECT * FROM items WHERE id=?", (job["reference_id"],)).fetchone()
    account = db.execute("SELECT * FROM accounts WHERE id=?", (job["account_id"],)).fetchone()
    if not item or not account:
        return "phone_item_scope_missing"
    return claim_blocker(
        db, dict(account), json.loads(item["payload"]), item["id"], job.get("checked_at", 0)
    )


def refresh_waits(db, now):
    rows = db.execute(
        "SELECT * FROM execution_jobs WHERE state='pending' AND json_extract(result,'$.waiting_for')='local_state_change' LIMIT 128"
    ).fetchall()
    for row in rows:
        reason = blocker(db, {**dict(row), "checked_at": now})
        if reason is None:
            db.execute(
                "UPDATE execution_jobs SET result='{}',available_at=?,updated_at=? WHERE id=? AND state='pending'",
                (now, now, row["id"]),
            )
