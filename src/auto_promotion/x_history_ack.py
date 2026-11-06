"""Explicit, target-bound acknowledgement of unrelated historical X outcomes.

This does not change an old outcome, authorize a retry, or grant account ownership.
"""

import json

from .interaction_intents import event_id, intent_blocker
from .models import canonical, digest, epoch
from .store import Store


def _key(task_id, account_id):
    return f"x_history_ack:{task_id}:{account_id}"


def _scope(db, task_id, account_id, now, *, creating=False):
    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not task or not account or account["platform"] != "x" or account["mode"] != "live":
        raise ValueError("XHistoryAcknowledgementScopeInvalid")
    policy = json.loads(task["policy"])
    if (
        task["state"] != "active"
        or task["mode"] != "live"
        or task["phase"] != "ready"
        or not task["manifest_hash"]
        or policy.get("execution_channel") != "local"
        or account_id not in json.loads(task["account_ids"])
        or epoch(policy["ends_at"]) <= now
        or not account["identity_hash"]
    ):
        raise ValueError("XHistoryAcknowledgementScopeInvalid")
    items = db.execute(
        "SELECT * FROM items WHERE task_id=? AND account_id=? ORDER BY id", (task_id, account_id)
    ).fetchall()
    if not items:
        raise ValueError("XHistoryAcknowledgementTargetsRequired")
    targets = []
    for item in items:
        payload = json.loads(item["payload"])
        source = event_id("x", payload)
        if (
            not source
            or payload.get("execution_mode") == "manual"
            or digest(payload) != item["payload_hash"]
            or (creating and (item["status"] != "not_attempted" or item["attempt_id"]))
            or item["status"] not in {"not_attempted", "executing", "confirmed"}
            or (creating and intent_blocker(db, account, task, payload, item["id"]))
        ):
            raise ValueError("XHistoryAcknowledgementTargetUnsafe")
        targets.append(
            {"item_id": item["id"], "payload_hash": item["payload_hash"], "event": source}
        )
    if len({t["event"] for t in targets}) != len(targets):
        raise ValueError("XHistoryAcknowledgementTargetUnsafe")
    aliases = [account_id] + [
        r[0]
        for r in db.execute(
            "SELECT id FROM accounts WHERE platform='x' AND identity_hash=? AND id<>? ORDER BY id",
            (account["identity_hash"], account_id),
        )
    ]
    history = []
    for alias in aliases:
        for old in db.execute(
            "SELECT i.id,i.task_id,i.payload,i.payload_hash,i.attempt_id,i.claimed_at,"
            "t.state task_state,t.created_at task_created FROM items i JOIN tasks t ON t.id=i.task_id "
            "WHERE i.account_id=? AND i.status='unknown' ORDER BY i.id",
            (alias,),
        ):
            payload = json.loads(old["payload"])
            action = payload.get("action")
            source = event_id("x", payload)
            if (
                old["task_id"] == task_id
                or digest(payload) != old["payload_hash"]
                or old["task_state"] not in {"paused", "cancelled", "completed"}
                or old["task_created"] > task["created_at"]
                or (action == "reply" and (not source or source in {t["event"] for t in targets}))
                or action not in {"reply", "post"}
            ):
                raise ValueError("XHistoryAcknowledgementHistoryUnsafe")
            history.append(
                {
                    "item_id": old["id"],
                    "task_id": old["task_id"],
                    "account_id": alias,
                    "payload_hash": old["payload_hash"],
                    "attempt_id": old["attempt_id"],
                    "action": action,
                    "event": source,
                }
            )
    return {
        "version": 1,
        "task_id": task_id,
        "account_id": account_id,
        "identity_hash": account["identity_hash"],
        "manifest_hash": task["manifest_hash"],
        "until": epoch(policy["ends_at"]),
        "targets": targets,
        "history": history,
    }


def record_ack(engine, task_id, account_id, *, source):
    if not isinstance(source, str) or not source.strip() or len(source) > 500:
        raise ValueError("ExplicitXHistoryAcknowledgementRequired")
    now = engine.clock()
    with engine.store.connect(write=True) as db:
        scope = _scope(db, task_id, account_id, now, creating=True)
        if not scope["history"]:
            raise ValueError("XHistoryAcknowledgementNotNeeded")
        value = {**scope, "source": source.strip(), "acknowledged_at": now}
        key = _key(task_id, account_id)
        existing = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if existing:
            prior = json.loads(existing[0])
            if {k: v for k, v in prior.items() if k not in {"source", "acknowledged_at"}} != scope:
                raise ValueError("XHistoryAcknowledgementChanged")
            return {
                "task_id": task_id,
                "account_id": account_id,
                "history_count": len(scope["history"]),
                "unchanged": True,
            }
        db.execute("INSERT INTO meta VALUES(?,?)", (key, canonical(value)))
        Store.audit(
            db,
            "x.unrelated_history_acknowledged",
            {
                "account_id": account_id,
                "manifest_hash": scope["manifest_hash"],
                "target_item_ids": [t["item_id"] for t in scope["targets"]],
                "history_item_ids": [h["item_id"] for h in scope["history"]],
                "history_digest": digest(scope["history"]),
                "source": source.strip(),
                "old_outcomes_changed": False,
                "old_targets_retry_allowed": False,
            },
            task_id,
            now,
        )
    return {
        "task_id": task_id,
        "account_id": account_id,
        "history_count": len(scope["history"]),
        "unchanged": False,
    }


def allowed_unknown_ids(db, task_id, account_id, now):
    """Return the still-valid historical IDs; None means absent or invalid."""
    saved = db.execute(
        "SELECT value FROM meta WHERE key=?", (_key(task_id, account_id),)
    ).fetchone()
    if not saved:
        return None
    try:
        value = json.loads(saved[0])
        current = _scope(db, task_id, account_id, now)
        if not isinstance(value.get("source"), str) or not value["source"].strip():
            return None
        for key in (
            "version",
            "task_id",
            "account_id",
            "identity_hash",
            "manifest_hash",
            "until",
            "targets",
        ):
            if value.get(key) != current[key]:
                return None
        prior = {h["item_id"]: h for h in value["history"]}
        if any(prior.get(h["item_id"]) != h for h in current["history"]):
            return None
        return {h["item_id"] for h in current["history"]}
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def has_ack(db, task_id, account_id):
    return bool(
        db.execute("SELECT 1 FROM meta WHERE key=?", (_key(task_id, account_id),)).fetchone()
    )
