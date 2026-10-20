"""Exact, versioned native final receipts. Legacy progress is never final proof."""

import contextlib
import json
import math
import sqlite3
from pathlib import Path

from .backends.common import BackendError, profile_path
from .models import digest

SCOPE = "native_execute_claim_v1"


def valid_result(result, platform, item):
    if not isinstance(result, dict):
        return False
    if result.get("status") == "failed_known":
        return True
    # Scheduling needs its own queue-ID/active lookup contract; not recovered here.
    return bool(
        result.get("status") == "confirmed"
        and result.get("verified") is True
        and isinstance(result.get("external_reference"), str)
        and result["external_reference"].strip()
        and (
            platform != "x"
            or item.get("action") in {"like", "repost"}
            or result.get("target_verified") is True
        )
    )


def binding(db, claim, account_id, platform, label, job_id):
    """Compute from execution-time durable rows, also used for exact revalidation."""
    if not isinstance(claim, dict) or not isinstance(claim.get("payload"), dict):
        return None
    account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    item = db.execute("SELECT * FROM items WHERE id=?", (claim.get("item_id"),)).fetchone()
    task = db.execute("SELECT * FROM tasks WHERE id=?", (claim.get("task_id"),)).fetchone()
    job = db.execute("SELECT * FROM execution_jobs WHERE id=?", (job_id,)).fetchone()
    attempt = db.execute("SELECT * FROM attempts WHERE id=?", (claim.get("attempt_id"),)).fetchone()
    if not all((account, item, task, job, attempt)) or not account["identity_hash"]:
        return None
    if not (
        account["platform"] == platform
        and account["backend_label"] == label
        and item["account_id"] == account_id == attempt["account_id"] == job["account_id"]
        and item["task_id"] == task["id"] == attempt["task_id"] == job["task_id"]
        and item["attempt_id"] == attempt["id"]
        and attempt["item_id"] == item["id"]
        and job["kind"] == "send"
        and job["reference_id"] == item["id"]
        and item["payload_hash"] == job["version"] == digest(claim.get("payload"))
        and json.loads(item["payload"]) == claim.get("payload")
        and task["manifest_hash"]
        and task["manifest_hash"] == claim.get("manifest_hash")
    ):
        return None
    return {
        "version": 1,
        "hash_scope": SCOPE,
        "task_id": task["id"],
        "item_id": item["id"],
        "attempt_id": attempt["id"],
        "account_id": account_id,
        "account_mode": account["mode"],
        "identity_hash": account["identity_hash"],
        "platform": platform,
        "label_hash": digest(label),
        "manifest_hash": task["manifest_hash"],
        "request_hash": digest(claim),
        "job_id": job["id"],
        "job_version": job["version"],
    }


def capture(request):
    if request["operation"] != "execute" or not request.get("database"):
        return None
    with contextlib.closing(
        sqlite3.connect(
            Path(request["database"]).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2
        )
    ) as db:
        db.row_factory = sqlite3.Row
        return binding(
            db,
            request["payload"],
            request.get("account_id"),
            request["platform"],
            request["account"],
            (request.get("execution_job") or {}).get("id"),
        )


def proof(store, row):
    """Only read the explicitly bound account's receipt, outside any business write lock."""
    try:
        with store.connect() as db:
            account = db.execute(
                "SELECT * FROM accounts WHERE id=?", (row["account_id"],)
            ).fetchone()
            task = db.execute(
                "SELECT manifest_hash FROM tasks WHERE id=?", (row["task_id"],)
            ).fetchone()
            if not account or not task:
                return None
            claim = {
                "item_id": row["id"],
                "task_id": row["task_id"],
                "attempt_id": row["attempt_id"],
                "payload": json.loads(row["payload"]),
                "manifest_hash": task["manifest_hash"],
            }
            expected = binding(
                db,
                claim,
                account["id"],
                account["platform"],
                account["backend_label"],
                "send:" + row["id"],
            )
        if expected is None:
            return None
        path = profile_path(account["platform"], account["backend_label"]) / "receipts.sqlite"
        with contextlib.closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
        ) as db:
            db.execute("PRAGMA query_only=ON")
            saved = db.execute(
                "SELECT payload_hash,result FROM receipts WHERE id=?", ("execute:" + row["id"],)
            ).fetchone()
        if not saved or saved[0] != digest(claim):
            return None
        receipt = json.loads(saved[1])
        if not isinstance(receipt, dict):
            return None
        finalized = receipt.get("receipt_finalized_at")
        result = receipt.get("receipt_final_result")
        actual_binding = receipt.get("receipt_binding")
        if not (
            isinstance(actual_binding, dict)
            and type(actual_binding.get("version")) is int
            and actual_binding == expected
            and type(finalized) in (int, float)
            and math.isfinite(finalized)
            and row["claimed_at"] <= finalized
            and isinstance(result, dict)
            and receipt.get("receipt_final_hash")
            == digest({"binding": expected, "result": result, "finalized_at": finalized})
            and valid_result(result, account["platform"], claim["payload"])
        ):
            return None
        return claim, expected, result
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, BackendError):
        # Missing/legacy/corrupt/unavailable evidence can never grant another send.
        return None
