"""Read-only checks before replacing an unattempted desktop continuation.

These checks only decide whether a short-lived delivery plan can be prepared.
They never acquire a job, change account ownership, or authorize a submission.
The existing queue, channel router, browser guard and receipt checks still apply.
"""

import json
import math
import os
import socket

from .cloud_control import CloudControl, CloudUnavailable, task_scope_health
from .task_channels import ack_matches, binding_matches, channel_binding, original_fence_valid


def _account_blocker(db, account_id, now):
    stop = db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()
    if not stop or stop[0] != "0":
        return "global_stop"
    if db.execute(
        "SELECT 1 FROM execution_jobs WHERE account_id=? AND state='leased' AND lease_until>? LIMIT 1",
        (account_id, now),
    ).fetchone():
        return "account_lease_active"
    if db.execute(
        "SELECT 1 FROM items WHERE account_id=? AND status IN ('executing','unknown','scheduled') LIMIT 1",
        (account_id,),
    ).fetchone():
        return "account_result_unresolved"
    if db.execute(
        "SELECT 1 FROM cloud_receipt_outbox r LEFT JOIN execution_jobs j ON j.id=r.job_id "
        "WHERE json_extract(r.lease,'$.account_id')=? OR j.account_id=? LIMIT 1",
        (account_id, account_id),
    ).fetchone():
        return "account_receipt_pending"
    return None


def _local_task_scope(db, account_id, task_id):
    task = db.execute("SELECT policy,account_ids FROM tasks WHERE id=?", (task_id,)).fetchone()
    return bool(
        task
        and account_id in json.loads(task["account_ids"])
        and json.loads(task["policy"]).get("execution_channel") == "local"
    )


def replacement_blocker(engine, account_id, item_id, *, task_id=None):
    """Reject replacement unless the old item is durably known to be unsubmitted."""
    with engine.store.connect() as db:
        item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not item or item["account_id"] != account_id:
            return "prior_item_missing"
        if task_id is not None and item["task_id"] != task_id:
            return "prior_item_task_mismatch"
        if (
            item["status"] not in {"missed", "not_attempted"}
            or item["attempt_id"]
            or item["claimed_at"]
            or item["external_reference"]
        ):
            return "prior_item_not_proven_unsubmitted"
        try:
            result = json.loads(item["result"] or "{}")
        except (TypeError, ValueError):
            return "prior_item_not_proven_unsubmitted"
        if not isinstance(result, dict) or result.get("submission_started") is True:
            return "prior_item_not_proven_unsubmitted"
        if item["status"] == "missed" and result.get("reason") != "delivery_window_elapsed":
            return "prior_item_not_proven_unsubmitted"
        if db.execute("SELECT 1 FROM attempts WHERE item_id=?", (item_id,)).fetchone():
            return "prior_attempt_exists"
        if db.execute("SELECT 1 FROM meta_browser_claims WHERE item_id=?", (item_id,)).fetchone():
            return "prior_browser_claim_exists"
        if db.execute(
            "SELECT 1 FROM execution_jobs j JOIN task_channel_claims c ON c.job_id=j.id "
            "WHERE j.reference_id=? LIMIT 1",
            (item_id,),
        ).fetchone():
            return "prior_channel_claim_exists"
        if db.execute(
            "SELECT 1 FROM execution_jobs WHERE reference_id=? AND state='leased' LIMIT 1",
            (item_id,),
        ).fetchone():
            return "prior_job_lease_exists"
        if db.execute(
            "SELECT 1 FROM cloud_receipt_outbox r JOIN execution_jobs j ON j.id=r.job_id "
            "WHERE j.reference_id=? LIMIT 1",
            (item_id,),
        ).fetchone():
            return "prior_receipt_pending"
    return None


def local_handoff_preflight(engine, account_id, task_id, *, required_until, replacement=True):
    """Coalesce ownership checks without claiming a job or refreshing a snapshot.

    A cloud-backed fence must also match one fresh, authenticated server response.
    When reusing a task, its task-scoped sync observation must be fresh. A new
    replacement instead needs its own sync check after creation and before begin;
    an expired old task cannot produce that observation. An independently scoped
    local fence is checked locally and does not contact the cloud.
    """
    now = engine.clock()
    if (
        not isinstance(required_until, (int, float))
        or not math.isfinite(required_until)
        or required_until <= now
    ):
        return {"ready": False, "reason": "invalid_execution_window"}
    with engine.store.connect() as db:
        row = db.execute(
            "SELECT * FROM account_task_channels WHERE account_id=?", (account_id,)
        ).fetchone()
        store = db.execute("SELECT value FROM meta WHERE key='execution_store_id'").fetchone()
        blocker = _account_blocker(db, account_id, now)
        if blocker:
            return {"ready": False, "reason": blocker}
        if not _local_task_scope(db, account_id, task_id):
            return {"ready": False, "reason": "local_task_scope_mismatch"}
        if not row or row["channel"] != "local" or row["state"] != "ready":
            return {"ready": False, "reason": "local_handoff_pending"}
        if row["error"] or row["retry_at"] > now:
            return {"ready": False, "reason": "local_handoff_error"}
        if row["until_at"] < required_until:
            return {"ready": False, "reason": "local_authority_window_too_short"}
        bound = channel_binding(db, account_id)
        node = os.getenv("AP_NODE_ID", socket.gethostname())
        if not store or not binding_matches(row, bound, node, store[0]):
            return {"ready": False, "reason": "local_authority_binding_mismatch"}
        if bound.get("fence_source") != row["fence_source"] or not original_fence_valid(
            db, bound, now, task_id
        ):
            return {"ready": False, "reason": "local_authority_unverified"}
        # A replacement has a new task id, so an old task-limited independent
        # grant cannot be silently extended to that replacement.
        if bound.get("fence_source") == "legacy":
            state_row = db.execute(
                "SELECT value FROM meta WHERE key='execution_channel'"
            ).fetchone()
            state = json.loads(state_row[0]) if state_row else {}
            if state.get("offline_only") is not True:
                return {"ready": False, "reason": "local_independent_isolation_unverified"}
            if replacement and state.get("through_task_ids"):
                return {"ready": False, "reason": "local_authority_task_scope_limited"}
        expected = dict(bound)
        source = bound["fence_source"]

    if source == "cloud_acknowledged":
        cloud = CloudControl.configured()
        if not cloud or cloud.node_id != expected["node_id"]:
            return {"ready": False, "reason": "cloud_binding_unavailable"}
        if not replacement:
            health = task_scope_health(engine, task_id, cloud=cloud)
            if not health.get("valid_for_task") or not health.get("execution_ready"):
                return {"ready": False, "reason": "task_sync_observation_stale"}
        try:
            observed = cloud.request("/v1/account-channel/status", {"account_id": account_id}).get(
                "channel"
            )
        except CloudUnavailable:
            return {"ready": False, "reason": "server_channel_unavailable"}
        if not ack_matches(observed, expected):
            return {"ready": False, "reason": "server_channel_mismatch"}
    elif source not in {"legacy", "standalone"}:
        return {"ready": False, "reason": "local_authority_unverified"}

    # A concurrent local handoff invalidates the just-read server evidence.
    with engine.store.connect() as db:
        current = db.execute(
            "SELECT * FROM account_task_channels WHERE account_id=?", (account_id,)
        ).fetchone()
        if (
            not current
            or current["state"] != "ready"
            or current["error"]
            or current["retry_at"] > engine.clock()
            or current["until_at"] < required_until
            or not binding_matches(current, expected, expected["node_id"], expected["store_id"])
            or channel_binding(db, account_id) != expected
            or not original_fence_valid(db, expected, engine.clock(), task_id)
            or not _local_task_scope(db, account_id, task_id)
        ):
            return {"ready": False, "reason": "local_authority_changed"}
        if source == "legacy":
            state_row = db.execute(
                "SELECT value FROM meta WHERE key='execution_channel'"
            ).fetchone()
            state = json.loads(state_row[0]) if state_row else {}
            if state.get("offline_only") is not True or (
                replacement and state.get("through_task_ids")
            ):
                return {"ready": False, "reason": "local_authority_changed"}
        blocker = _account_blocker(db, account_id, engine.clock())
        if blocker:
            return {"ready": False, "reason": blocker}
    if source == "cloud_acknowledged" and not replacement:
        health = task_scope_health(engine, task_id, cloud=cloud)
        if not health.get("valid_for_task") or not health.get("execution_ready"):
            return {"ready": False, "reason": "task_sync_observation_stale"}
    return {
        "ready": True,
        "reason": None,
        "fence_source": source,
        "until_at": expected["until_at"],
        "new_task_scope_check_required": bool(replacement),
    }
