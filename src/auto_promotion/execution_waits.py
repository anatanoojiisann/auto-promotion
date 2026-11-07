"""Pre-submit control waits. Never requeue an attempted or uncertain submission."""

import json
import math
import os
from pathlib import Path

from .models import canonical, digest

CONFIGURATION_ERRORS = frozenset(
    {
        "CloudControlConfigurationMissing",
        "LocalChannelBindingMismatch",
        "LocalAuthorizationTaskScopeMismatch",
        "LocalPrimaryCloudTaskHeld",
        "LocalPrimaryAccountNotAuthorized",
    }
)


def control_revision(db, job):
    """Opaque scoped digest; credentials and unrelated account state are never returned."""
    keys = [
        "cloud_control_required",
        "execution_local_primary",
        "local_primary_account:" + job["account_id"],
        "execution_store_id",
        "execution_channel_policy",
        "account_channel_binding:" + job["account_id"],
        "task_channel_decision:" + job["task_id"],
    ]
    values = {
        k: (
            r[0]
            if (r := db.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone())
            else None
        )
        for k in keys
    }
    for key in ("execution_channel", "local_channel_authority"):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        value = json.loads(row[0]) if row else {}
        values[key] = (
            value
            if not value.get("account_ids") or job["account_id"] in value["account_ids"]
            else {}
        )
    row = db.execute(
        "SELECT channel,generation,until_at,state,fence_source FROM account_task_channels WHERE account_id=?",
        (job["account_id"],),
    ).fetchone()
    values["account_control"] = dict(row) if row else None
    values["version"] = job["version"]
    # Environment updates require a fresh worker. Token file replacement can be
    # noticed by a running worker without loading credential bytes into diagnostics.
    names = (
        "AP_CLOUD_CONTROL_URL",
        "AP_CLOUD_NODE_TOKEN",
        "AP_CLOUD_OPERATOR_TOKEN",
        "AP_NODE_ID",
        "AP_CLOUD_OPERATOR_TOKEN_FILE",
    )
    values["environment"] = {name: os.getenv(name) for name in names}
    filename = os.getenv("AP_CLOUD_OPERATOR_TOKEN_FILE")
    try:
        stat = Path(filename).stat() if filename else None
        values["token_revision"] = [stat.st_mtime_ns, stat.st_size] if stat else None
    except OSError:
        values["token_revision"] = None
    return digest(values)


def retry_result(db, job, error, now):
    if not getattr(error, "account_control_failure", False):
        return None
    from .task_channels import safe_channel_code

    code = safe_channel_code(str(error))
    old = json.loads(job["result"]).get("control_wait", {})
    count = old.get("failures", 0) + 1 if old.get("reason") == code else 1
    first = old.get("first_failed_at", now) if old.get("reason") == code else now
    parked = code in CONFIGURATION_ERRORS
    requested = getattr(error, "retry_after_seconds", 0)
    requested = requested if isinstance(requested, (int, float)) and math.isfinite(requested) else 0
    delay = max(min(300, 30 * 2 ** min(count - 1, 4)), requested)
    retry_at = None if parked or now + delay >= job["expires_at"] else now + delay
    result = {
        "error_type": type(error).__name__,
        "control_wait": {
            "reason": code,
            "failures": count,
            "first_failed_at": first,
            "last_failed_at": now,
            "retry_at": retry_at,
            "waiting_for": "configuration_change"
            if parked
            else "retry"
            if retry_at
            else "deadline",
            "revision": control_revision(db, job) if parked else None,
        },
    }
    return retry_at or job["expires_at"], result


def refresh_control_waits(queue):
    """Cheap reads while unchanged; changed configuration wakes only original jobs."""
    sql = (
        "SELECT j.* FROM execution_jobs j JOIN tasks t ON t.id=j.task_id "
        "WHERE j.state='pending' AND t.state='active' AND j.expires_at>? "
        "AND json_extract(j.result,'$.control_wait.waiting_for')='configuration_change'"
    )
    now = queue.clock()
    with queue.store.connect() as db:
        changed = [
            row["id"]
            for row in db.execute(sql, (now,)).fetchall()
            if json.loads(row["result"])["control_wait"].get("revision")
            != control_revision(db, row)
        ]
    if not changed:
        return
    with queue.store.connect(write=True) as db:
        for identifier in changed:
            row = db.execute(sql + " AND j.id=?", (now, identifier)).fetchone()
            if not row:
                continue
            source = queue._source(db, row)
            if not source or source["attempted"] or source["status"] != "not_attempted":
                continue
            result = json.loads(row["result"])
            if result["control_wait"].get("revision") == control_revision(db, row):
                continue
            result["control_wait"].update(waiting_for="recheck", retry_at=now)
            db.execute(
                "UPDATE execution_jobs SET available_at=?,updated_at=?,result=? WHERE id=? AND state='pending'",
                (now, now, canonical(result), identifier),
            )
            queue.store.audit(
                db, "execution.control_recheck_ready", {"job_id": identifier}, row["task_id"], now
            )


def round_cutoff(db, task_id, expires):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='phone_round_members'"
    ).fetchone():
        return expires
    row = db.execute(
        "SELECT r.submit_deadline FROM phone_round_members m JOIN phone_rounds r ON r.id=m.round_id WHERE m.task_id=?",
        (task_id,),
    ).fetchone()
    return min(expires, row[0]) if row and row[0] is not None else expires
