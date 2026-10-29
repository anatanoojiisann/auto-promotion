"""Durable read-only retry policy, shared by old and newly created statistics slots."""

import json
import math

from .models import canonical


def retry_key(item_id):
    return "statistics_retry:" + item_id


def pending_until(db, item_id):
    row = db.execute("SELECT value FROM meta WHERE key=?", (retry_key(item_id),)).fetchone()
    return json.loads(row[0]).get("until", 0) if row else 0


def settle_read(db, item_id, result, complete, now):
    key = retry_key(item_id)
    if complete:
        db.execute("DELETE FROM meta WHERE key=?", (key,))
        return now
    if result.get("status") == "partial":
        return now + 1
    old = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    previous = json.loads(old[0]) if old else {}
    reason = result.get("reason") or result.get("status") or "read_unavailable"
    count = min(10, previous.get("failures", 0) + 1) if previous.get("reason") == reason else 1
    base = (
        3600
        if reason in {"no_durable_receipt", "feedback_parent_unavailable", "ChannelPrivateError"}
        else 300
    )
    seconds = min(21600, base * 2 ** (count - 1))
    until = now + seconds
    for field in ("read_rpc_budget_resets_at", "account_retry_at"):
        stamp = result.get(field)
        if type(stamp) in {int, float} and math.isfinite(stamp):
            until = max(until, stamp)
    delay = result.get("read_retry_after_seconds") or result.get("retry_after_seconds")
    if type(delay) in {int, float} and math.isfinite(delay) and delay > 0:
        until = max(until, now + delay)
    db.execute(
        "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, canonical({"until": until, "failures": count, "reason": reason})),
    )
    return until
