"""Explicit, task-scoped control coordination. Never an outbound attempt."""

import json
import os
import socket
import uuid

from .cloud_control import CloudUnavailable
from .continuation_preflight import _account_blocker
from .execution_channels import binding, independent_account_supported
from .models import digest, epoch
from .routing import route_for, task_route
from .store import Store

LEASE_SECONDS = 180


def _handoff_key(task_id, account_id, item_id):
    return f"local_task_handoff:{task_id}:{account_id}:{item_id}"


def _handoff_scope(db, source_task_id, target_task_id, account_id, item_id, now, node_id, store_id):
    """Check a single, immutable, distinct X target against existing local authority."""
    from .interaction_intents import event_id, intent_blocker
    from .models import digest
    from .task_channels import binding_matches, channel_binding, original_fence_valid
    from .x_history_ack import allowed_unknown_ids

    source = db.execute("SELECT * FROM tasks WHERE id=?", (source_task_id,)).fetchone()
    target = db.execute("SELECT * FROM tasks WHERE id=?", (target_task_id,)).fetchone()
    account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    item = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    route = db.execute("SELECT * FROM account_task_channels WHERE account_id=?", (account_id,)).fetchone()
    bound = channel_binding(db, account_id)
    state_row = db.execute("SELECT value FROM meta WHERE key='execution_channel'").fetchone()
    scope_row = db.execute("SELECT value FROM meta WHERE key='local_channel_authority'").fetchone()
    state = json.loads(state_row[0]) if state_row else {}
    scope = json.loads(scope_row[0]) if scope_row else {}
    if not (source and target and account and item and source_task_id != target_task_id):
        raise ValueError("LocalHandoffScopeInvalid")
    for task in (source, target):
        policy = json.loads(task["policy"])
        if (task["state"] != "active" or task["phase"] != "ready" or not task["manifest_hash"]
            or policy.get("execution_channel") != "local" or account_id not in json.loads(task["account_ids"])
            or epoch(policy["ends_at"]) <= now):
            raise ValueError("LocalHandoffScopeInvalid")
    if (account["platform"] != "x" or account["mode"] != "live" or not account["identity_hash"]
        or account["status"] != "healthy" or account["disabled"] or account["cooldown_until"] > now
        or item["task_id"] != target_task_id or item["account_id"] != account_id
        or item["status"] != "not_attempted" or item["attempt_id"] or item["claimed_at"]
        or item["external_reference"]):
        raise ValueError("LocalHandoffTargetUnsafe")
    payload = json.loads(item["payload"])
    event = event_id("x", payload)
    if (payload.get("action") != "reply" or not event or digest(payload) != item["payload_hash"]
        or intent_blocker(db, account, target, payload, item_id)
        or db.execute("SELECT 1 FROM attempts WHERE item_id=?", (item_id,)).fetchone()):
        raise ValueError("LocalHandoffTargetUnsafe")
    if (not route or route["state"] != "ready" or route["channel"] != "local"
        or route["task_id"] != source_task_id or bound.get("coordination_task_id") != source_task_id
        or not binding_matches(route, bound, node_id, store_id)
        or not original_fence_valid(db, bound, now, source_task_id)
        or allowed_unknown_ids(db, source_task_id, account_id, now) is None):
        raise ValueError("LocalHandoffOwnershipUnverified")
    if (state.get("active") != "local" or state.get("offline_only") is not True
        or state.get("authorization") != "local_queue"
        or state.get("until", 0) < epoch(json.loads(target["policy"])["ends_at"])
        or not state.get("generation")
        or state.get("node_id") != node_id or state.get("store_id") != store_id
        or account_id not in state.get("account_ids", [])
        or sorted(scope.get("account_ids", [])) != sorted(state.get("account_ids", []))
        or (state.get("through_task_ids") and not {source_task_id, target_task_id}.issubset(state["through_task_ids"]))
        or state.get("account_authorizations", {}).get(account_id, {}).get("source") != "explicit_user_local_task"):
        raise ValueError("LocalHandoffAuthorityMissing")
    return {"source_task_id": source_task_id, "source_manifest_hash": source["manifest_hash"],
            "task_id": target_task_id, "manifest_hash": target["manifest_hash"],
            "account_id": account_id, "identity_hash": account["identity_hash"],
            "item_id": item_id, "payload_hash": item["payload_hash"], "event": event,
            "node_id": node_id, "store_id": store_id}


def activate_local_task_handoff(engine, source_task_id, target_task_id, account_id, item_id, *, source):
    """Use a verified local fence in the user's existing account-local scope.

    The selected target proves why this explicit handoff is needed. No task,
    payload, outcome, attempt, or job is changed; queue and dedupe guards remain.
    """
    if not isinstance(source, str) or not source.strip() or len(source) > 500:
        raise ValueError("ExplicitLocalHandoffRequired")
    now = engine.clock()
    node_id = os.getenv("AP_NODE_ID", socket.gethostname())
    store_id = binding(engine)
    with engine.store.connect(write=True) as db:
        key = _handoff_key(target_task_id, account_id, item_id)
        if db.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone():
            raise ValueError("LocalHandoffAlreadyActivated")
        value = _handoff_scope(db, source_task_id, target_task_id, account_id, item_id, now, node_id, store_id)
        from .x_history_ack import allowed_unknown_ids
        acknowledged = allowed_unknown_ids(db, source_task_id, account_id, now)
        blocker = _account_blocker(db, account_id, now, task_id=source_task_id,
                                   acknowledged_unknown_ids=acknowledged)
        if blocker:
            raise ValueError(blocker)
        account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if db.execute("SELECT 1 FROM account_blocks WHERE account_id=? AND state='active' AND (until_at IS NULL OR until_at>?)", (account_id, now)).fetchone():
            raise ValueError("OfflineAccountBlocked")
        for alias in db.execute("SELECT id FROM accounts WHERE platform='x' AND identity_hash=? AND id<>?", (account["identity_hash"], account_id)):
            if _account_blocker(db, alias[0], now, acknowledged_unknown_ids=acknowledged):
                raise ValueError("LocalAuthorizationAliasUnresolved")
        route = route_for(dict(account))
        if (db.execute("SELECT 1 FROM account_lanes WHERE account_id=? AND expires_at>?", (account_id, now)).fetchone()
            or (route.resource and db.execute("SELECT 1 FROM execution_resources WHERE resource=? AND expires_at>?", (route.resource, now)).fetchone())):
            raise ValueError("LocalAuthorizationBusy")
        state = json.loads(db.execute("SELECT value FROM meta WHERE key='execution_channel'").fetchone()[0])
        generation = uuid.uuid4().hex
        authority = {"generation": generation, "account_id": account_id, "node_id": node_id,
                     "store_id": store_id, "task_id": source_task_id, "channel": "local",
                     "until_at": state["until"], "fence_source": "legacy",
                     "legacy_generation": state["generation"]}
        from .task_channels import save_binding
        save_binding(db, authority)
        db.execute("UPDATE account_task_channels SET generation=?,until_at=?,state='ready',fence_source='legacy',updated_at=?,error=NULL,retry_at=0 WHERE account_id=?",
                   (generation, state["until"], now, account_id))
        db.execute("INSERT INTO meta VALUES(?,?)", (key, json.dumps({"scope": value, "source": source.strip(), "at": now, "generation": generation}, sort_keys=True)))
        Store.audit(db, "execution.account_channel_changed", {"account_id": account_id, "channel": "local", "generation": generation}, source_task_id, now)
        Store.audit(db, "execution.explicit_local_task_handoff", {
            "account_id": account_id, "source_task_id": source_task_id, "item_id": item_id,
            "manifest_hash": value["manifest_hash"], "source": source.strip(), "generation": generation,
            "attempt_created": False, "old_outcomes_changed": False}, target_task_id, now)
    return {"item_id": item_id, "generation": generation, "channel": "local", "attempt_created": False}


def authorization_route(engine, db, account, policy):
    if policy.get("execution_method") != "phone":
        if not independent_account_supported(db, account):
            raise CloudUnavailable("LocalAuthorizationRouteUnsupported")
        return route_for(account)
    service = getattr(
        engine.adapters, "phone_reddit" if account["platform"] == "reddit" else "phone_social", None
    )
    if not service or account["id"] not in service.config.bindings:
        raise CloudUnavailable("PhoneBindingRequired")
    if not service.fresh(account):
        raise CloudUnavailable("FreshPhoneIdentityRequired")
    route = task_route(account, {}, "phone", engine.adapters)
    binding = service.config.bindings[account["id"]]
    if (
        route.transport != "phone"
        or route.resource != "phone:" + digest(binding.device_id)
        or policy.get("execution_node_os", "any") not in {"any", binding.node_os}
    ):
        raise CloudUnavailable("LocalAuthorizationRouteUnsupported")
    return route


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
    if account["mode"] != "live":
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
    from .x_history_ack import allowed_unknown_ids, has_ack

    acknowledged = allowed_unknown_ids(db, task_id, account_id, now)
    if has_ack(db, task_id, account_id) and acknowledged is None:
        raise CloudUnavailable("XHistoryAcknowledgementChanged")
    blocker = _account_blocker(
        db, account_id, now, task_id=task_id, acknowledged_unknown_ids=acknowledged
    )
    if blocker:
        raise CloudUnavailable(blocker)
    if account["identity_hash"]:
        for alias in db.execute(
            "SELECT id FROM accounts WHERE platform=? AND identity_hash=? AND id<>?",
            (account["platform"], account["identity_hash"], account_id),
        ).fetchall():
            if _account_blocker(db, alias[0], now, acknowledged_unknown_ids=acknowledged):
                raise CloudUnavailable("LocalAuthorizationAliasUnresolved")
    route = authorization_route(engine, db, dict(account), policy)
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
