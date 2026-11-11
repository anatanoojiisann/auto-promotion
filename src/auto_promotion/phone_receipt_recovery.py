"""Persistent, bounded receipt follow-up for original phone submissions only."""

import json
import time

from httpx import HTTPError

from .backends.common import BackendError
from .models import canonical
from .store import Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_social_receipt_jobs(
 item_id TEXT PRIMARY KEY,execution_id TEXT NOT NULL,request_hash TEXT NOT NULL,
 check_id TEXT,state TEXT NOT NULL,expires_at REAL NOT NULL,next_at REAL NOT NULL,
 result TEXT NOT NULL DEFAULT '{}',generation INTEGER NOT NULL DEFAULT 0
);
"""


def followup(service, request, snapshot):
    from phone_fleet_protocol import fingerprint
    from phone_fleet_protocol.social import SocialReceipt, receipt_matches

    stop = service.store.one("SELECT value FROM meta WHERE key='global_stop'")
    if stop and stop["value"] == "1":
        return {
            "status": "unknown",
            "verified": False,
            "reason": "PhoneSocialReceiptPaused",
            "receipt_pending": False,
        }
    item_id = request.task_item_id
    original = snapshot.get("result") or {}
    social = original.get("social") or {}
    if (
        request.social.platform not in {"facebook", "instagram"}
        or snapshot.get("status") != "unknown"
        or original.get("simulated") is not False
        or social.get("submission_started") is not True
    ):
        return None
    auth = service.store.one(
        "SELECT * FROM phone_social_authorizations WHERE item_id=?", (item_id,)
    )
    digest = fingerprint(request)
    if (
        not auth
        or auth["used_at"] is None
        or not auth["native_id"]
        or auth["execution_id"] != snapshot.get("id")
        or auth["request_hash"] != digest
        or auth["device_id"] != request.device_id
    ):
        return {
            "status": "unknown",
            "verified": False,
            "reason": "PhoneSocialReceiptOriginalProofRequired",
        }
    now = time.time()
    with service.store.connect(write=True) as db:
        db.execute(
            "INSERT OR IGNORE INTO phone_social_receipt_jobs(item_id,execution_id,request_hash,state,expires_at,next_at) VALUES(?,?,?,'pending',?,?)",
            (item_id, snapshot["id"], digest, now + 300, now),
        )
        job = dict(
            db.execute(
                "SELECT * FROM phone_social_receipt_jobs WHERE item_id=?", (item_id,)
            ).fetchone()
        )
    if job["execution_id"] != snapshot["id"] or job["request_hash"] != digest:
        raise BackendError("PhoneSocialReceiptCorrelationMismatch")
    pending = {
        "status": "unknown",
        "verified": False,
        "submission_started": True,
        "reason": "PhoneSocialReceiptPending",
        "receipt_pending": True,
    }
    if job["state"] in {"paused", "cancel_pending", "cancelled"}:
        return {**pending, "reason": "PhoneSocialReceiptPaused", "receipt_pending": False}
    if job["state"] == "exhausted" or job["expires_at"] <= now:
        with service.store.connect(write=True) as db:
            db.execute(
                "UPDATE phone_social_receipt_jobs SET state='exhausted' WHERE item_id=?", (item_id,)
            )
        return {**pending, "reason": "PhoneSocialReceiptBudgetExhausted", "receipt_pending": False}
    if job["next_at"] > now and job["state"] != "confirmed":
        return pending
    origin = "/executions/" + snapshot["id"] + "/receipt-checks"
    if job["state"] == "confirmed":
        value = json.loads(job["result"])
    else:
        try:
            value = (
                service.request("GET", origin + "/" + job["check_id"])
                if job["check_id"]
                else service.request(
                    "POST",
                    origin,
                    {
                        "expected_revision": snapshot["revision"],
                        "request_key": fingerprint(["receipt", item_id, digest, job["generation"]]),
                    },
                )
            )
        except (BackendError, HTTPError) as error:
            with service.store.connect(write=True) as db:
                db.execute(
                    "UPDATE phone_social_receipt_jobs SET next_at=? WHERE item_id=? AND state='pending'",
                    (now + 15, item_id),
                )
                Store.audit(
                    db,
                    "phone_social.receipt_read_failed",
                    {"item_id": item_id, "error_type": type(error).__name__},
                    request.business_task_id,
                    now,
                )
            return {**pending, "reason": "PhoneSocialReceiptReadUnavailable"}
        with service.store.connect(write=True) as db:
            # A concurrent pause always wins over a completed network read.
            changed = db.execute(
                "UPDATE phone_social_receipt_jobs SET check_id=?,result=?,next_at=? WHERE item_id=? AND state='pending'",
                (value["id"], canonical(value), now + 5, item_id),
            ).rowcount
        if not changed:
            return {**pending, "reason": "PhoneSocialReceiptPaused", "receipt_pending": False}
    if value.get("execution_id") != snapshot["id"] or value.get("command_id") != snapshot.get(
        "command_id"
    ):
        raise BackendError("PhoneSocialReceiptCorrelationMismatch")
    if value.get("phase") != "confirmed":
        if value.get("phase") in {"exhausted", "cancelled"}:
            with service.store.connect(write=True) as db:
                db.execute(
                    "UPDATE phone_social_receipt_jobs SET state=? WHERE item_id=? AND state='pending'",
                    (value["phase"], item_id),
                )
        observations = value.get("observations") or []
        return {
            **pending,
            "receipt_pending": value.get("phase") == "pending",
            "reason": ("PhoneSocialReceipt:" + observations[-1]["reason"])
            if observations
            else "PhoneSocialReceiptPending",
        }
    proof = (value.get("observations") or [{}])[-1]
    receipt = SocialReceipt.model_validate(proof["receipt"]) if proof.get("receipt") else None
    if (
        proof.get("state") != "found"
        or not proof.get("baseline_sha256")
        or proof.get("execution_id") != snapshot["id"]
        or proof.get("command_id") != snapshot.get("command_id")
        or proof.get("device_id") != request.device_id
        or proof.get("node_id") != snapshot.get("node_id")
        or proof.get("command_sha256") != value.get("command_sha256")
        or not receipt_matches(receipt, request.social, social.get("submitted_at"))
    ):
        raise BackendError("PhoneSocialReceiptCorrelationMismatch")
    with service.store.connect(write=True) as db:
        current = db.execute(
            "SELECT state FROM phone_social_receipt_jobs WHERE item_id=?", (item_id,)
        ).fetchone()
        if not current or current["state"] not in {"pending", "confirmed"}:
            return {**pending, "reason": "PhoneSocialReceiptPaused", "receipt_pending": False}
        db.execute(
            "UPDATE phone_social_receipt_jobs SET state='confirmed',result=? WHERE item_id=? AND state='pending'",
            (canonical(value), item_id),
        )
        Store.audit(
            db,
            "phone_social.receipt_readback_verified",
            {"item_id": item_id, "execution_id": snapshot["id"], "native_id": receipt.native_id},
            request.business_task_id,
            now,
        )
    return {
        "status": "confirmed",
        "verified": True,
        "external_reference": receipt.permalink,
        "native_id": receipt.native_id,
        "comment_id": receipt.native_id,
        "native_comment_id": receipt.native_id,
        "published_at": receipt.created_at,
        "submission_started": True,
        "reconciliation_source": "phone_native_receipt",
        "original_runtime_status": "unknown",
    }


def pause(service, task_id=None):
    with service.store.connect(write=True) as db:
        clause = " AND item_id IN (SELECT id FROM items WHERE task_id=?)" if task_id else ""
        count = db.execute(
            "UPDATE phone_social_receipt_jobs SET state='cancel_pending',next_at=0 WHERE state='pending'"
            + clause,
            (task_id,) if task_id else (),
        ).rowcount
        if count:
            Store.audit(db, "phone_social.receipts_paused", {"count": count}, task_id, time.time())
    return count


def resume(service, item_id):
    stop = service.store.one("SELECT value FROM meta WHERE key='global_stop'")
    if stop and stop["value"] == "1":
        return False
    job = service.store.one("SELECT * FROM phone_social_receipt_jobs WHERE item_id=?", (item_id,))
    item = service.store.one("SELECT status,task_id FROM items WHERE id=?", (item_id,))
    if (
        not job
        or not item
        or item["status"] != "unknown"
        or job["state"] not in {"paused", "cancel_pending", "cancelled", "exhausted"}
    ):
        return False
    # Stop any remote check whose create response was lost before opening a new read generation.
    service.request("DELETE", "/executions/" + job["execution_id"] + "/receipt-checks")
    now = time.time()
    with service.store.connect(write=True) as db:
        changed = db.execute(
            "UPDATE phone_social_receipt_jobs SET state='pending',check_id=NULL,result='{}',generation=generation+1,expires_at=?,next_at=? WHERE item_id=? AND state=? AND generation=?",
            (now + 300, now, item_id, job["state"], job["generation"]),
        ).rowcount
        if changed:
            Store.audit(
                db,
                "phone_social.receipts_resumed",
                {"item_id": item_id, "read_only": True},
                item["task_id"],
                now,
            )
    return bool(changed)


def run_due(service):
    stop = service.store.one("SELECT value FROM meta WHERE key='global_stop'")
    if stop and stop["value"] == "1":
        pause(service)
    now = time.time()
    rows = service.store.rows(
        "SELECT j.*,i.task_id,t.state task_state FROM phone_social_receipt_jobs j JOIN items i ON i.id=j.item_id JOIN tasks t ON t.id=i.task_id WHERE j.state IN ('pending','cancel_pending') AND j.next_at<=? ORDER BY j.next_at LIMIT 8",
        (now,),
    )
    for row in rows:
        try:
            if row["state"] == "cancel_pending" or row["task_state"] in {"paused", "cancelled"}:
                service.request("DELETE", "/executions/" + row["execution_id"] + "/receipt-checks")
                with service.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE phone_social_receipt_jobs SET state='cancelled' WHERE item_id=?",
                        (row["item_id"],),
                    )
            else:
                service.engine.reconcile(row["item_id"], phone_receipt_lookup=True)
        except Exception as error:
            with service.store.connect(write=True) as db:
                db.execute(
                    "UPDATE phone_social_receipt_jobs SET next_at=? WHERE item_id=?",
                    (now + 15, row["item_id"]),
                )
                Store.audit(
                    db,
                    "phone_social.receipt_read_failed",
                    {"item_id": row["item_id"], "error_type": type(error).__name__},
                    row["task_id"],
                    now,
                )
    return {"processed": len(rows)}
