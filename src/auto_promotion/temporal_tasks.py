"""Opt-in workflow admission above the existing authoritative execution queue."""

import json

from .models import digest, epoch

QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS temporal_task_links(
 task_id TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, workflow_id TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL, registered_at REAL NOT NULL, requested_at REAL, admitted_at REAL,
 instruction TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS temporal_task_admission ON temporal_task_links(state,requested_at);
CREATE TRIGGER IF NOT EXISTS temporal_task_claim_gate BEFORE UPDATE OF state ON execution_jobs
 WHEN NEW.state='leased' AND OLD.state<>'leased' AND NEW.kind IN ('send','join')
 AND EXISTS(SELECT 1 FROM temporal_task_links t WHERE t.task_id=NEW.task_id AND t.state<>'enabled')
 BEGIN SELECT RAISE(ABORT,'TemporalTaskAdmissionRequired'); END;
"""


def register(engine, task_id, instruction):
    from .core import Conflict
    from .queue_store import JobQueue
    from .store import Store

    if not instruction or len(instruction) > 2000:
        raise Conflict("TemporalHumanInstructionRequired")
    JobQueue(engine)  # Initialize the existing queue/schema before this short transaction.
    with engine.store.connect(write=True) as db:
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise Conflict("TemporalTaskNotFound")
        policy = json.loads(task["policy"])
        items = db.execute("SELECT * FROM items WHERE task_id=?", (task_id,)).fetchall()
        if (task["state"] != "draft" or not task["manifest_hash"] or not items
                or policy.get("execution_method") != "phone" or policy.get("execution_channel") != "local"
                or epoch(policy["ends_at"]) <= engine.clock()
                or any(i["status"] != "not_attempted" or i["attempt_id"] or i["external_reference"] for i in items)
                or db.execute("SELECT 1 FROM attempts WHERE task_id=?", (task_id,)).fetchone()):
            raise Conflict("TemporalRequiresNewFrozenUnattemptedLocalPhoneTask")
        store_id = db.execute("SELECT value FROM meta WHERE key='execution_store_id'").fetchone()
        identifier = "promotion-" + digest([store_id[0] if store_id else "local", task_id, task["manifest_hash"]])
        old = db.execute("SELECT * FROM temporal_task_links WHERE task_id=?", (task_id,)).fetchone()
        if old:
            if old["manifest_hash"] != task["manifest_hash"]:
                raise Conflict("TemporalManifestChanged")
            return dict(old)
        db.execute("INSERT INTO temporal_task_links VALUES(?,?,?,'registered',?,NULL,NULL,?)",
                   (task_id, task["manifest_hash"], identifier, engine.clock(), instruction))
        Store.audit(db, "temporal.task_registered", {"workflow_id": identifier,
                    "manifest_hash": task["manifest_hash"], "instruction": instruction}, task_id, engine.clock())
        return dict(db.execute("SELECT * FROM temporal_task_links WHERE task_id=?", (task_id,)).fetchone())


def request_registered(engine, db, task_id):
    # Called inside the SAME explicit-start transaction. Old clients cannot claim
    # a newly registered task until the workflow admits it (SQLite trigger fence).
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='temporal_task_links'").fetchone():
        return
    row = db.execute("SELECT * FROM temporal_task_links WHERE task_id=?", (task_id,)).fetchone()
    if not row:
        return
    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if task["manifest_hash"] != row["manifest_hash"]:
        from .core import Conflict

        raise Conflict("TemporalManifestChanged")
    if task["state"] == "active" and task["phase"] == "ready" and row["state"] == "registered":
        db.execute("UPDATE temporal_task_links SET state='requested',requested_at=? WHERE task_id=?",
                   (engine.clock(), task_id))


def pending(engine):
    return engine.store.rows("SELECT l.task_id,l.manifest_hash,l.workflow_id,t.policy FROM temporal_task_links l "
                             "JOIN tasks t ON t.id=l.task_id WHERE l.state='requested' AND t.state='active' "
                             "ORDER BY l.requested_at LIMIT 20")


def admit(engine, task_id, manifest_hash, workflow_id):
    from .account_control_state import snapshot
    from .core import Conflict
    from .store import Store

    task = engine.task(task_id)
    link = engine.store.one("SELECT * FROM temporal_task_links WHERE task_id=?", (task_id,))
    if not link or link["manifest_hash"] != manifest_hash or link["workflow_id"] != workflow_id:
        raise Conflict("TemporalTaskCorrelationRequired")
    if link["state"] == "enabled":
        return {"admitted": True, "task_id": task_id, "manifest_hash": manifest_hash}
    if task["state"] != "active" or task["phase"] != "ready" or link["state"] != "requested":
        return {"admitted": False, "waiting_for": "explicit_start_or_ready_phase"}
    if task["manifest_hash"] != manifest_hash or engine.clock() >= epoch(task["policy"]["ends_at"]):
        raise Conflict("TemporalTaskManifestOrDeadlineChanged")
    for account_id in task["account_ids"]:
        value = snapshot(engine, account_id, task_id=task_id)
        if not value["allowed_read"]:
            return {"admitted": False, "waiting_for": value["reason"]}
        if not engine.account_readiness(task, engine.account(account_id))["ready"]:
            return {"admitted": False, "waiting_for": "phone_identity_required"}
    with engine.store.connect(write=True) as db:
        current = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if current["state"] != "active" or current["manifest_hash"] != manifest_hash:
            raise Conflict("TemporalTaskStoppedOrChanged")
        if db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()[0] != "0":
            return {"admitted": False, "waiting_for": "global_stop"}
        accounts = json.loads(current["account_ids"])
        for identifier in accounts:
            row = db.execute("SELECT disabled,cooldown_until FROM accounts WHERE id=?", (identifier,)).fetchone()
            if row["disabled"] or row["cooldown_until"] > engine.clock():
                return {"admitted": False, "waiting_for": "account_stopped_or_cooling_down"}
        changed = db.execute("UPDATE temporal_task_links SET state='enabled',admitted_at=? "
                             "WHERE task_id=? AND state='requested' AND manifest_hash=?",
                             (engine.clock(), task_id, manifest_hash)).rowcount
        if changed:
            Store.audit(db, "temporal.task_admitted", {"workflow_id": workflow_id,
                        "manifest_hash": manifest_hash}, task_id, engine.clock())
    return {"admitted": True, "task_id": task_id, "manifest_hash": manifest_hash}


def status(engine, task_id):
    from .account_control_state import task_state

    task = engine.task(task_id)
    value = task_state(task)
    value["registration"] = engine.store.one("SELECT task_id,manifest_hash,workflow_id,state,admitted_at "
                                             "FROM temporal_task_links WHERE task_id=?", (task_id,))
    value["deadline"] = epoch(task["policy"]["ends_at"])
    return value
