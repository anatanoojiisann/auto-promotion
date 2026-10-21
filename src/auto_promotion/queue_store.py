"""Durable reference jobs; all outbound effects remain guarded by Engine and backends."""

import json
import os
import threading
import time
from contextlib import contextmanager

from .models import canonical, digest, epoch
from .routing import route_for

_CURRENT_JOB = threading.local()


def executor_task_ids():
    """Optional task allowlist for a deliberately scoped local executor."""
    raw = os.getenv("AP_EXECUTOR_TASK_IDS")
    if raw is None:
        return None
    return sorted({task_id.strip() for task_id in raw.split(",") if task_id.strip()})


@contextmanager
def job_scope(job):
    previous = getattr(_CURRENT_JOB, "owner", None)
    _CURRENT_JOB.owner = job["owner"]
    try:
        yield
    finally:
        _CURRENT_JOB.owner = previous


def bind_connection(db):
    db.create_function("ap_pool_owner", 0, lambda: getattr(_CURRENT_JOB, "owner", None))


QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_task_channels(
 account_id TEXT PRIMARY KEY,channel TEXT NOT NULL,generation TEXT NOT NULL,
 until_at REAL NOT NULL,task_id TEXT NOT NULL,state TEXT NOT NULL,
 fence_source TEXT NOT NULL,updated_at REAL NOT NULL,error TEXT,
 retry_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS task_channel_claims(
 job_id TEXT PRIMARY KEY,owner TEXT NOT NULL,generation TEXT NOT NULL,channel TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_jobs(
 id TEXT PRIMARY KEY,kind TEXT NOT NULL,reference_id TEXT NOT NULL,task_id TEXT,
 account_id TEXT,pool TEXT NOT NULL,route TEXT NOT NULL,version TEXT NOT NULL,
 available_at REAL NOT NULL,expires_at REAL NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
 owner TEXT,lease_until REAL NOT NULL DEFAULT 0,created_at REAL NOT NULL,updated_at REAL NOT NULL,
 result TEXT NOT NULL DEFAULT '{}',UNIQUE(kind,reference_id)
);
CREATE INDEX IF NOT EXISTS execution_jobs_due ON execution_jobs(pool,state,available_at);
CREATE INDEX IF NOT EXISTS execution_jobs_task ON execution_jobs(task_id);
CREATE TABLE IF NOT EXISTS pool_workers(
 id TEXT PRIMARY KEY,pool TEXT NOT NULL,node_id TEXT NOT NULL,pid INTEGER NOT NULL,
 started_at REAL NOT NULL,heartbeat_at REAL NOT NULL,state TEXT NOT NULL,reason TEXT
);
CREATE TABLE IF NOT EXISTS execution_resources(
 resource TEXT PRIMARY KEY,owner TEXT NOT NULL,expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cloud_receipt_outbox(
 job_id TEXT PRIMARY KEY,lease TEXT NOT NULL,outcome TEXT,
 updated_at REAL NOT NULL,retry_at REAL NOT NULL DEFAULT 0,error TEXT
);
CREATE TABLE IF NOT EXISTS cloud_receipt_history(
 job_id TEXT PRIMARY KEY,lease TEXT NOT NULL,outcome TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS execution_job_routes(
 job_id TEXT PRIMARY KEY,route TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS pooled_send_attempt BEFORE INSERT ON attempts
 WHEN (SELECT value FROM meta WHERE key='execution_runtime')='pooled'
 BEGIN
 SELECT CASE WHEN NOT EXISTS(
 SELECT 1 FROM execution_jobs j WHERE j.kind='send' AND j.reference_id=NEW.item_id
 AND j.account_id=NEW.account_id AND j.state='leased' AND j.owner=ap_pool_owner()
 AND j.lease_until>unixepoch('now')) THEN RAISE(ABORT,'PersistentJobLeaseRequired') END;
 END;
CREATE TRIGGER IF NOT EXISTS pooled_join_attempt BEFORE INSERT ON join_attempts
 WHEN (SELECT value FROM meta WHERE key='execution_runtime')='pooled'
 BEGIN
 SELECT CASE WHEN NOT EXISTS(
 SELECT 1 FROM execution_jobs j WHERE j.kind='join' AND j.reference_id=NEW.candidate_id
 AND j.account_id=NEW.account_id AND j.state='leased' AND j.owner=ap_pool_owner()
 AND j.lease_until>unixepoch('now')) THEN RAISE(ABORT,'PersistentJobLeaseRequired') END;
 END;
CREATE TRIGGER IF NOT EXISTS task_channel_send_attempt_v2 BEFORE INSERT ON attempts
 WHEN EXISTS(SELECT 1 FROM account_task_channels WHERE account_id=NEW.account_id)
 OR COALESCE((SELECT json_extract(policy,'$.execution_channel') FROM tasks WHERE id=NEW.task_id),'inherit')<>'inherit'
 BEGIN
 SELECT CASE WHEN NOT EXISTS(
 SELECT 1 FROM execution_jobs j JOIN task_channel_claims c ON c.job_id=j.id JOIN tasks t ON t.id=j.task_id
 JOIN account_task_channels a ON a.account_id=j.account_id
 WHERE j.reference_id=NEW.item_id AND j.kind='send' AND j.owner=ap_pool_owner()
 AND c.owner=j.owner AND a.state='ready' AND a.channel=c.channel
 AND COALESCE(json_extract(t.policy,'$.execution_channel'),'inherit') IN ('inherit',a.channel)
 AND a.generation=c.generation AND (a.channel='cloud' OR a.until_at>unixepoch('now'))
 ) THEN RAISE(ABORT,'TaskChannelClaimRequired') END;
 END;
CREATE TRIGGER IF NOT EXISTS task_channel_join_attempt_v2 BEFORE INSERT ON join_attempts
 WHEN EXISTS(SELECT 1 FROM account_task_channels WHERE account_id=NEW.account_id)
 OR COALESCE((SELECT json_extract(policy,'$.execution_channel') FROM tasks WHERE id=NEW.task_id),'inherit')<>'inherit'
 BEGIN
 SELECT CASE WHEN NOT EXISTS(
 SELECT 1 FROM execution_jobs j JOIN task_channel_claims c ON c.job_id=j.id JOIN tasks t ON t.id=j.task_id
 JOIN account_task_channels a ON a.account_id=j.account_id
 WHERE j.reference_id=NEW.candidate_id AND j.kind='join' AND j.owner=ap_pool_owner()
 AND c.owner=j.owner AND a.state='ready' AND a.channel=c.channel
 AND COALESCE(json_extract(t.policy,'$.execution_channel'),'inherit') IN ('inherit',a.channel)
 AND a.generation=c.generation AND (a.channel='cloud' OR a.until_at>unixepoch('now'))
 ) THEN RAISE(ABORT,'TaskChannelClaimRequired') END;
 END;
"""

WRITE_TERMINAL = {
    "confirmed",
    "scheduled",
    "failed_known",
    "unknown",
    "manual_attention",
    "missed",
    "cancelled",
    "joined",
    "already_joined",
    "pending_admin_approval",
}


class JobQueue:
    def __init__(self, engine):
        self.engine, self.store, self.clock = engine, engine.store, engine.clock

    def sync(self, task_id=None):
        """Idempotently reference only explicitly active tasks; never modify their manifests."""
        sql = "SELECT * FROM tasks WHERE state='active'"
        params = ()
        if task_id:
            sql += " AND id=?"
            params = (task_id,)
        added = 0
        for task in self.store.rows(sql, params):
            policy = json.loads(task["policy"])
            if task["phase"] == "starting" and policy.get("outbound_after_task_ids"):
                from .core import Conflict
                from .task_dependencies import publication_schedule

                with self.store.connect() as db:
                    _, reason = publication_schedule(db, task, self.clock())
                if reason not in {
                    "interaction_dependencies_pending",
                    "interaction_schedule_pacing_blocked",
                }:
                    try:
                        self.engine.start_task(task["id"], source="scheduler")
                    except Conflict:
                        pass
                    task = self.store.one("SELECT * FROM tasks WHERE id=?", (task["id"],))
            if (
                task["phase"] == "starting"
                and policy.get("outbound_after_membership_seconds") is not None
            ):
                # A read-only membership tracker may have completed a linked join.
                # Only resume an explicitly started task with verified evidence.
                verified = self.store.rows(
                    "SELECT 1 FROM items i JOIN candidates c ON c.account_id=i.account_id "
                    "AND lower(c.target)=lower(json_extract(i.payload,'$.target')) "
                    "WHERE i.task_id=? AND c.state IN ('joined','already_joined') "
                    "AND json_extract(c.result,'$.verified')=1 LIMIT 1",
                    (task["id"],),
                )
                if verified:
                    from .core import Conflict

                    try:
                        self.engine.start_task(task["id"], source="scheduler")
                    except Conflict:
                        pass
                    task = self.store.one("SELECT * FROM tasks WHERE id=?", (task["id"],))
            if self.clock() >= epoch(policy["ends_at"]):
                with self.store.connect(write=True) as db:
                    db.execute(
                        "UPDATE items SET status='missed',result=? WHERE task_id=? AND status='not_attempted' AND attempt_id IS NULL",
                        (canonical({"reason": "delivery_window_elapsed"}), task["id"]),
                    )
                    self.engine._mark_completed(db, task["id"])
                continue
            if task["phase"] == "ready":
                rows = self.store.rows(
                    "SELECT * FROM items WHERE task_id=? AND status='not_attempted' AND attempt_id IS NULL ORDER BY position",
                    (task["id"],),
                )
                for row in rows:
                    payload = json.loads(row["payload"])
                    account = self.engine.account(row["account_id"])
                    route = route_for(account, payload)
                    native = payload["action"] in {"schedule_message", "schedule_file"}
                    due = epoch(policy["starts_at"]) if native else row["due_at"]
                    expires = min(
                        epoch(policy["ends_at"]), row["due_at"] if native else row["due_at"] + 900
                    )
                    added += self._put(
                        "send", row["id"], task, account, route, row["payload_hash"], due, expires
                    )
            if policy["max_joins"] and task["phase"] in {"discovered", "starting", "ready"}:
                for row in self.store.rows(
                    "SELECT * FROM candidates WHERE task_id=? AND decision='qualified' AND state='not_attempted' AND NOT EXISTS(SELECT 1 FROM join_attempts a WHERE a.candidate_id=candidates.id)",
                    (task["id"],),
                ):
                    account = self.engine.account(row["account_id"])
                    if account["platform"] != "telegram":
                        continue
                    added += self._put(
                        "join",
                        row["id"],
                        task,
                        account,
                        route_for(account),
                        digest(json.loads(row["payload"])),
                        epoch(policy["starts_at"]),
                        epoch(policy["ends_at"]),
                    )
        self.recover()
        return added

    def _put(self, kind, reference, task, account, route, version, due, expires):
        now = self.clock()
        with self.store.connect(write=True) as db:
            before = db.total_changes
            db.execute(
                "INSERT OR IGNORE INTO execution_jobs(id,kind,reference_id,task_id,account_id,pool,route,version,available_at,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    kind + ":" + reference,
                    kind,
                    reference,
                    task["id"],
                    account["id"],
                    route.pool,
                    canonical(route.public()),
                    version,
                    due,
                    expires,
                    now,
                    now,
                ),
            )
            return int(db.total_changes != before)

    def recover(self):
        now = self.clock()
        terminal = ",".join("?" for _ in WRITE_TERMINAL)
        query = (
            "SELECT j.* FROM execution_jobs j LEFT JOIN items i "
            "ON j.kind='send' AND i.id=j.reference_id LEFT JOIN candidates c "
            "ON j.kind='join' AND c.id=j.reference_id LEFT JOIN tasks t ON t.id=j.task_id "
            "WHERE j.kind IN ('send','join') AND (j.state IN ('pending','leased') OR (j.state='unknown' AND j.kind='send' "
            "AND i.status IN ('confirmed','scheduled','failed_known','cancelled','missed') "
            "AND j.account_id=i.account_id AND j.task_id=i.task_id AND j.version=i.payload_hash "
            "AND EXISTS(SELECT 1 FROM attempts a WHERE a.id=i.attempt_id AND a.item_id=i.id "
            "AND a.account_id=i.account_id AND a.task_id=i.task_id))) AND ("
            f"(j.kind='send' AND (i.id IS NULL OR i.status IN ({terminal}))) OR "
            f"(j.kind='join' AND (c.id IS NULL OR c.state IN ({terminal}))) OR "
            "(j.state='pending' AND (t.state='cancelled' OR j.expires_at<=?)) OR "
            "(j.state='leased' AND j.lease_until<=?))"
        )
        params = (*sorted(WRITE_TERMINAL), *sorted(WRITE_TERMINAL), now, now)
        if not self.store.one(query + " LIMIT 1", params):
            return
        with self.store.connect(write=True) as db:
            # Re-read under the write lock: another worker may have renewed or
            # settled a lease since the cheap read-only candidate check.
            for row in db.execute(query, params).fetchall():
                source = self._source(db, row)
                parent = db.execute(
                    "SELECT state FROM tasks WHERE id=?", (row["task_id"],)
                ).fetchone()
                if source is None:
                    state = "cancelled"
                elif source["status"] in WRITE_TERMINAL:
                    state = source["status"]
                elif (
                    parent
                    and parent["state"] == "cancelled"
                    and row["state"] == "pending"
                    and not source["attempted"]
                ):
                    state = "cancelled"
                elif (
                    row["state"] == "pending"
                    and row["expires_at"] <= now
                    and not source["attempted"]
                ):
                    state = "missed"
                    if row["kind"] == "send":
                        db.execute(
                            "UPDATE items SET status='missed',result=? WHERE id=? AND status='not_attempted' AND attempt_id IS NULL",
                            (canonical({"reason": "delivery_window_elapsed"}), row["reference_id"]),
                        )
                elif row["state"] == "leased" and row["lease_until"] <= now:
                    # A recorded attempt is never made pending again after losing a lease.
                    state = "unknown" if source["attempted"] else "pending"
                else:
                    continue
                db.execute(
                    "UPDATE execution_jobs SET state=?,owner=NULL,lease_until=0,updated_at=? WHERE id=?",
                    (state, now, row["id"]),
                )

    @staticmethod
    def _source(db, row):
        if row["kind"] == "send":
            item = db.execute(
                "SELECT status,attempt_id FROM items WHERE id=?", (row["reference_id"],)
            ).fetchone()
            return (
                {"status": item["status"], "attempted": bool(item["attempt_id"])} if item else None
            )
        candidate = db.execute(
            "SELECT state FROM candidates WHERE id=?", (row["reference_id"],)
        ).fetchone()
        attempted = db.execute(
            "SELECT 1 FROM join_attempts WHERE candidate_id=?", (row["reference_id"],)
        ).fetchone()
        return {"status": candidate["state"], "attempted": bool(attempted)} if candidate else None

    def claim(self, pool, owner, lease_seconds=90):
        self.recover()
        now = self.clock()
        limits = self.engine.execution_settings()
        with self.store.connect(write=True) as db:
            expiry_pending = db.execute(
                "SELECT 1 FROM meta WHERE key='local_fallback_expiry_requested'"
            ).fetchone()
            if db.execute("SELECT value FROM meta WHERE key='global_stop'").fetchone()[0] != "0":
                return None
            if db.execute(
                "SELECT COUNT(*) FROM execution_jobs WHERE state='leased' AND lease_until>? AND pool=?",
                (now, pool),
            ).fetchone()[0] >= limits.get("pool_workers", {}).get(pool, 1):
                return None
            sql = (
                "SELECT j.* FROM execution_jobs j JOIN tasks t ON t.id=j.task_id "
                "JOIN accounts a ON a.id=j.account_id LEFT JOIN items i "
                "ON j.kind='send' AND i.id=j.reference_id WHERE j.pool=? "
                "AND j.state='pending' AND j.available_at<=? AND t.state='active' "
                "AND a.disabled=0 AND a.cooldown_until<=? AND a.next_at<=? "
                "AND NOT EXISTS(SELECT 1 FROM execution_jobs busy WHERE busy.state='leased' "
                "AND busy.lease_until>? AND busy.account_id=j.account_id) "
                "AND NOT EXISTS(SELECT 1 FROM account_lanes lane "
                "WHERE lane.account_id=j.account_id AND lane.expires_at>?)"
            )
            params = [pool, now, now, now, now, now]
            from .execution_channels import local_account_scope

            accounts = local_account_scope(db, now)
            if accounts is not None:
                sql += " AND (COALESCE(json_extract(t.policy,'$.execution_channel'),'inherit')<>'inherit' "
                sql += "OR EXISTS(SELECT 1 FROM account_task_channels ch WHERE ch.account_id=j.account_id) OR ("
                sql += "j.account_id IN (" + (",".join("?" for _ in accounts) or "NULL") + ")"
                sql += " AND NOT EXISTS(SELECT 1 FROM cloud_receipt_outbox receipt WHERE receipt.job_id=j.id)))"
                params.extend(accounts)
            if expiry_pending:
                sql += " AND (COALESCE(json_extract(t.policy,'$.execution_channel'),'inherit')<>'inherit' "
                sql += "OR EXISTS(SELECT 1 FROM account_task_channels ch WHERE ch.account_id=j.account_id))"
            task_ids = executor_task_ids()
            if task_ids is not None:
                if not task_ids:
                    return None
                sql += " AND j.task_id IN (" + ",".join("?" for _ in task_ids) + ")"
                params.extend(task_ids)
            sql += (
                " ORDER BY CASE WHEN j.kind='send' THEN 0 ELSE 1 END,a.next_at,"
                "j.available_at,COALESCE(i.position,0),j.id LIMIT 64"
            )
            rows = db.execute(sql, params).fetchall()
            for row in rows:
                from .task_channels import handoff_blocks

                channel = db.execute(
                    "SELECT state,task_id,retry_at FROM account_task_channels WHERE account_id=?",
                    (row["account_id"],),
                ).fetchone()
                if channel and (
                    channel["retry_at"] > now or handoff_blocks(db, channel, row["task_id"], now)
                ):
                    continue
                source = self._source(db, row)
                if not source or source["attempted"] or source["status"] != "not_attempted":
                    continue
                task = db.execute(
                    "SELECT phase,policy FROM tasks WHERE id=?", (row["task_id"],)
                ).fetchone()
                if (row["kind"] == "send" and task["phase"] != "ready") or now >= row["expires_at"]:
                    continue
                route = json.loads(row["route"])
                account = db.execute(
                    "SELECT * FROM account_lanes WHERE account_id=? AND expires_at>?",
                    (row["account_id"], now),
                ).fetchone()
                resource = route.get("resource")
                resource_row = (
                    db.execute(
                        "SELECT * FROM execution_resources WHERE resource=? AND expires_at>?",
                        (resource, now),
                    ).fetchone()
                    if resource
                    else None
                )
                if account or resource_row:
                    continue
                if db.execute(
                    "SELECT 1 FROM execution_jobs WHERE state='leased' AND lease_until>? AND account_id=?",
                    (now, row["account_id"]),
                ).fetchone():
                    continue
                if (
                    resource
                    and db.execute(
                        "SELECT 1 FROM execution_jobs WHERE state='leased' AND lease_until>? AND json_extract(route,'$.resource')=?",
                        (now, resource),
                    ).fetchone()
                ):
                    continue
                db.execute(
                    "UPDATE execution_jobs SET state='leased',owner=?,lease_until=?,updated_at=? WHERE id=? AND state='pending'",
                    (owner, now + lease_seconds, now, row["id"]),
                )
                return {
                    **dict(row),
                    "owner": owner,
                    "state": "leased",
                    "lease_until": now + lease_seconds,
                }
        return None

    def renew(self, job, seconds=90):
        with self.store.connect(write=True) as db:
            db.execute(
                "UPDATE pool_workers SET heartbeat_at=? WHERE id=? AND state='running'",
                (self.clock(), job["owner"]),
            )
            return (
                db.execute(
                    "UPDATE execution_jobs SET lease_until=?,updated_at=? WHERE id=? AND owner=? AND state='leased' AND lease_until>?",
                    (self.clock() + seconds, self.clock(), job["id"], job["owner"], self.clock()),
                ).rowcount
                == 1
            )

    @contextmanager
    def heartbeat(self, job, remote_renew=None):
        stop = threading.Event()

        def pulse():
            while not stop.wait(10):
                try:
                    if (remote_renew and not remote_renew()) or not self.renew(job):
                        return
                except Exception:
                    return

        thread = threading.Thread(target=pulse, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=2)

    def settle(self, job, error=None):
        now = self.clock()
        with self.store.connect(write=True) as db:
            source = self._source(db, job)
            state = source["status"] if source else "cancelled"
            if state == "executing" or (state == "not_attempted" and source["attempted"]):
                state = "unknown"
            delay = now + (30 if error else 5)
            if state == "not_attempted":
                account = db.execute(
                    "SELECT * FROM accounts WHERE id=?",
                    (job["account_id"],),
                ).fetchone()
                delay = max(delay, account["next_at"], account["cooldown_until"])
                if account["status"] != "healthy":
                    delay = max(delay, now + 300)
                if job["kind"] == "send":
                    from .target_checks import TargetChecks

                    item = db.execute(
                        "SELECT payload FROM items WHERE id=?", (job["reference_id"],)
                    ).fetchone()
                    deferred = TargetChecks(
                        self.engine,
                        dict(account),
                        "preflight",
                        {"payload": json.loads(item[0])},
                    ).read(db)
                    if deferred:
                        delay = max(delay, deferred["eligibility_retry_at"])
                state = "pending"
            db.execute(
                "UPDATE execution_jobs SET state=?,available_at=?,owner=NULL,lease_until=0,updated_at=?,result=? WHERE id=? AND owner=?",
                (
                    state,
                    delay,
                    now,
                    canonical({"error_type": type(error).__name__} if error else {}),
                    job["id"],
                    job["owner"],
                ),
            )
        return state

    def pools(self, task_id=None):
        now = self.clock()
        rows = self.store.rows(
            "SELECT pool,state,count(*) count,MIN(available_at) oldest FROM execution_jobs "
            + ("WHERE task_id=? " if task_id else "")
            + "GROUP BY pool,state",
            (task_id,) if task_id else (),
        )
        cloud = self.store.one("SELECT value FROM meta WHERE key='cloud_control_health'")
        from .execution_channels import enabled, read_state

        return {
            "jobs": rows,
            "account_channels": self.store.rows(
                "SELECT account_id,channel,state,until_at,task_id,error,retry_at FROM account_task_channels"
            ),
            "cloud_receipts_pending": self.store.one("SELECT COUNT(*) n FROM cloud_receipt_outbox")[
                "n"
            ],
            "cloud": json.loads(cloud["value"]) if cloud else {"state": "not_configured"},
            "channel": {
                "policy": "cloud_then_local" if enabled(self.store) else "cloud_only",
                **read_state(self.store),
            },
            "cloud_settings_pending": bool(
                self.store.one("SELECT value FROM meta WHERE key='cloud_pending_settings'")
            ),
            "workers": self.store.rows(
                "SELECT id,pool,node_id,pid,heartbeat_at,state FROM pool_workers WHERE heartbeat_at>?",
                (now - 60,),
            ),
        }


def verify_job_claim(db, request):
    context = request.get("execution_job")
    if context and context.get("task_channel"):
        from .task_channels import valid_context

        if not valid_context(db, context, now=time.time()):
            raise ValueError("TaskChannelClaimExpired")
    enabled = db.execute("SELECT value FROM meta WHERE key='execution_runtime'").fetchone()
    required = db.execute("SELECT value FROM meta WHERE key='cloud_control_required'").fetchone()
    from .execution_channels import local_context_valid

    if (
        required
        and required[0] == "1"
        and (not context or not context.get("cloud"))
        and not local_context_valid(db, context)
    ):
        raise ValueError("CloudExecutionClaimRequired")
    if not context:
        if enabled and enabled[0] == "pooled":
            raise ValueError("PersistentJobClaimRequired")
        return
    row = db.execute("SELECT * FROM execution_jobs WHERE id=?", (context.get("id"),)).fetchone()
    reference = (
        request["payload"].get("candidate_id")
        if request["operation"] == "join"
        else request["payload"].get("item_id")
    )
    if (
        not row
        or row["owner"] != context.get("owner")
        or row["state"] != "leased"
        or row["lease_until"] <= time.time()
        or row["reference_id"] != reference
        or row["account_id"] != request["account_id"]
    ):
        raise ValueError("PersistentJobClaimExpired")
