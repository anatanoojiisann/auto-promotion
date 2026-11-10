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
    from .local_primary import configured

    if configured(db):
        return True
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
    if job.get("authorization_only") is True:
        from .local_authorization import owns_authorization

        return owns_authorization(db, job, now)
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
    if value.get("x_history_ack_required"):
        from .x_history_ack import allowed_unknown_ids

        if allowed_unknown_ids(db, task_id, value.get("account_id"), now) is None:
            return False
    if value.get("coordination_task_id"):
        task = db.execute("SELECT manifest_hash FROM tasks WHERE id=?", (task_id,)).fetchone()
        if (
            value["coordination_task_id"] != task_id
            or not task
            or task[0] != value.get("coordination_manifest_hash")
        ):
            return False
    if value.get("fence_source") == "local_primary":
        from .local_primary import valid

        grant = valid(db, value.get("account_id", ""))
        return bool(
            grant
            and grant["generation"] == value.get("primary_grant_generation")
            and value.get("node_id") == grant["node_id"]
            and value.get("store_id") == grant["store_id"]
            and value.get("until_at", 0) > now
        )
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
        from .local_primary import configured, valid

        if configured(db):
            if not accounts or not all(valid(db, account) for account in accounts):
                return False
            try:
                for account in accounts:
                    control_preflight(engine, task_id, account, policy)
            except CloudUnavailable:
                return False
            return True
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
    from .local_authorization import authorization_route

    current_local_accounts = set()
    supported_accounts = []
    with engine.store.connect() as db:
        for account_id in task["account_ids"]:
            account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not account or account["mode"] != "live":
                continue
            try:
                authorization_route(engine, db, dict(account), task["policy"])
            except Exception:
                # One unavailable adapter must not hide another account's controls.
                continue
            supported_accounts.append(account_id)
        store = db.execute("SELECT value FROM meta WHERE key='execution_store_id'").fetchone()
        node = os.getenv("AP_NODE_ID", socket.gethostname())
        from .local_primary import configured, valid

        for account_id in task["account_ids"]:
            if configured(db) and valid(db, account_id):
                current_local_accounts.add(account_id)
            row = db.execute(
                "SELECT * FROM account_task_channels WHERE account_id=?", (account_id,)
            ).fetchone()
            bound = channel_binding(db, account_id)
            if (
                store
                and row
                and row["channel"] == "local"
                and row["state"] == "ready"
                and row["until_at"] > now
                and binding_matches(row, bound, node, store[0])
                and original_fence_valid(db, bound, now, task["id"])
            ):
                current_local_accounts.add(account_id)
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
        with engine.store.connect() as db:
            primary = configured(db)
        if primary:
            try:
                control_preflight(engine, task["id"], row["account_id"], task["policy"])
            except CloudUnavailable as error:
                row["blocking_reason"] = "账号通道检查暂未通过：" + safe_channel_code(str(error))
            else:
                row["blocking_reason"] = None
    scoped_accounts = state.get("account_ids", [])
    expired = bool(
        state.get("active") == "local"
        and state.get("until", 0) <= now
        and any(
            c == "local"
            and a not in current_local_accounts
            and (not scoped_accounts or a in scoped_accounts)
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
        "local_authorization_accounts": supported_accounts,
        "local_authorizations": engine.store.rows(
            "SELECT account_id,task_id,state,error,until_at,updated_at FROM channel_authorization_requests "
            "WHERE task_id=?",
            (task["id"],),
        ),
    }


def control_plan(engine, task_id, account_id, policy, *, authorization_only=False):
    """Read-only channel resolution shared by admission and the actual executor.

    Expiry never grants permission to go online. Offline intent remains scoped
    to its accounts; a different account's intent cannot disable this route.
    """
    from .execution_channels import offline_only, read_state

    channel = effective(engine, policy, task_id, account_id)
    until = epoch(policy["ends_at"])
    if until <= engine.clock():
        raise CloudUnavailable("TaskChannelWindowExpired")
    from .local_primary import configured, valid

    with engine.store.connect() as db:
        primary = configured(db)
        grant = valid(db, account_id) if primary else None
        bound = channel_binding(db, account_id)
    if primary:
        if authorization_only:
            raise CloudUnavailable("LocalPrimaryTaskAuthorizationNotRequired")
        if channel != "local":
            raise CloudUnavailable("LocalPrimaryCloudTaskHeld")
        if not grant:
            raise CloudUnavailable("LocalPrimaryAccountNotAuthorized")
        return dict(
            task_id=task_id,
            account_id=account_id,
            channel=channel,
            until=until,
            no_cloud=True,
            cloud=None,
            local_state={},
            node_id=grant["node_id"],
            store_id=grant["store_id"],
            authorization_only=False,
            primary=True,
            primary_grant=grant,
        )
    # Explicit task-only coordination is durable local intent for THIS account.
    # A new task cannot borrow it or renew it implicitly through the cloud.
    coordinated_local = channel == "local" and bool(bound.get("coordination_task_id"))
    scoped_offline = offline_only(engine.store, account_id)
    no_cloud = (scoped_offline or coordinated_local) and not authorization_only
    if no_cloud and channel == "cloud":
        raise CloudUnavailable("LocalOfflineModeActive")
    cloud = None if no_cloud else CloudControl.configured()
    state = read_state(engine.store)
    node_id = (state.get("node_id") if no_cloud and scoped_offline else None) or (
        cloud.node_id if cloud else os.getenv("AP_NODE_ID", socket.gethostname())
    )
    stored = engine.store.one("SELECT value FROM meta WHERE key='execution_store_id'")
    return dict(
        task_id=task_id,
        account_id=account_id,
        channel=channel,
        until=until,
        no_cloud=no_cloud,
        cloud=cloud,
        local_state=state,
        node_id=node_id,
        store_id=stored["value"] if stored else None,
        authorization_only=authorization_only,
    )


def local_fence_source(engine, plan):
    from .execution_channels import independent_active

    if plan.get("primary"):
        from .local_primary import valid

        with engine.store.connect() as db:
            grant = valid(db, plan["account_id"])
        return (
            "local_primary"
            if grant and grant["generation"] == plan["primary_grant"]["generation"]
            else None
        )
    state = plan["local_state"]
    if (
        plan["channel"] == "local"
        and plan["account_id"] in state.get("account_ids", [])
        and state.get("until", 0) >= plan["until"]
        and state.get("store_id") == plan["store_id"]
        and state.get("node_id") == plan["node_id"]
        and plan["store_id"] is not None
        and independent_active(engine)
    ):
        return "legacy"
    required = engine.store.one("SELECT value FROM meta WHERE key='cloud_control_required'")
    if not plan["cloud"] and not (required and required["value"] == "1"):
        return "standalone"
    return None


def control_snapshot(engine, db, plan, *, job_id=""):
    """Validate current account ownership without acquiring or changing it."""
    now = engine.clock()
    account_id, task_id, channel, until, no_cloud, cloud, local_state, node_id, store_id = (
        plan[k]
        for k in (
            "account_id",
            "task_id",
            "channel",
            "until",
            "no_cloud",
            "cloud",
            "local_state",
            "node_id",
            "store_id",
        )
    )
    prior = db.execute(
        "SELECT * FROM account_task_channels WHERE account_id=?", (account_id,)
    ).fetchone()
    prior = dict(prior) if prior else None
    primary_supersede = bool(
        plan.get("primary")
        and prior
        and prior["until_at"] <= now
        and plan["primary_grant"].get("prior_generation") == prior["generation"]
    )
    if prior and prior["retry_at"] > now and not primary_supersede:
        raise CloudUnavailable(
            safe_channel_code(prior["error"] or "TaskChannelHandoffPending"),
            retry_after_seconds=max(1, math.ceil(prior["retry_at"] - now)),
        )
    if handoff_blocks(db, prior, task_id, now) and not primary_supersede:
        raise CloudUnavailable("TaskChannelHandoffPending")
    old_binding = channel_binding(db, account_id)
    if (
        no_cloud
        and old_binding.get("coordination_task_id")
        and old_binding["coordination_task_id"] != task_id
        and not primary_supersede
    ):
        raise CloudUnavailable("LocalAuthorizationTaskScopeMismatch")
    bound = binding_matches(prior, old_binding, node_id, store_id)
    if no_cloud and prior and prior["channel"] == "cloud" and not primary_supersede:
        proof = local_state.get("offline_fences", {}).get(account_id, {})
        if (
            not bound
            or prior["state"] != "ready"
            or prior["until_at"] > now
            or proof.get("generation") != prior["generation"]
            or proof.get("until_at") != prior["until_at"]
        ):
            raise CloudUnavailable("OfflinePriorOwnershipUnverified")
    from .x_history_ack import has_ack

    x_history_ack = plan["authorization_only"] and has_ack(db, task_id, account_id)
    reusable = bool(
        prior
        and prior["state"] == "ready"
        and bound
        and prior["channel"] == channel
        and prior["until_at"] >= until
        and original_fence_valid(db, old_binding, now, task_id)
        and not (old_binding.get("fence_source") == "standalone" and cloud)
        and not (x_history_ack and not old_binding.get("x_history_ack_required"))
    )
    if not reusable:
        if db.execute(
            "SELECT 1 FROM execution_jobs WHERE account_id=? AND state='leased' AND lease_until>? AND id<>?",
            (account_id, now, job_id),
        ).fetchone():
            raise CloudUnavailable("TaskChannelHandoffBusy")
        if db.execute(
            "SELECT 1 FROM cloud_receipt_outbox WHERE json_extract(lease,'$.account_id')=? OR job_id IN "
            "(SELECT id FROM execution_jobs WHERE account_id=?) LIMIT 1",
            (account_id, account_id),
        ).fetchone():
            raise CloudUnavailable("LocalChannelReceiptPending")
        if not cloud and (
            plan["authorization_only"] or channel != "local" or not local_fence_source(engine, plan)
        ):
            raise CloudUnavailable("CloudControlConfigurationMissing")
    return prior, old_binding, bound, reusable


def control_preflight(engine, task_id, account_id, policy):
    """A local eligibility check, never a cloud claim or a send permission."""
    plan = control_plan(engine, task_id, account_id, policy)
    with engine.store.connect() as db:
        _, _, _, reusable = control_snapshot(engine, db, plan)
    return {
        "channel": plan["channel"],
        "ownership": "confirmed"
        if reusable
        else "local_fence"
        if local_fence_source(engine, plan)
        else "coordination_required",
    }


class TaskChannelRouter:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def handoff_tasks(self, job, prior):
        from .execution_channels import read_state

        if job.get("authorization_only"):
            return [job["task_id"]]
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
        from .execution_timing import stage

        try:
            with stage(self.engine, job["task_id"], "account_control", job_id=job["id"]):
                context, cloud = self._prepare(job)
        except Exception as exc:
            details = {
                "job_id": job["id"],
                "account_id": job["account_id"],
                "stage": "authorization",
                "reason": safe_channel_code(str(exc)),
            }
            diagnostic = getattr(exc, "diagnostics", None)
            if isinstance(diagnostic, dict):
                if diagnostic.get("remote_response_received") is True:
                    details["execution_step"] = "account_block_import"
                    if diagnostic.get("cause") in {
                        "database_locked",
                        "database_error",
                        "sqlite_busy",
                        "sqlite_error",
                    }:
                        details["exception_type"] = "SQLiteError"
                elif diagnostic.get("operation") == "snapshot_sync":
                    details["execution_step"] = "cloud_snapshot_sync"
            if isinstance(exc, CloudUnavailable):
                exc.account_control_failure = True
            self.store.audit_best_effort(
                "execution.channel_failed",
                details,
                task_id=job["task_id"],
            )
            raise
        audit_route(
            self.engine,
            job,
            context,
            stage="authorization_only" if job.get("authorization_only") else "authorized",
        )
        return context, cloud

    def _prepare(self, job):
        from .execution_channels import binding

        now = self.engine.clock()
        authorization_only = job.get("authorization_only") is True
        if authorization_only:
            from .local_authorization import validate_scope

            with self.store.connect() as db:
                if not owns_job(db, job, now):
                    raise CloudUnavailable("LocalAuthorizationLeaseExpired")
                validate_scope(self.engine, db, job["task_id"], job["account_id"], now)
        task = self.store.one("SELECT policy FROM tasks WHERE id=?", (job["task_id"],))
        if not task:
            raise CloudUnavailable("TaskChannelTaskMissing")
        policy = json.loads(task["policy"])
        plan = control_plan(
            self.engine,
            job["task_id"],
            job["account_id"],
            policy,
            authorization_only=authorization_only,
        )
        channel, until, no_cloud, cloud, local_state, node_id = (
            plan[k] for k in ("channel", "until", "no_cloud", "cloud", "local_state", "node_id")
        )
        store_id = binding(self.engine)
        plan["store_id"] = store_id
        if authorization_only and (node_id != job["node_id"] or store_id != job["store_id"]):
            raise CloudUnavailable("LocalChannelBindingMismatch")
        if (
            not authorization_only
            and cloud
            and self.store.one(
                "SELECT 1 FROM cloud_receipt_outbox WHERE json_extract(lease,'$.account_id')=? LIMIT 1",
                (job["account_id"],),
            )
        ):
            CloudJournal(self.engine, cloud).flush(account_id=job["account_id"])
        with self.store.connect(write=True) as db:
            if not owns_job(db, job, self.engine.clock()):
                raise CloudUnavailable("PersistentJobClaimExpired")
            from .x_history_ack import has_ack

            x_history_ack = authorization_only and has_ack(db, job["task_id"], job["account_id"])
            prior, old_binding, bound, reusable = control_snapshot(
                self.engine, db, plan, job_id=job["id"]
            )
            if reusable:
                generation = prior["generation"]
                authority = old_binding
            else:
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
                if authorization_only:
                    authority.update(
                        coordination_task_id=job["task_id"],
                        coordination_manifest_hash=job["version"],
                    )
                    if x_history_ack:
                        authority["x_history_ack_required"] = True
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
                if authorization_only:
                    if not cloud:
                        raise CloudUnavailable("CloudControlConfigurationMissing")
                    observed = cloud.request(
                        "/v1/account-channel/status", {"account_id": job["account_id"]}
                    ).get("channel")
                    if not ack_matches(observed, authority):
                        raise CloudUnavailable("TaskChannelHandoffUnverified")
                if channel == "cloud":
                    if not cloud:
                        raise CloudUnavailable("CloudControlConfigurationMissing")
                    cloud.flush_settings(self.engine, account_id=job["account_id"])
                    cloud.sync(self.engine, task_ids=[job["task_id"]])
            else:
                fence_source = local_fence_source(self.engine, plan)
                if not authorization_only and channel == "local" and fence_source:
                    if fence_source == "local_primary":
                        authority["primary_grant_generation"] = plan["primary_grant"]["generation"]
                    if fence_source == "legacy":
                        authority["legacy_generation"] = local_state.get("generation")
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
                        if authorization_only:
                            cloud.sync_channel_authorization(self.engine, job)
                        else:
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
                    if authorization_only:
                        validate_scope(
                            self.engine, db, job["task_id"], job["account_id"], self.engine.clock()
                        )
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
        if authorization_only:
            return {**context, "task_channel": False, "authorization_only": True}, cloud
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
