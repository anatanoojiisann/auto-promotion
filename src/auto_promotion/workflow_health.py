"""Authenticated coordinator loop heartbeat, separate from server availability."""
import json
import re

from .models import canonical


def heartbeat(engine, value):
    if not isinstance(value, dict) or set(value) != {"worker_id", "task_queue"}:
        raise ValueError("ExplicitWorkflowWorkerIdentityRequired")
    if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", v) for v in value.values()):
        raise ValueError("InvalidWorkflowWorkerIdentity")
    observed = {**value, "observed_at": engine.clock()}
    with engine.store.connect(write=True) as db:
        db.execute("INSERT INTO meta(key,value) VALUES('workflow_worker_heartbeat',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (canonical(observed),))
    return {"recorded": True}


def status(engine):
    row=engine.store.one("SELECT value FROM meta WHERE key='workflow_worker_heartbeat'")
    value=json.loads(row["value"]) if row else {}
    observed=value.get("observed_at")
    fresh=type(observed) in (int,float) and 0 <= engine.clock()-observed <= 30
    return {"coordinator_loop_alive": fresh, "observed_at": observed,
            "reason": "workflow_worker_loop_observed" if fresh else "WorkflowWorkerHeartbeatMissing",
            "publication_permission": False}
