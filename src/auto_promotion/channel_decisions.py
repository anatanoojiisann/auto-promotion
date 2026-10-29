"""Bind channel intent to a task without changing its frozen policy or manifest."""

import json

from .models import canonical
from .store import Store


def saved(db, task_id):
    row = db.execute(
        "SELECT value FROM meta WHERE key=?", ("task_channel_decision:" + task_id,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def freeze(db, task, now, *, source, actor):
    prior = saved(db, task["id"])
    if prior:
        return prior
    policy = json.loads(task["policy"])
    requested = policy.get("execution_channel", "inherit")
    resolved = {}
    for account in json.loads(task["account_ids"]):
        if requested != "inherit":
            resolved[account] = requested
    decision = {
        "requested": requested,
        "resolved": resolved,
        "source": source,
        "actor": actor,
        "intent_reference": policy.get("execution_channel_source", ""),
        "reason": "explicit_task_channel" if requested != "inherit" else "follow_default_requested",
        "policy_hash": task["policy_hash"],
        "at": now,
    }
    db.execute(
        "INSERT INTO meta VALUES(?,?)", ("task_channel_decision:" + task["id"], canonical(decision))
    )
    Store.audit(db, "execution.channel_bound", decision, task["id"], now)
    return decision


def resolved(engine, policy, task_id=None, account_id=None):
    selected = policy.get("execution_channel", "inherit")
    if selected != "inherit":
        return selected
    if task_id:
        with engine.store.connect() as db:
            value = saved(db, task_id)
        if value and account_id in value["resolved"]:
            return value["resolved"][account_id]
    from .execution_channels import read_state

    state = read_state(engine.store)
    if account_id and state.get("account_ids") and account_id not in state["account_ids"]:
        return "cloud"
    # An expired independent-local instruction pauses locally; it never silently turns online.
    return state.get("active", "cloud")


def audit_route(engine, job, context, *, stage="authorized", error=None):
    with engine.store.connect(write=True) as db:
        task = db.execute(
            "SELECT policy,policy_hash FROM tasks WHERE id=?", (job["task_id"],)
        ).fetchone()
        decision = saved(db, job["task_id"])
        data = {
            "job_id": job["id"],
            "account_id": job["account_id"],
            "stage": stage,
            "requested_channel": json.loads(task["policy"]).get("execution_channel", "inherit"),
            "channel": context.get("channel"),
            "generation": context.get("generation"),
            "execution_node": context.get("node_id"),
            "execution_host": "local_platform_executor",
            "policy_hash": task["policy_hash"],
            "decision_reason": (decision or {}).get("reason", "legacy_default"),
            "intent_reference": (decision or {}).get("intent_reference", ""),
            "intent_source": (decision or {}).get("source", "legacy_unrecorded"),
        }
        if error:
            from .task_channels import safe_channel_code

            data["reason"] = safe_channel_code(str(error))
        Store.audit(
            db,
            "execution.job_channel" if not error else "execution.channel_failed",
            data,
            job["task_id"],
            engine.clock(),
        )
