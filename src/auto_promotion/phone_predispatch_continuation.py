"""Scoped continuation past a provably pre-dispatch Instagram phone failure.

The old result remains unknown. This only permits one different, named item;
the facts are checked again under the claiming transaction.
"""

import json
import math
import sqlite3
import time

from .models import canonical, digest
from .store import Store


def _facts(db, old_id, new_id, now):
    from .meta_browser import event_key

    if old_id == new_id:
        return None
    old = db.execute(
        "SELECT i.*,t.state task_state,t.policy task_policy FROM items i "
        "JOIN tasks t ON t.id=i.task_id WHERE i.id=?",
        (old_id,),
    ).fetchone()
    new = db.execute(
        "SELECT i.*,t.state task_state,t.policy task_policy FROM items i "
        "JOIN tasks t ON t.id=i.task_id WHERE i.id=?",
        (new_id,),
    ).fetchone()
    if not old or not new or old["account_id"] != new["account_id"]:
        return None
    account = db.execute(
        "SELECT platform FROM accounts WHERE id=?", (old["account_id"],)
    ).fetchone()
    if not account or account["platform"] != "instagram":
        return None
    old_payload, new_payload = json.loads(old["payload"]), json.loads(new["payload"])
    old_policy, new_policy = json.loads(old["task_policy"]), json.loads(new["task_policy"])
    result = json.loads(old["result"] or "{}")
    if (
        old["status"] != "unknown"
        or old["task_state"] != "completed"
        or old["external_reference"] is not None
        or not old["attempt_id"]
        or result.get("reason") != "transport_outcome_unknown"
        or result.get("submission_started") is True
        or new["status"] != "not_attempted"
        or new["attempt_id"] is not None
        or new["claimed_at"] is not None
        or new["external_reference"] is not None
        or new["task_state"] not in {"draft", "active", "paused"}
        or old["task_id"] == new["task_id"]
        or old_policy.get("execution_method") != "phone"
        or new_policy.get("execution_method") != "phone"
        or old_policy.get("execution_channel") != "local"
        or new_policy.get("execution_channel") != "local"
        or old_payload.get("action") != "comment"
        or new_payload.get("action") != "comment"
        or digest(old_payload) != old["payload_hash"]
        or digest(new_payload) != new["payload_hash"]
    ):
        return None
    old_event = event_key("instagram", old_payload["target"], old_payload)
    new_event = event_key("instagram", new_payload["target"], new_payload)
    if old_event == new_event:
        return None
    if (
        db.execute("SELECT count(*) FROM items WHERE task_id=?", (old["task_id"],)).fetchone()[0]
        != 1
    ):
        return None
    attempts = db.execute("SELECT * FROM attempts WHERE item_id=?", (old_id,)).fetchall()
    if len(attempts) != 1:
        return None
    attempt = attempts[0]
    if (
        attempt["id"] != old["attempt_id"]
        or attempt["outcome"] != "unknown"
        or attempt["finished_at"] is None
        or attempt["finished_at"] > now
    ):
        return None
    calls = db.execute(
        "SELECT id,status,error_code FROM calls WHERE task_id=? AND platform='instagram' "
        "AND operation='execute' AND at>=? AND at<=?",
        (old["task_id"], attempt["started_at"], attempt["finished_at"]),
    ).fetchall()
    if (
        len(calls) != 1
        or calls[0]["status"] != "error"
        or calls[0]["error_code"] != "PhoneFleetDeviceNotReady"
    ):
        return None
    if db.execute("SELECT 1 FROM meta_browser_claims WHERE item_id=?", (old_id,)).fetchone():
        return None
    auths = db.execute(
        "SELECT * FROM phone_social_authorizations WHERE item_id=?", (old_id,)
    ).fetchall()
    if len(auths) != 1:
        return None
    auth = auths[0]
    if (
        auth["used_at"] is not None
        or auth["native_id"] is not None
        or auth["execution_id"] is not None
        or auth["expires_at"] > now
    ):
        return None
    if db.execute(
        "SELECT 1 FROM phone_social_operations WHERE kind='social_submit' AND (item_id=? OR id=?)",
        (old_id, auth["id"]),
    ).fetchone():
        return None
    claim, context = json.loads(auth["claim"]), json.loads(auth["context"])
    claimed = claim.get("payload") or {}
    if (
        claim.get("account_id") != old["account_id"]
        or claim.get("platform") != "instagram"
        or claimed.get("item_id") != old_id
        or claimed.get("task_id") != old["task_id"]
        or claimed.get("attempt_id") != old["attempt_id"]
        or context.get("platform") != "instagram"
        or context.get("action") != "comment"
        or context.get("target_url") != old_payload.get("target")
        or context.get("text") != old_payload.get("text")
    ):
        return None
    if db.execute(
        "SELECT 1 FROM execution_jobs WHERE task_id=? AND "
        "(state NOT IN ('unknown','cancelled','failed_known') OR owner IS NOT NULL OR lease_until>?)",
        (old["task_id"], now),
    ).fetchone():
        return None
    return {
        "version": 1,
        "account_id": old["account_id"],
        "old_item_id": old_id,
        "old_task_id": old["task_id"],
        "old_payload_hash": old["payload_hash"],
        "old_attempt_id": old["attempt_id"],
        "authorization_id": auth["id"],
        "authorization_expires_at": auth["expires_at"],
        "error_call_id": calls[0]["id"],
        "old_event": old_event,
        "new_item_id": new_id,
        "new_task_id": new["task_id"],
        "new_payload_hash": new["payload_hash"],
        "new_event": new_event,
    }


def record_scope(store, old_id, new_id, instruction, *, now=None):
    """Record a user-requested, short-lived scope without modifying either item."""
    now = time.time() if now is None else now
    if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 2000:
        raise ValueError("ExplicitInstructionRequired")
    if not math.isfinite(now):
        raise ValueError("InvalidTime")
    with store.connect(write=True) as db:
        facts = _facts(db, old_id, new_id, now)
        if not facts:
            raise ValueError("PhonePredispatchEvidenceRequired")
        marker = {
            **facts,
            "instruction": instruction.strip(),
            "created_at": now,
            "expires_at": now + 1200,
        }
        key = "phone_predispatch_continuation:" + old_id + ":" + new_id
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, canonical(marker)))
        Store.audit(
            db, "phone_social.scoped_predispatch_continuation", marker, facts["new_task_id"], now
        )
    return marker


def allowed(db, old_id, new_id, account_id, payload, now):
    """Fail closed on changed evidence, scope, permission use or any other unknown."""
    try:
        row = db.execute(
            "SELECT value FROM meta WHERE key=?",
            ("phone_predispatch_continuation:" + old_id + ":" + new_id,),
        ).fetchone()
        if not row:
            return False
        marker = json.loads(row["value"])
        created, expires = marker.get("created_at"), marker.get("expires_at")
        if (
            type(created) not in (int, float)
            or type(expires) not in (int, float)
            or not math.isfinite(created)
            or not math.isfinite(expires)
            or not created <= now <= expires
            or not 0 < expires - created <= 1200
            or not isinstance(marker.get("instruction"), str)
            or not marker["instruction"].strip()
            or marker.get("account_id") != account_id
            or marker.get("new_payload_hash") != digest(payload)
        ):
            return False
        facts = _facts(db, old_id, new_id, now)
        return bool(facts and all(marker.get(k) == v for k, v in facts.items()))
    except (sqlite3.Error, TypeError, ValueError, KeyError, AttributeError):
        return False
