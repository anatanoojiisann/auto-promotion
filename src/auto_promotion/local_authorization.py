"""Explicit, task-scoped control coordination. Never an outbound attempt."""

import json
import os
import socket
import uuid

from .cloud_control import CloudUnavailable
from .continuation_preflight import _account_blocker
from .execution_channels import binding, independent_account_supported
from .models import epoch
from .routing import route_for
from .store import Store

LEASE_SECONDS = 180


def validate_scope(engine, db, task_id, account_id, now):
    task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    if not task or not account:
        raise CloudUnavailable("LocalAuthorizationScopeMissing")
    policy = json.loads(task["policy"])
    if (
        task["state"] != "active"
        or task["phase"] != "ready"
        or not task["manifest_hash"]
        or policy.get("execution_channel") != "local"
        or account_id not in json.loads(task["account_ids"])
        or epoch(policy["ends_at"]) <= now
    ):
        raise CloudUnavailable("LocalAuthorizationRequiresStartedLocalTask")
    if account["mode"] != "live" or not independent_account_supported(db, dict(account)):
        raise CloudUnavailable("LocalAuthorizationRouteUnsupported")
    if (
        not engine.live_enabled
        or account["status"] != "healthy"
        or account["disabled"]
        or account["cooldown_until"] > now
    ):
        raise CloudUnavailable("LocalAuthorizationAccountUnavailable")
    if db.execute(
        "SELECT 1 FROM account_blocks WHERE account_id=? AND state='active' "
        "AND (until_at IS NULL OR until_at>?)",
        (account_id, now),
    ).fetchone():
        raise CloudUnavailable("OfflineAccountBlocked")
    blocker = _account_blocker(db, account_id, now, task_id=task_id)
    if blocker:
        raise CloudUnavailable(blocker)
    if account["identity_hash"]:
        for alias in db.execute(
            "SELECT id FROM accounts WHERE platform=? AND identity_hash=? AND id<>?",
            (account["platform"], account["identity_hash"], account_id),
        ).fetchall():
            if _account_blocker(db, alias[0], now):
                raise CloudUnavailable("LocalAuthorizationAliasUnresolved")
    route = route_for(dict(account))
    if db.execute(
        "SELECT 1 FROM account_lanes WHERE account_id=? AND expires_at>?",
        (account_id, now),
    ).fetchone() or (
        route.resource
        and db.execute(
            "SELECT 1 FROM execution_resources WHERE resource=? AND expires_at>?",
            (route.resource, now),
        ).fetchone()
    ):
        raise CloudUnavailable("LocalAuthorizationBusy")
    # Do not use a narrow coordination request to flush global settings changes.
    if db.execute(
        "SELECT 1 FROM meta WHERE key IN "
        "('cloud_pending_settings','local_fallback_expiry_requested')"
    ).fetchone():
        raise CloudUnavailable("LocalAuthorizationOtherControlPending")
    return dict(task), dict(account)


def owns_authorization(db, job, now):
    row = db.execute(
        "SELECT * FROM channel_authorization_requests WHERE id=?", (job.get("id"),)
    ).fetchone()
    task = db.execute(
        "SELECT manifest_hash,state FROM tasks WHERE id=?", (job.get("task_id"),)
    ).fetchone()
    account = db.execute("SELECT * FROM accounts WHERE id=?", (job.get("account_id"),)).fetchone()
    store = db.execute("SELECT value FROM meta WHERE key='execution_store_id'").fetchone()
    return bool(
        row
        and task
        and task["state"] == "active"
        and task["manifest_hash"] == job.get("version")
        and account
        and row["identity_key"] == route_for(dict(account)).identity
        and store
        and store[0] == row["store_id"]
        and row["node_id"] == os.getenv("AP_NODE_ID", socket.gethostname())
        and row["state"] == "leased"
        and row["owner"] == job.get("owner")
        and row["lease_until"] > now
        and row["until_at"] > now
        and all(
            row[k] == job.get(k)
            for k in ("task_id", "account_id", "version", "node_id", "store_id")
        )
    )


def authorize_local_channel(engine, task_id, account_id, *, coordinate_once=False, source=""):
    """Coordinate this account/task only after an explicit human instruction/click.

    No task is started, job claimed, browser opened, attempt created or quota
    consumed. On uncertainty the same durable handoff generation is reconciled.
    """
    if (
        coordinate_once is not True
        or not isinstance(source, str)
        or not source.strip()
        or len(source) > 500
    ):
        raise ValueError("ExplicitLocalCoordinationRequired")
    now = engine.clock()
    with engine.store.connect() as db:
        validate_scope(engine, db, task_id, account_id, now)
    store_id = binding(engine)
    node_id = os.getenv("AP_NODE_ID", socket.gethostname())
    owner = uuid.uuid4().hex
    with engine.store.connect(write=True) as db:
        task, account = validate_scope(engine, db, task_id, account_id, now)
        identity = route_for(account).identity
        # Cover aliases as well as the account selected by the user.
        if db.execute(
            "SELECT 1 FROM execution_jobs WHERE state='leased' AND lease_until>? "
            "AND json_extract(route,'$.identity')=?",
            (now, identity),
        ).fetchone():
            raise CloudUnavailable("LocalAuthorizationBusy")
        prior = db.execute(
            "SELECT * FROM channel_authorization_requests WHERE identity_key=?", (identity,)
        ).fetchone()
        if prior and (
            prior["account_id"] != account_id
            or prior["state"] == "leased"
            and prior["lease_until"] > now
        ):
            raise CloudUnavailable("LocalAuthorizationBusy")
        if (
            prior
            and prior["state"] == "pending"
            and prior["until_at"] > now
            and prior["task_id"] != task_id
        ):
            raise CloudUnavailable("TaskChannelHandoffPending")
        request_id = prior["id"] if prior and prior["task_id"] == task_id else uuid.uuid4().hex
        until = epoch(json.loads(task["policy"])["ends_at"])
        db.execute(
            "INSERT INTO channel_authorization_requests VALUES(?,?,?,?,?,?,?,?,?,?,'leased',?,NULL,?,?) "
            "ON CONFLICT(account_id) DO UPDATE SET id=excluded.id,task_id=excluded.task_id,"
            "version=excluded.version,source=excluded.source,node_id=excluded.node_id,store_id=excluded.store_id,"
            "until_at=excluded.until_at,owner=excluded.owner,state='leased',lease_until=excluded.lease_until,"
            "error=NULL,updated_at=excluded.updated_at",
            (
                account_id,
                request_id,
                identity,
                task_id,
                task["manifest_hash"],
                source.strip(),
                node_id,
                store_id,
                until,
                owner,
                now + LEASE_SECONDS,
                now,
                now,
            ),
        )
        Store.audit(
            db,
            "execution.local_coordination_requested",
            {
                "account_id": account_id,
                "request_id": request_id,
                "source": source.strip(),
                "manifest_hash": task["manifest_hash"],
                "authorization_only": True,
            },
            task_id,
            now,
        )
    job = {
        "id": request_id,
        "account_id": account_id,
        "task_id": task_id,
        "version": task["manifest_hash"],
        "owner": owner,
        "authorization_only": True,
        "node_id": node_id,
        "store_id": store_id,
    }
    from .task_channels import TaskChannelRouter, safe_channel_code

    try:
        context, _ = TaskChannelRouter(engine).prepare(job)
        with engine.store.connect(write=True) as db:
            validate_scope(engine, db, task_id, account_id, engine.clock())
            if not owns_authorization(db, job, engine.clock()):
                raise CloudUnavailable("LocalAuthorizationLeaseExpired")
            db.execute(
                "UPDATE channel_authorization_requests SET state='ready',owner=NULL,"
                "lease_until=0,error=NULL,updated_at=? WHERE id=? AND owner=?",
                (engine.clock(), request_id, owner),
            )
            Store.audit(
                db,
                "execution.local_coordination_completed",
                {
                    "account_id": account_id,
                    "request_id": request_id,
                    "generation": context["generation"],
                    "authorization_only": True,
                },
                task_id,
                engine.clock(),
            )
        return {
            "authorized": True,
            "authorization_only": True,
            "account_id": account_id,
            "task_id": task_id,
            "generation": context["generation"],
            "until": until,
            "request_id": request_id,
            "attempt_created": False,
        }
    except Exception as exc:
        code = safe_channel_code(str(exc))
        with engine.store.connect(write=True) as db:
            db.execute(
                "UPDATE channel_authorization_requests SET state='pending',owner=NULL,"
                "lease_until=0,error=?,updated_at=? WHERE id=? AND owner=?",
                (code, engine.clock(), request_id, owner),
            )
        if isinstance(exc, CloudUnavailable) and str(exc) == code:
            raise
        raise CloudUnavailable(code) from None
