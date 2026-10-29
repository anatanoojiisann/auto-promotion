"""Task-selected channels with durable, per-account, fail-closed handoffs.

Legacy tasks keep their original global default. A managed account is fenced
locally before any server request; lost responses never reopen its old channel.
"""

import json
import math
import os
import socket
import uuid

from .cloud_control import CloudControl, CloudJournal, CloudUnavailable
from .models import canonical, epoch
from .store import Store


def requested(policy):
    return policy.get("execution_channel", "inherit")


def managed(db, task_id, account_id):
    task = db.execute("SELECT policy FROM tasks WHERE id=?", (task_id,)).fetchone()
    return bool(
        (task and requested(json.loads(task[0])) != "inherit")
        or db.execute(
            "SELECT 1 FROM account_task_channels WHERE account_id=?", (account_id,)
        ).fetchone()
    )


def pending_managed(engine):
    return bool(
        engine.store.one(
            "SELECT 1 FROM execution_jobs j JOIN tasks t ON t.id=j.task_id "
            "WHERE j.state='pending' AND t.state='active' AND j.expires_at>? AND "
            "(COALESCE(json_extract(t.policy,'$.execution_channel'),'inherit')<>'inherit' "
            "OR EXISTS(SELECT 1 FROM account_task_channels a WHERE a.account_id=j.account_id)) LIMIT 1",
            (engine.clock(),),
        )
    )


def effective(engine, policy, task_id=None, account_id=None):
    from .channel_decisions import resolved

    return resolved(engine, policy, task_id, account_id)


def handoff_blocks(db, authority, task_id, now):
    if not authority or authority["state"] != "handoff" or authority["task_id"] == task_id:
        return False
    owner = db.execute(
        "SELECT state,policy FROM tasks WHERE id=?", (authority["task_id"],)
    ).fetchone()
    return bool(
        owner and owner["state"] == "active" and epoch(json.loads(owner["policy"])["ends_at"]) > now
    )


def valid_context(db, context, *, now):
    if not context or not context.get("task_channel"):
        return False
    row = db.execute(
        "SELECT a.*,j.owner job_owner,j.state job_state,j.task_id job_task_id,j.lease_until,t.policy,c.owner claim_owner,"
        "c.generation claim_generation,c.channel claim_channel FROM execution_jobs j "
        "JOIN account_task_channels a ON a.account_id=j.account_id "
        "JOIN task_channel_claims c ON c.job_id=j.id JOIN tasks t ON t.id=j.task_id WHERE j.id=?",
        (context.get("id"),),
    ).fetchone()
    from .channel_decisions import saved

    decision = saved(db, row["job_task_id"]) if row else None
    if (
        decision
        and row["account_id"] in decision["resolved"]
        and decision["resolved"][row["account_id"]] != context.get("channel")
    ):
        return False
    bound = channel_binding(db, row["account_id"]) if row else {}
    store = db.execute("SELECT value FROM meta WHERE key='execution_store_id'").fetchone()
    return bool(
        row
        and row["state"] == "ready"
        and row["job_state"] == "leased"
        and row["lease_until"] > now
        and row["job_owner"] == row["claim_owner"] == context.get("owner")
        and row["generation"] == row["claim_generation"] == context.get("generation")
        and row["channel"] == row["claim_channel"] == context.get("channel")
        and requested(json.loads(row["policy"])) in {"inherit", context.get("channel")}
        and (row["channel"] == "cloud" or row["until_at"] > now)
        and store
        and store[0] == context.get("store_id")
        and binding_matches(row, bound, context.get("node_id"), context.get("store_id"))
        and context.get("account_id") == row["account_id"]
        and context.get("task_id") == row["job_task_id"]
        and original_fence_valid(db, bound, now, row["job_task_id"])
    )


def guard_context(engine, context):
    with engine.store.connect() as db:
        if not valid_context(db, context, now=engine.clock()):
            raise CloudUnavailable("TaskChannelClaimExpired")


def safe_channel_code(value):
    return (
        value
        if isinstance(value, str) and value.isidentifier() and len(value) <= 100
        else "TaskChannelUnavailable"
    )


def owns_job(db, job, now):
    row = db.execute("SELECT * FROM execution_jobs WHERE id=?", (job["id"],)).fetchone()
    return bool(
        row
        and row["owner"] == job["owner"]
        and row["state"] == "leased"
        and row["lease_until"] > now
        and row["account_id"] == job["account_id"]
        and row["task_id"] == job["task_id"]
        and row["version"] == job["version"]
    )


def channel_binding(db, account_id):
    row = db.execute(
        "SELECT value FROM meta WHERE key=?", ("account_channel_binding:" + account_id,)
    ).fetchone()
    return json.loads(row[0]) if row else {}


def save_binding(db, value):
    db.execute(
        "INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        ("account_channel_binding:" + value["account_id"], canonical(value)),
    )


def binding_matches(row, value, node_id, store_id):
    return bool(
        row
        and value
        and value.get("node_id") == node_id
        and value.get("store_id") == store_id
        and all(
            value.get(k) == row[k]
            for k in ("account_id", "task_id", "generation", "channel", "until_at")
        )
    )


def original_fence_valid(db, value, now, task_id):
    if value.get("fence_source") == "cloud_acknowledged":
        return value.get("until_at", 0) > now
    if value.get("fence_source") == "standalone":
        required = db.execute(
            "SELECT value FROM meta WHERE key='cloud_control_required'"
        ).fetchone()
        return not (required and required[0] == "1") and not os.getenv("AP_CLOUD_CONTROL_URL")
    if value.get("fence_source") != "legacy":
        return False
    state = db.execute("SELECT value FROM meta WHERE key='execution_channel'").fetchone()
    scope = db.execute("SELECT value FROM meta WHERE key='local_channel_authority'").fetchone()
    setting = db.execute("SELECT value FROM meta WHERE key='execution_channel_policy'").fetchone()
    state, scope = json.loads(state[0]) if state else {}, json.loads(scope[0]) if scope else {}
    return bool(
        setting
        and setting[0] == "cloud_then_local"
        and scope
        and state.get("active") == "local"
        and state.get("authorization") == "local_queue"
        and state.get("generation") == value.get("legacy_generation")
        and state.get("until", 0) >= value.get("until_at", 0) > now
        and state.get("node_id") == value.get("node_id")
        and state.get("store_id") == value.get("store_id")
        and value.get("account_id") in state.get("account_ids", [])
        and sorted(scope.get("account_ids", [])) == sorted(state.get("account_ids", []))
        and (not state.get("through_task_ids") or task_id in state["through_task_ids"])
    )


def ack_matches(observed, expected):
    return bool(
        observed
        and all(
            observed.get(key) == expected[key]
            for key in (
                "account_id",
                "node_id",
                "store_id",
                "task_id",
                "generation",
                "channel",
                "until_at",
            )
        )
    )


def transition_current(db, job, authority, now):
    row = db.execute(
        "SELECT * FROM account_task_channels WHERE account_id=?", (job["account_id"],)
    ).fetchone()
    bound = channel_binding(db, job["account_id"])
    return bool(
        owns_job(db, job, now)
        and row
        and row["state"] == "handoff"
        and row["generation"] == authority["generation"]
        and bound.get("transition") == authority.get("transition")
        and bound.get("owner") == job["owner"]
    )


def record_channel_failure(engine, job, error, *, authority=None, context=None, handoff=False):
    if getattr(error, "channel_failure_recorded", False):
        return
    generation = authority.get("generation") if authority else (context or {}).get("generation")
    if not generation:
        return
    now = engine.clock()
    delay = getattr(error, "retry_after_seconds", None)
    delay = delay if isinstance(delay, (int, float)) and math.isfinite(delay) and delay > 0 else 30
    with engine.store.connect(write=True) as db:
        if not owns_job(db, job, now):
            return
        if handoff and not transition_current(db, job, authority, now):
            return
        row = db.execute(
            "SELECT * FROM account_task_channels WHERE account_id=?", (job["account_id"],)
        ).fetchone()
        if not row or row["generation"] != generation:
            return
        db.execute(
            "UPDATE account_task_channels SET error=?,retry_at=MAX(retry_at,?) WHERE account_id=? AND generation=? AND state=?",
            (
                safe_channel_code(
                    str(error) if isinstance(error, CloudUnavailable) else type(error).__name__
                ),
                now + delay,
                job["account_id"],
                generation,
                "handoff" if handoff else "ready",
            ),
        )
    error.channel_failure_recorded = True


def task_local_scope_valid(engine, task_id):
    """Display evidence only; provider claims and final authorization remain mandatory."""
    from .execution_channels import independent_scope, read_state

    now = engine.clock()
    with engine.store.connect() as db:
        task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        store = db.execute("SELECT value FROM meta WHERE key='execution_store_id'").fetchone()
        if not task or not store:
            return False
        policy = json.loads(task["policy"])
        if (
            task["state"] != "active"
            or requested(policy) == "cloud"
            or epoch(policy["ends_at"]) <= now
        ):
            return False
        accounts = json.loads(task["account_ids"])
        node = os.getenv("AP_NODE_ID", socket.gethostname())
        explicit = True
        for account in accounts:
            row = db.execute(
                "SELECT * FROM account_task_channels WHERE account_id=?", (account,)
            ).fetchone()
            bound = channel_binding(db, account)
            if not (
                row
                and row["state"] == "ready"
                and row["channel"] == "local"
                and row["until_at"] > now
                and binding_matches(row, bound, node, store[0])
                and original_fence_valid(db, bound, now, task_id)
            ):
                explicit = False
                break
        if explicit and accounts:
            return True
        authority = independent_scope(db)
    state = read_state(engine.store)
    return bool(
        authority
        and state.get("authorization") == "local_queue"
        and state.get("active") == "local"
        and state.get("reason") in {"explicit_user_local", "CloudWriteQuotaExceeded"}
        and state.get("until", 0) > now
        and state.get("node_id") == node
        and state.get("store_id") == store[0]
        and state.get("generation")
        and set(accounts) <= set(state.get("account_ids", []))
        and sorted(state.get("account_ids", [])) == sorted(authority.get("account_ids", []))
        and (not state.get("through_task_ids") or task_id in state["through_task_ids"])
    )


def snapshot(engine, task):
    rows = engine.store.rows(
        "SELECT account_id,channel,state,until_at,error,retry_at,updated_at,fence_source "
        "FROM account_task_channels WHERE account_id IN ("
        + ",".join("?" for _ in task["account_ids"])
        + ")",
        tuple(task["account_ids"]),
    )
    from .execution_channels import read_state

    state, now = read_state(engine.store), engine.clock()
    selected = {a: effective(engine, task["policy"], task["id"], a) for a in task["account_ids"]}
    for row in rows:
        row["authorization_valid_until"] = row["until_at"]
        row["blocking_reason"] = (
            "账号通道交接尚未确认；保留原有归属，等待核验"
            if row["state"] == "handoff"
            else "账号通道检查暂未通过：" + safe_channel_code(row["error"])
            if row["error"] and row["retry_at"] > now
            else None
        )
    scoped_accounts = state.get("account_ids", [])
    expired = bool(
        state.get("active") == "local"
        and state.get("until", 0) <= now
        and any(
            c == "local" and (not scoped_accounts or a in scoped_accounts)
            for a, c in selected.items()
        )
    )
    return {
        "requested": requested(task["policy"]),
        "default": effective(engine, {"execution_channel": "inherit"}),
        "resolved": selected,
        "local_authority_expired": expired,
        "authorization_required": True,
        "execution_location": "local_platform_executor",
        "intent_source": task["policy"].get("execution_channel_source", ""),
        "accounts": rows,
    }


class TaskChannelRouter:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def handoff_tasks(self, job, prior):
        from .execution_channels import read_state

        task_ids = {job["task_id"]}
        task_ids.update(
            r["task_id"]
            for r in self.store.rows(
                "SELECT DISTINCT j.task_id FROM execution_jobs j JOIN task_channel_claims c ON c.job_id=j.id "
                "WHERE j.account_id=? AND c.channel='local'",
                (job["account_id"],),
            )
        )
        legacy = read_state(self.store)
        if job["account_id"] in legacy.get("account_ids", []):
            task_ids.update(
                r["task_id"]
                for r in self.store.rows(
                    "SELECT task_id FROM attempts WHERE account_id=? AND started_at>=? UNION "
                    "SELECT task_id FROM join_attempts WHERE account_id=? AND started_at>=?",
                    (
                        job["account_id"],
                        legacy.get("since", 0),
                        job["account_id"],
                        legacy.get("since", 0),
                    ),
                )
            )
        if prior and prior["channel"] == "local":
            task_ids.add(prior["task_id"])
        return sorted(task_ids)

    def prepare(self, job):
        from .channel_decisions import audit_route

        try:
            context, cloud = self._prepare(job)
        except Exception as exc:
            self.store.audit_best_effort(
                "execution.channel_failed",
                {
                    "job_id": job["id"],
                    "account_id": job["account_id"],
                    "stage": "authorization",
                    "reason": safe_channel_code(str(exc)),
                },
                task_id=job["task_id"],
            )
            raise
        audit_route(self.engine, job, context)
        return context, cloud

    def _prepare(self, job):
        from .execution_channels import binding, independent_active, offline_only, read_state

        now = self.engine.clock()
        task = self.store.one("SELECT policy FROM tasks WHERE id=?", (job["task_id"],))
        if not task:
            raise CloudUnavailable("TaskChannelTaskMissing")
        policy = json.loads(task["policy"])
        channel, until = (
            effective(self.engine, policy, job["task_id"], job["account_id"]),
            epoch(policy["ends_at"]),
        )
        if until <= now:
            raise CloudUnavailable("TaskChannelWindowExpired")
        no_cloud = offline_only(self.store, job["account_id"]) or (
            channel == "local" and offline_only(self.store)
        )
        if no_cloud and channel == "cloud":
            raise CloudUnavailable("LocalOfflineModeActive")
        cloud = None if no_cloud else CloudControl.configured()
        local_state = read_state(self.store)
        node_id = (local_state.get("node_id") if no_cloud else None) or (
            cloud.node_id if cloud else os.getenv("AP_NODE_ID", socket.gethostname())
        )
        store_id = binding(self.engine)
        if cloud and self.store.one(
            "SELECT 1 FROM cloud_receipt_outbox WHERE json_extract(lease,'$.account_id')=? LIMIT 1",
            (job["account_id"],),
        ):
            CloudJournal(self.engine, cloud).flush(account_id=job["account_id"])
        with self.store.connect(write=True) as db:
            if not owns_job(db, job, self.engine.clock()):
                raise CloudUnavailable("PersistentJobClaimExpired")
            prior = db.execute(
                "SELECT * FROM account_task_channels WHERE account_id=?", (job["account_id"],)
            ).fetchone()
            prior = dict(prior) if prior else None
            if prior and prior["retry_at"] > now:
                raise CloudUnavailable(
                    safe_channel_code(prior["error"] or "TaskChannelHandoffPending"),
                    retry_after_seconds=max(1, math.ceil(prior["retry_at"] - now)),
                )
            if handoff_blocks(db, prior, job["task_id"], now):
                raise CloudUnavailable("TaskChannelHandoffPending")
            old_binding = channel_binding(db, job["account_id"])
            bound = binding_matches(prior, old_binding, node_id, store_id)
            if no_cloud and prior and prior["channel"] == "cloud":
                proof = local_state.get("offline_fences", {}).get(job["account_id"], {})
                if (
                    not bound
                    or prior["state"] != "ready"
                    or prior["until_at"] > now
                    or proof.get("generation") != prior["generation"]
                    or proof.get("until_at") != prior["until_at"]
                ):
                    raise CloudUnavailable("OfflinePriorOwnershipUnverified")
            reusable = bool(
                prior
                and prior["state"] == "ready"
                and bound
                and prior["channel"] == channel
                and prior["until_at"] >= until
                and original_fence_valid(db, old_binding, now, job["task_id"])
                and not (old_binding.get("fence_source") == "standalone" and cloud)
            )
            if reusable:
                generation = prior["generation"]
                authority = old_binding
            else:
                if db.execute(
                    "SELECT 1 FROM execution_jobs WHERE account_id=? AND state='leased' AND lease_until>? AND id<>?",
                    (job["account_id"], now, job["id"]),
                ).fetchone():
                    raise CloudUnavailable("TaskChannelHandoffBusy")
                if db.execute(
                    "SELECT 1 FROM cloud_receipt_outbox WHERE json_extract(lease,'$.account_id')=? OR job_id IN "
                    "(SELECT id FROM execution_jobs WHERE account_id=?) LIMIT 1",
                    (job["account_id"], job["account_id"]),
                ).fetchone():
                    raise CloudUnavailable("LocalChannelReceiptPending")
                recovering = bool(
                    prior
                    and prior["state"] == "handoff"
                    and prior["task_id"] == job["task_id"]
                    and bound
                )
                if recovering:
                    channel, until, generation = (
                        prior["channel"],
                        prior["until_at"],
                        prior["generation"],
                    )
                else:
                    generation = uuid.uuid4().hex
                authority = {
                    "generation": generation,
                    "account_id": job["account_id"],
                    "node_id": node_id,
                    "store_id": store_id,
                    "task_id": job["task_id"],
                    "channel": channel,
                    "until_at": until,
                    "owner": job["owner"],
                    "job_id": job["id"],
                    "transition": uuid.uuid4().hex,
                    "fence_source": "pending",
                }
                db.execute(
                    "INSERT INTO account_task_channels VALUES(?,?,?,?,?,'handoff','pending',?,NULL,0) "
                    "ON CONFLICT(account_id) DO UPDATE SET channel=excluded.channel,generation=excluded.generation,"
                    "until_at=excluded.until_at,task_id=excluded.task_id,state='handoff',fence_source='pending',"
                    "updated_at=excluded.updated_at,error=NULL,retry_at=0",
                    (job["account_id"], channel, generation, until, job["task_id"], now),
                )
                save_binding(db, authority)
                if not recovering:
                    Store.audit(
                        db,
                        "execution.account_handoff_started",
                        {
                            "account_id": job["account_id"],
                            "channel": channel,
                            "generation": generation,
                        },
                        job["task_id"],
                        now,
                    )
        try:
            if reusable:
                if channel == "cloud":
                    if not cloud:
                        raise CloudUnavailable("CloudControlConfigurationMissing")
                    cloud.flush_settings(self.engine, account_id=job["account_id"])
                    cloud.sync(self.engine, task_ids=[job["task_id"]])
            else:
                required = self.store.one(
                    "SELECT value FROM meta WHERE key='cloud_control_required'"
                )
                legacy = read_state(self.store)
                legacy_fence = (
                    channel == "local"
                    and independent_active(self.engine)
                    and job["account_id"] in legacy.get("account_ids", [])
                    and legacy.get("until", 0) >= until
                    and legacy.get("store_id") == store_id
                    and legacy.get("node_id") == node_id
                )
                standalone = not cloud and not (required and required["value"] == "1")
                if channel == "local" and (legacy_fence or standalone):
                    fence_source = "legacy" if legacy_fence else "standalone"
                    if legacy_fence:
                        authority["legacy_generation"] = legacy.get("generation")
                else:
                    if not cloud:
                        raise CloudUnavailable("CloudControlConfigurationMissing")
                    observed = cloud.request(
                        "/v1/account-channel/status", {"account_id": job["account_id"]}
                    ).get("channel")
                    if not ack_matches(observed, authority):
                        if observed and observed.get("generation") == generation:
                            shorter = {**authority, "until_at": observed.get("until_at")}
                            if not (
                                ack_matches(observed, shorter)
                                and isinstance(observed.get("until_at"), (int, float))
                                and self.engine.clock() < observed["until_at"] < until
                            ):
                                raise CloudUnavailable("TaskChannelHandoffUnverified")
                        cloud.flush_settings(self.engine, account_id=job["account_id"])
                        CloudJournal(self.engine, cloud).flush(account_id=job["account_id"])
                        cloud.sync(self.engine, task_ids=self.handoff_tasks(job, prior))
                        observed = cloud.request(
                            "/v1/account-channel",
                            {
                                "local_store_id": store_id,
                                "account_id": job["account_id"],
                                "channel": channel,
                                "generation": generation,
                                "until": until,
                                "node_id": node_id,
                                "task_id": job["task_id"],
                                "expected_generation": observed.get("generation")
                                if observed
                                else None,
                            },
                            operator=True,
                        ).get("channel")
                    if not ack_matches(observed, authority):
                        raise CloudUnavailable("TaskChannelHandoffUnverified")
                    fence_source = "cloud_acknowledged"
                with self.store.connect(write=True) as db:
                    if not transition_current(db, job, authority, self.engine.clock()):
                        raise CloudUnavailable("PersistentJobClaimExpired")
                    authority["fence_source"] = fence_source
                    save_binding(db, authority)
                    db.execute(
                        "UPDATE account_task_channels SET state='ready',fence_source=?,updated_at=?,error=NULL,retry_at=0 "
                        "WHERE account_id=? AND generation=? AND state='handoff'",
                        (fence_source, self.engine.clock(), job["account_id"], generation),
                    )
                    Store.audit(
                        db,
                        "execution.account_channel_changed",
                        {
                            "account_id": job["account_id"],
                            "channel": channel,
                            "generation": generation,
                        },
                        job["task_id"],
                        self.engine.clock(),
                    )
        except Exception as exc:
            exc.channel_generation = generation
            exc.channel_transition = authority.get("transition")
            record_channel_failure(self.engine, job, exc, authority=authority, handoff=not reusable)
            raise
        context = {
            "id": job["id"],
            "owner": job["owner"],
            "task_channel": True,
            "channel": channel,
            "generation": generation,
            "node_id": node_id,
            "store_id": store_id,
            "account_id": job["account_id"],
            "task_id": job["task_id"],
        }
        with self.store.connect(write=True) as db:
            if not owns_job(db, job, self.engine.clock()):
                raise CloudUnavailable("PersistentJobClaimExpired")
            current = db.execute(
                "SELECT * FROM account_task_channels WHERE account_id=?", (job["account_id"],)
            ).fetchone()
            if not current or current["state"] != "ready" or current["generation"] != generation:
                raise CloudUnavailable("TaskChannelClaimExpired")
            db.execute(
                "INSERT INTO task_channel_claims VALUES(?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
                "owner=excluded.owner,generation=excluded.generation,channel=excluded.channel",
                (job["id"], job["owner"], generation, channel),
            )
        guard_context(self.engine, context)
        return context, cloud
