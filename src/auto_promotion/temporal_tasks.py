"""Opt-in workflow admission above the existing authoritative execution queue."""

import json

from .models import digest, epoch

AUTO_ENROLL_INSTRUCTION = "Automatic enrollment of a frozen local phone draft; execution requires its original explicit start"

QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS phone_dynamic_bindings(
 account_id TEXT PRIMARY KEY,platform TEXT NOT NULL,device_id TEXT NOT NULL,
 async_visual_review INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS phone_visual_preparations(
 item_id TEXT PRIMARY KEY,task_id TEXT NOT NULL,manifest_hash TEXT NOT NULL,operation_id TEXT NOT NULL UNIQUE,
 phase TEXT NOT NULL,request TEXT,native_execution_id TEXT,review_id TEXT,source_snapshot TEXT,
 review_decision_sha256 TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,prepared_at REAL,
 review_synced_at REAL,expires_at REAL,reason TEXT
);
CREATE INDEX IF NOT EXISTS phone_visual_preparation_task ON phone_visual_preparations(task_id,phase);
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
CREATE TRIGGER IF NOT EXISTS phone_dynamic_task_start AFTER UPDATE OF state,phase ON tasks
 WHEN NEW.state='active' AND NEW.phase='ready' AND NEW.manifest_hash IS NOT NULL
 AND EXISTS(SELECT 1 FROM items WHERE task_id=NEW.id)
 AND NOT EXISTS(SELECT 1 FROM attempts WHERE task_id=NEW.id)
 AND NOT EXISTS(SELECT 1 FROM items WHERE task_id=NEW.id AND
   (status<>'not_attempted' OR attempt_id IS NOT NULL OR external_reference IS NOT NULL))
 BEGIN
 INSERT OR IGNORE INTO temporal_task_links
 SELECT NEW.id,NEW.manifest_hash,
   'promotion-'||COALESCE((SELECT value FROM meta WHERE key='execution_store_id'),'local')||'-'||NEW.id||'-'||substr(NEW.manifest_hash,1,32),
   'registered',NEW.updated_at,NULL,NULL,
   'Automatic enrollment of a frozen local phone draft; execution requires its original explicit start'
 WHERE (SELECT value FROM meta WHERE key='phone_dynamic_admission_enabled')='1'
   AND NEW.created_at>=CAST((SELECT value FROM meta WHERE key='phone_dynamic_admission_cutover') AS REAL)
   AND json_extract(NEW.policy,'$.execution_method')='phone'
   AND json_extract(NEW.policy,'$.execution_channel')='local'
   AND NOT EXISTS(SELECT 1 FROM json_each(NEW.account_ids) j WHERE NOT EXISTS(
     SELECT 1 FROM phone_dynamic_bindings b JOIN accounts a ON a.id=b.account_id
     WHERE b.account_id=j.value AND a.platform=b.platform AND a.mode='live'));
 UPDATE temporal_task_links SET state='requested',requested_at=NEW.updated_at
 WHERE task_id=NEW.id AND state='registered' AND manifest_hash=NEW.manifest_hash;
 INSERT OR IGNORE INTO phone_visual_preparations(item_id,task_id,manifest_hash,operation_id,phase,created_at,updated_at)
 SELECT i.id,NEW.id,NEW.manifest_hash,'phone-visual:'||NEW.id||':'||i.id,'required',NEW.updated_at,NEW.updated_at
 FROM items i JOIN phone_dynamic_bindings b ON b.account_id=i.account_id
 JOIN temporal_task_links l ON l.task_id=NEW.id
 WHERE i.task_id=NEW.id AND b.async_visual_review=1
 AND l.instruction='Automatic enrollment of a frozen local phone draft; execution requires its original explicit start';
 END;
CREATE TRIGGER IF NOT EXISTS phone_visual_item_claim_gate BEFORE UPDATE OF state ON execution_jobs
 WHEN NEW.state='leased' AND OLD.state<>'leased' AND NEW.kind='send'
 AND EXISTS(SELECT 1 FROM phone_visual_preparations p WHERE p.item_id=NEW.reference_id AND
   (p.phase<>'ready' OR p.expires_at IS NULL OR p.expires_at<=NEW.updated_at))
 BEGIN SELECT RAISE(ABORT,'PhoneVisualPreparationRequired'); END;
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
    from .phone_visual_preparation import rows as preparation_rows

    has_preparations = bool(preparation_rows(engine, task_id))
    dynamic = link["instruction"] == AUTO_ENROLL_INSTRUCTION
    if link["state"] == "enabled":
        return {"admitted": True, "task_id": task_id, "manifest_hash": manifest_hash,
                "preparation_required": has_preparations}
    if task["state"] != "active" or task["phase"] != "ready" or link["state"] != "requested":
        return {"admitted": False, "waiting_for": "explicit_start_or_ready_phase"}
    if task["manifest_hash"] != manifest_hash or engine.clock() >= epoch(task["policy"]["ends_at"]):
        raise Conflict("TemporalTaskManifestOrDeadlineChanged")
    for account_id in task["account_ids"]:
        value = snapshot(engine, account_id, task_id=task_id)
        if not value["allowed_read"] and not dynamic:
            return {"admitted": False, "waiting_for": value["reason"]}
        # New tasks have independent per-item queue gates. Admission is not a
        # platform permission; account stops and fresh native checks remain in
        # the original claim, preflight and final single-use authorization.
        if not dynamic and not engine.account_readiness(task, engine.account(account_id))["ready"]:
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
            if not dynamic and (row["disabled"] or row["cooldown_until"] > engine.clock()):
                return {"admitted": False, "waiting_for": "account_stopped_or_cooling_down"}
        changed = db.execute("UPDATE temporal_task_links SET state='enabled',admitted_at=? "
                             "WHERE task_id=? AND state='requested' AND manifest_hash=?",
                             (engine.clock(), task_id, manifest_hash)).rowcount
        if changed:
            Store.audit(db, "temporal.task_admitted", {"workflow_id": workflow_id,
                        "manifest_hash": manifest_hash}, task_id, engine.clock())
    return {"admitted": True, "task_id": task_id, "manifest_hash": manifest_hash,
            "preparation_required": has_preparations}


def status(engine, task_id):
    from .account_control_state import task_state

    task = engine.task(task_id)
    value = task_state(task)
    value["registration"] = engine.store.one("SELECT task_id,manifest_hash,workflow_id,state,admitted_at "
                                             "FROM temporal_task_links WHERE task_id=?", (task_id,))
    value["deadline"] = epoch(task["policy"]["ends_at"])
    from .workflow_health import status as worker_status
    value["workflow_worker"] = worker_status(engine)
    if value["registration"] and value["registration"]["state"] == "requested" and not value["workflow_worker"]["coordinator_loop_alive"]:
        value["waiting_for"] = "WorkflowWorkerHeartbeatMissing"
    from .phone_visual_preparation import summary

    value["preparation"] = summary(engine, task_id)
    return value


def dynamically_authorized(service, db, task_id, account_id):
    """A commissioned route reads the original explicit start, never an ID allowlist.

    This is route eligibility, not a send permit. Queue admission, stops, quotas,
    immutable claims and the existing one-use native authorization still apply.
    """
    if not getattr(service.config, "dynamic_task_authorization_enabled", False):
        return False
    binding = service.config.bindings.get(account_id)
    if not binding or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='temporal_task_links'").fetchone():
        return False
    row = db.execute(
        "SELECT t.*,l.manifest_hash AS registered_manifest,l.state AS coordination_state,l.requested_at "
        "FROM tasks t JOIN temporal_task_links l ON l.task_id=t.id WHERE t.id=?", (task_id,)).fetchone()
    if (not row or row["state"] != "active" or row["phase"] != "ready"
            or not row["manifest_hash"] or row["manifest_hash"] != row["registered_manifest"]
            or row["coordination_state"] not in {"requested", "preparing", "enabled"}
            or row["requested_at"] is None or account_id not in json.loads(row["account_ids"])):
        return False
    policy = json.loads(row["policy"])
    return (policy.get("execution_method") == "phone" and policy.get("execution_channel") == "local"
            and service.engine.clock() < epoch(policy["ends_at"]))


def auto_register_new_phone_task(engine, task_id, *, freeze=False):
    """Enroll only new frozen drafts; enrollment itself never starts execution.

    Historical active/paused tasks and all attempted or unknown items remain on
    their original route. Called outside write transactions to avoid nested locks.
    """
    service = getattr(engine.adapters, "phone_social", None)
    if service is None or not getattr(service.config, "dynamic_task_authorization_enabled", False):
        return None
    task = engine.task(task_id)
    policy = task["policy"]
    if (task["state"] != "draft" or not task["items"]
            or policy.get("execution_method") != "phone" or policy.get("execution_channel") != "local"
            or epoch(policy["ends_at"]) <= engine.clock()
            or not task["account_ids"] or any(a not in service.config.bindings for a in task["account_ids"])
            or any(i["status"] != "not_attempted" or i.get("attempt_id") or i.get("external_reference")
                   for i in task["items"])):
        return None
    cutover = engine.store.one("SELECT value FROM meta WHERE key='phone_dynamic_admission_cutover'")
    if not cutover or task["created_at"] < float(cutover["value"]):
        return None
    if not task["manifest_hash"]:
        if freeze:
            # prepare() validates/freezes and then enrolls outside its transaction.
            engine.prepare(task_id)
        return None
    return register(engine, task_id, AUTO_ENROLL_INSTRUCTION)


def commission_dynamic_admission(service):
    """Commission once; old connected clients also hit the same durable start fence.

    Feature-off/old clients cannot silently disable an existing commissioning.
    No existing task, account stop, permission or outcome is changed.
    """
    if not getattr(service.config, "dynamic_task_authorization_enabled", False):
        return
    from .queue_store import JobQueue

    JobQueue(service.engine)
    with service.store.connect(write=True) as db:
        db.execute("INSERT OR IGNORE INTO meta VALUES('phone_dynamic_admission_cutover',?)",
                   (str(service.engine.clock()),))
        db.execute("INSERT INTO meta VALUES('phone_dynamic_admission_enabled','1') "
                   "ON CONFLICT(key) DO UPDATE SET value='1'")
        db.execute("DELETE FROM phone_dynamic_bindings")
        db.executemany("INSERT INTO phone_dynamic_bindings VALUES(?,?,?,?)",
                       [(identifier,binding.platform,binding.device_id,int(
                           getattr(service.config,"async_visual_review_enabled",False)
                           and binding.platform=="telegram" and binding.receipt_reader=="telegram_api"
                           and identifier in service.config.single_pass_adapters))
                        for identifier,binding in service.config.bindings.items()])
