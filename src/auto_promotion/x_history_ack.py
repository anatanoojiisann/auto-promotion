"""Explicit, target-bound acknowledgement of unrelated historical X outcomes.

This does not change an old outcome, authorize a retry, or grant account ownership.
"""

import json

from .interaction_intents import event_id, intent_blocker
from .models import canonical, digest, epoch
from .store import Store


def _key(task_id, account_id):
    return f"x_history_ack:{task_id}:{account_id}"


def _proven_unsent_terminal(db, item):
    """Keep an existing target scope valid without reviving terminal items."""
    if item["external_reference"]:
        return False
    try:
        result = json.loads(item["result"] or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(result, dict):
        return False
    attempts = db.execute(
        "SELECT id,outcome,finished_at FROM attempts WHERE item_id=?", (item["id"],)
    ).fetchall()
    if item["status"] == "missed":
        return (
            result.get("reason") == "delivery_window_elapsed"
            and not item["attempt_id"]
            and not attempts
        )
    if item["status"] == "failed_known":
        return (
            result.get("status") == "failed_known"
            and result.get("verified") is False
            and result.get("confirmed_not_submitted") is True
            and result.get("submission_started") is False
            and len(attempts) == 1
            and attempts[0]["id"] == item["attempt_id"]
            and attempts[0]["outcome"] == "failed_known"
            and attempts[0]["finished_at"] is not None
        )
    return False


def _self_unknown_proof(db, item, now):
    """A settled unknown can be excluded, but can never become retryable."""
    if item["status"] != "unknown" or not item["attempt_id"] or item["external_reference"]:
        raise ValueError("XHistoryAcknowledgementSelfUnknownUnsafe")
    attempts = db.execute("SELECT * FROM attempts WHERE item_id=?", (item["id"],)).fetchall()
    jobs = db.execute(
        "SELECT * FROM execution_jobs WHERE reference_id=? AND kind='send'", (item["id"],)
    ).fetchall()
    if (
        len(attempts) != 1
        or attempts[0]["id"] != item["attempt_id"]
        or attempts[0]["account_id"] != item["account_id"]
        or attempts[0]["task_id"] != item["task_id"]
        or attempts[0]["outcome"] != "unknown"
        or attempts[0]["finished_at"] is None
        or len(jobs) != 1
        or jobs[0]["state"] != "unknown"
        or jobs[0]["owner"] is not None
        or jobs[0]["lease_until"] > now
        or db.execute(
            "SELECT 1 FROM cloud_receipt_outbox WHERE job_id=?", (jobs[0]["id"],)
        ).fetchone()
    ):
        raise ValueError("XHistoryAcknowledgementSelfUnknownUnsafe")
    return {
        "item_id": item["id"],
        "attempt_id": item["attempt_id"],
        "payload_hash": item["payload_hash"],
        "job_id": jobs[0]["id"],
    }


def _scope(db, task_id, account_id, now, *, creating=False, self_unknown_ids=()):
    self_unknown_ids = set(self_unknown_ids)
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
    targets, self_unknowns = [], []
    for item in items:
        payload = json.loads(item["payload"])
        source = event_id("x", payload)
        if (
            not source
            or payload.get("execution_mode") == "manual"
            or digest(payload) != item["payload_hash"]
            or (creating and (item["status"] != "not_attempted" or item["attempt_id"]))
            or (
                item["status"] not in {"not_attempted", "executing", "confirmed"}
                and not (item["status"] == "unknown" and item["id"] in self_unknown_ids)
                and (creating or not _proven_unsent_terminal(db, item))
            )
            or (creating and intent_blocker(db, account, task, payload, item["id"]))
        ):
            raise ValueError("XHistoryAcknowledgementTargetUnsafe")
        if item["id"] in self_unknown_ids:
            self_unknowns.append(_self_unknown_proof(db, item, now))
        targets.append(
            {"item_id": item["id"], "payload_hash": item["payload_hash"], "event": source}
        )
    if len({t["event"] for t in targets}) != len(targets):
        raise ValueError("XHistoryAcknowledgementTargetUnsafe")
    if {p["item_id"] for p in self_unknowns} != self_unknown_ids:
        raise ValueError("XHistoryAcknowledgementSelfUnknownUnsafe")
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
            self_unknown = old["task_id"] == task_id and old["id"] in self_unknown_ids
            if (
                (old["task_id"] == task_id and not self_unknown)
                or digest(payload) != old["payload_hash"]
                or (
                    not self_unknown
                    and old["task_state"] not in {"paused", "cancelled", "completed"}
                )
                or old["task_created"] > task["created_at"]
                or (
                    action == "reply"
                    and (
                        not source or (not self_unknown and source in {t["event"] for t in targets})
                    )
                )
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
    result = {
        "version": 2 if self_unknown_ids else 1,
        "task_id": task_id,
        "account_id": account_id,
        "identity_hash": account["identity_hash"],
        "manifest_hash": task["manifest_hash"],
        "until": epoch(policy["ends_at"]),
        "targets": targets,
        "history": history,
    }
    if self_unknown_ids:
        result["self_unknowns"] = self_unknowns
    return result


def record_self_unknown_exclusion(engine, task_id, account_id, item_id, *, source):
    """Exclude one explicitly identified unknown from a previously frozen task.

    This retains the original item/attempt/job and only permits distinct pending
    targets. It never changes an outcome, acquires ownership, or sends.
    """
    if not isinstance(source, str) or not source.strip() or len(source) > 500:
        raise ValueError("ExplicitXHistoryAcknowledgementRequired")
    now = engine.clock()
    with engine.store.connect(write=True) as db:
        key = _key(task_id, account_id)
        saved = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if not saved:
            raise ValueError("XHistoryAcknowledgementRequired")
        prior = json.loads(saved[0])
        if prior.get("version") == 2:
            if {p["item_id"] for p in prior.get("self_unknowns", [])} == {
                item_id
            } and allowed_unknown_ids(db, task_id, account_id, now) is not None:
                return {
                    "task_id": task_id,
                    "account_id": account_id,
                    "item_id": item_id,
                    "unchanged": True,
                }
            raise ValueError("XHistoryAcknowledgementChanged")
        if (
            prior.get("version") != 1
            or not isinstance(prior.get("source"), str)
            or not prior["source"].strip()
        ):
            raise ValueError("XHistoryAcknowledgementChanged")
        current = _scope(db, task_id, account_id, now, self_unknown_ids={item_id})
        for field in (
            "task_id",
            "account_id",
            "identity_hash",
            "manifest_hash",
            "until",
            "targets",
        ):
            if prior.get(field) != current[field]:
                raise ValueError("XHistoryAcknowledgementChanged")
        previous_history = {p["item_id"]: p for p in prior["history"]}
        new_history = [p for p in current["history"] if p["item_id"] == item_id]
        if len(new_history) != 1 or any(
            previous_history.get(p["item_id"]) != p
            for p in current["history"]
            if p["item_id"] != item_id
        ):
            raise ValueError("XHistoryAcknowledgementChanged")
        value = {
            **current,
            "history": [*prior["history"], *new_history],
            "source": source.strip(),
            "acknowledged_at": now,
            "prior_source": prior["source"],
            "prior_acknowledged_at": prior.get("acknowledged_at"),
        }
        db.execute("UPDATE meta SET value=? WHERE key=?", (canonical(value), key))
        Store.audit(
            db,
            "x.self_unknown_excluded",
            {
                "account_id": account_id,
                "item_id": item_id,
                "manifest_hash": current["manifest_hash"],
                "attempt_id": current["self_unknowns"][0]["attempt_id"],
                "source": source.strip(),
                "old_outcomes_changed": False,
                "old_targets_retry_allowed": False,
            },
            task_id,
            now,
        )
    return {"task_id": task_id, "account_id": account_id, "item_id": item_id, "unchanged": False}


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
        self_unknowns = value.get("self_unknowns", []) if value.get("version") == 2 else []
        current = _scope(
            db, task_id, account_id, now, self_unknown_ids={p["item_id"] for p in self_unknowns}
        )
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
        if value.get("self_unknowns", []) != current.get("self_unknowns", []):
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
